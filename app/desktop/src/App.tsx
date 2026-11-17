
import { useCallback, useEffect, useState } from "react";

import Sidebar from "./components/sidebar/Sidebar";
import ChatArea from "./components/chat/ChatArea";
import ProjectsWorkspace from "./components/projects/ProjectsWorkspace";
import CodingWorkspace from "./components/coding/CodingWorkspace";
import DocumentsWorkspace from "./components/documents/DocumentsWorkspace";
import ModelsWorkspace from "./components/Models/ModelsWorkspace";

import { chatApi } from "./services/api";
import type {
  Conversation,
  ChatMessage,
  ModelInfo,
  Project,
} from "./types/chat";

import "./styles/main.css";
import "./styles/projects.css";

type MainView = "chat" | "projects" | "coding" | "documents" | "models";

function App() {
  const [collapsed, setCollapsed] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [trash, setTrash] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeTitle, setActiveTitle] = useState("New Chat");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [showTrash, setShowTrash] = useState(false);
  const [modelInfo, setModelInfo] = useState<ModelInfo | null>(null);
  const [view, setView] = useState<MainView>("chat");
  const [projectRefreshKey, setProjectRefreshKey] = useState(0);

  const refreshChats = useCallback(async () => {
    try {
      const [chatData, trashData] = await Promise.all([
        chatApi.list(),
        chatApi.trash(),
      ]);

      setConversations(chatData);
      setTrash(trashData);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Unable to load conversations"
      );
    }
  }, []);

  useEffect(() => {
    refreshChats();
    chatApi.getModel().then(setModelInfo).catch(() => setModelInfo(null));
  }, [refreshChats]);

  useEffect(() => {
    let cancelled = false;

    async function performSearch() {
      if (!searchQuery.trim()) {
        await refreshChats();
        return;
      }

      try {
        const results = await chatApi.search(searchQuery);
        if (!cancelled) setConversations(results);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Search failed");
        }
      }
    }

    performSearch();

    return () => {
      cancelled = true;
    };
  }, [searchQuery, refreshChats]);

  async function createNewChat() {
    try {
      setError("");
      setSuccess("");
      setView("chat");
      setShowTrash(false);

      const conversation = await chatApi.create({ title: "New Chat" });

      await refreshChats();

      setActiveId(conversation.id);
      setActiveTitle(conversation.title);
      setMessages([]);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Unable to create chat"
      );
    }
  }

  async function selectChat(id: string) {
    try {
      setError("");
      setSuccess("");
      setLoading(true);
      setView("chat");

      const [chatData, messageData] = await Promise.all([
        chatApi.list(),
        chatApi.getMessages(id),
      ]);

      const selected = chatData.find((chat) => chat.id === id);

      if (!selected) throw new Error("Conversation not found");

      setConversations(chatData);
      setActiveId(id);
      setActiveTitle(selected.title);
      setMessages(messageData);
      setShowTrash(false);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Unable to open conversation"
      );
    } finally {
      setLoading(false);
    }
  }

  async function sendMessage(content: string) {
    let conversationId = activeId;
    let temporaryAssistantId: string | null = null;

    try {
      setError("");
      setSuccess("");

      if (!conversationId) {
        const conversation = await chatApi.create({ title: "New Chat" });
        conversationId = conversation.id;

        setActiveId(conversationId);
        setActiveTitle(conversation.title);
      }

      const currentConversationId = conversationId;

      const userMessage: ChatMessage = {
        id: `local-user-${Date.now()}`,
        conversation_id: currentConversationId,
        role: "user",
        content,
        created_at: new Date().toISOString(),
      };

      temporaryAssistantId = `local-assistant-${Date.now()}`;

      const assistantPlaceholder: ChatMessage = {
        id: temporaryAssistantId,
        conversation_id: currentConversationId,
        role: "assistant",
        content: "",
        created_at: new Date().toISOString(),
      };

      setMessages((previous) => [
        ...previous,
        userMessage,
        assistantPlaceholder,
      ]);

      setGenerating(true);

      await chatApi.streamChat(currentConversationId, content, (token) => {
        setMessages((previous) =>
          previous.map((item) =>
            item.id === temporaryAssistantId
              ? { ...item, content: item.content + token }
              : item
          )
        );
      });

      const [savedMessages, chatData, trashData] = await Promise.all([
        chatApi.getMessages(currentConversationId),
        chatApi.list(),
        chatApi.trash(),
      ]);

      setMessages(savedMessages);
      setConversations(chatData);
      setTrash(trashData);

      const current = chatData.find(
        (chat) => chat.id === currentConversationId
      );

      if (current) setActiveTitle(current.title);
    } catch (err) {
      if (temporaryAssistantId) {
        setMessages((previous) =>
          previous.filter((item) => item.id !== temporaryAssistantId)
        );
      }

      const message =
        err instanceof Error ? err.message : "Unable to generate response";

      setError(message);
      throw err;
    } finally {
      setGenerating(false);
    }
  }

  async function renameChat(id: string, title: string) {
    try {
      await chatApi.rename(id, title);
      await refreshChats();

      if (activeId === id) setActiveTitle(title);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rename failed");
    }
  }

  async function pinChat(id: string, pinned: boolean) {
    try {
      await chatApi.pin(id, pinned);
      await refreshChats();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Pin operation failed");
    }
  }

  async function deleteChat(id: string) {
    if (generating || !window.confirm("Move this conversation to Trash?")) {
      return;
    }

    try {
      await chatApi.delete(id);

      if (activeId === id) {
        setActiveId(null);
        setActiveTitle("New Chat");
        setMessages([]);
      }

      await refreshChats();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  }

  async function restoreChat(id: string) {
    try {
      await chatApi.restore(id);
      await refreshChats();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Restore failed");
    }
  }

  async function permanentlyDeleteChat(id: string) {
    if (generating) return;

    try {
      await chatApi.permanentlyDelete(id);

      if (activeId === id) {
        setActiveId(null);
        setActiveTitle("New Chat");
        setMessages([]);
      }

      await refreshChats();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Permanent deletion failed"
      );
    }
  }

  async function updateFeedback(
    messageId: string,
    rating: "good" | "bad" | null
  ) {
    try {
      if (rating) {
        await chatApi.setFeedback(messageId, rating);
      } else {
        await chatApi.clearFeedback(messageId);
      }

      setMessages((previous) =>
        previous.map((item) =>
          item.id === messageId ? { ...item, feedback: rating } : item
        )
      );
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Unable to save feedback"
      );
    }
  }

  async function regenerateMessage(messageId: string) {
    if (!activeId || generating) return;

    setError("");
    setGenerating(true);

    setMessages((previous) =>
      previous.map((item) =>
        item.id === messageId ? { ...item, content: "" } : item
      )
    );

    try {
      await chatApi.regenerate(activeId, messageId, (token) => {
        setMessages((previous) =>
          previous.map((item) =>
            item.id === messageId
              ? { ...item, content: item.content + token }
              : item
          )
        );
      });

      setMessages(await chatApi.getMessages(activeId));
      await refreshChats();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Regeneration failed");

      const saved = await chatApi.getMessages(activeId).catch(() => messages);
      setMessages(saved);
    } finally {
      setGenerating(false);
    }
  }

  async function addResponseToProject(messageId: string) {
    try {
      setError("");
      setSuccess("");

      const projects: Project[] = await chatApi.listProjects();
      let selectedProjectId = "";

      if (projects.length) {
        const options = projects
          .map((project, index) => `${index + 1}. ${project.name}`)
          .join("\n");

        const answer = window.prompt(
          `Choose a project by number, or type a new project name:\n${options}`
        );

        if (!answer?.trim()) return;

        const index = Number(answer.trim());

        if (Number.isInteger(index) && index >= 1 && index <= projects.length) {
          selectedProjectId = projects[index - 1].id;
        } else {
          selectedProjectId = (
            await chatApi.createProject(answer.trim())
          ).id;
        }
      } else {
        const name = window.prompt("Enter a name for the new project:");

        if (!name?.trim()) return;

        selectedProjectId = (await chatApi.createProject(name.trim())).id;
      }

      await chatApi.addToProject(messageId, selectedProjectId);

      setSuccess("Response added to project successfully.");
      setProjectRefreshKey((value) => value + 1);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Unable to add response to project"
      );
    }
  }

  async function openConversationFromProject(conversationId: string) {
    await selectChat(conversationId);
  }

  return (
    <div className="app-container">
      <Sidebar
        collapsed={collapsed}
        onToggle={() => setCollapsed((previous) => !previous)}
        onNewChat={createNewChat}
        conversations={conversations}
        activeId={activeId}
        onSelectChat={selectChat}
        onRename={renameChat}
        onPin={pinChat}
        onDelete={deleteChat}
        onSearch={setSearchQuery}
        trash={trash}
        showTrash={showTrash}
        onToggleTrash={() => {
          setShowTrash((previous) => !previous);
          setView("chat");
        }}
        onRestore={restoreChat}
        onPermanentDelete={permanentlyDeleteChat}
        onProjectsClick={() => {
          setView("projects");
          setShowTrash(false);
          setError("");
          setSuccess("");
        }}
        onCodingClick={() => {
          setView("coding");
          setShowTrash(false);
          setError("");
          setSuccess("");
        }}
        onDocumentsClick={() => {
          setView("documents");
          setShowTrash(false);
          setError("");
          setSuccess("");
        }}
        onModelsClick={() => {
          setView("models");
          setShowTrash(false);
          setError("");
          setSuccess("");
        }}
        activeView={view}
        loading={loading || generating}
      />

      {view === "models" ? (
        <ModelsWorkspace />
      ) : view === "documents" ? (
        <DocumentsWorkspace onError={setError} onSuccess={setSuccess} />
      ) : view === "coding" ? (
        <CodingWorkspace onError={setError} onSuccess={setSuccess} />
      ) : view === "projects" ? (
        <ProjectsWorkspace
          refreshKey={projectRefreshKey}
          onOpenConversation={openConversationFromProject}
          onError={setError}
          onSuccess={setSuccess}
        />
      ) : (
        <ChatArea
          key={activeId ?? "empty-chat"}
          title={activeTitle}
          messages={messages}
          onSend={sendMessage}
          onFeedback={updateFeedback}
          onRegenerate={regenerateMessage}
          onAddToProject={addResponseToProject}
          modelName={modelInfo?.name || "Mistral · Local"}
          loading={loading}
          generating={generating}
          error={error}
        />
      )}

      {success && (
        <div className="app-toast success-toast">{success}</div>
      )}
    </div>
  );
}

export default App;
