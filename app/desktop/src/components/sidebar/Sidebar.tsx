
import { useState } from "react";
import {
  Plus,
  Search,
  FileText,
  Images,
  Settings,
  PanelLeftClose,
  Code2,
  FolderKanban,
  PanelLeft,
  MessageSquare,
  Pin,
  Trash2,
  Pencil,
  RotateCcw,
  X,
  Settings2
} from "lucide-react";

import type { Conversation } from "../../types/chat";

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
  onNewChat: () => void;
  conversations: Conversation[];
  activeId: string | null;
  onSelectChat: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  onDelete: (id: string) => void;
  onSearch: (query: string) => void;
  trash: Conversation[];
  showTrash: boolean;
  onToggleTrash: () => void;
  onRestore: (id: string) => void;
  onPermanentDelete: (id: string) => void;
  onProjectsClick: () => void;
  onDocumentsClick: () => void;
  onCodingClick: () => void;
  activeView: "chat" | "projects" | "coding" | "documents" | "models";
  loading: boolean;
  onModelsClick: () => void;
}

export default function Sidebar(props: SidebarProps) {
  const {
    collapsed,
    onToggle,
    onNewChat,
    conversations,
    activeId,
    onSelectChat,
    onRename,
    onPin,
    onDelete,
    onSearch,
    trash,
    showTrash,
    onToggleTrash,
    onRestore,
    onPermanentDelete,
    onProjectsClick,
    onDocumentsClick,
    onCodingClick,
    activeView,
    loading,
    onModelsClick
  } = props;

  const [search, setSearch] = useState("");

  // Collapsed sidebar
  if (collapsed) {
    return (
      <aside className="sidebar collapsed">
        <button
          className="icon-button"
          onClick={onToggle}
          title="Expand sidebar"
        >
          <PanelLeft size={19} />
        </button>

        <button
          className="icon-button"
          onClick={onNewChat}
          title="New chat"
        >
          <Plus size={19} />
        </button>

        <button
          className={`icon-button ${
            activeView === "projects" ? "nav-active" : ""
          }`}
          onClick={onProjectsClick}
          title="Projects"
        >
          <FolderKanban size={19} />
        </button>

        <button
          className={`icon-button ${
            activeView === "documents" ? "nav-active" : ""
          }`}
          onClick={onDocumentsClick}
          title="Documents"
        >
          <FileText size={19} />
        </button>

        <button
          className={`icon-button ${
            activeView === "coding" ? "nav-active" : ""
          }`}
          onClick={onCodingClick}
          title="Coding Workspace"
        >
          <Code2 size={19} />
        </button>
        <button
          className={`nav-item ${activeView === "models" ? "nav-active" : ""}`}
          onClick={onModelsClick}
          type="button"
        >
          <Settings2 size={18} />
          <span>Model Manager</span>
        </button>
      </aside>
    );
  }

  // Rename conversation
  function renameChat(chat: Conversation) {
    const title = window.prompt(
      "Enter new conversation name:",
      chat.title
    );

    if (title?.trim()) {
      onRename(chat.id, title.trim());
    }
  }

  // Render conversation
  const renderChat = (chat: Conversation) => (
    <div
      className={`chat-list-item ${
        activeId === chat.id && activeView === "chat" ? "active" : ""
      }`}
      key={chat.id}
    >
      <button
        className="chat-select"
        onClick={() => onSelectChat(chat.id)}
      >
        <MessageSquare size={14} />
        <span>{chat.title}</span>
      </button>

      <div className="chat-actions">
        <button
          title={chat.is_pinned ? "Unpin" : "Pin"}
          onClick={() => onPin(chat.id, !chat.is_pinned)}
        >
          <Pin size={13} />
        </button>

        <button
          title="Rename"
          onClick={() => renameChat(chat)}
        >
          <Pencil size={13} />
        </button>

        <button
          title="Move to trash"
          onClick={() => onDelete(chat.id)}
        >
          <Trash2 size={13} />
        </button>
      </div>
    </div>
  );

  return (
    <aside className="sidebar">

      {/* Brand */}
      <div className="brand">
        <div className="brand-logo">✦</div>

        <span>LOCAL AI STUDIO</span>

        <button
          className="icon-button brand-toggle"
          onClick={onToggle}
          title="Collapse sidebar"
        >
          <PanelLeftClose size={18} />
        </button>
      </div>

      {/* New Chat */}
      <button
        className="new-chat-button"
        onClick={onNewChat}
      >
        <Plus size={18} />
        <span>New Chat</span>
      </button>

      {/* Search */}
      <div className="search-box">
        <Search size={16} />

        <input
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            onSearch(event.target.value);
          }}
          placeholder="Search chats..."
        />

        {search && (
          <button
            onClick={() => {
              setSearch("");
              onSearch("");
            }}
            title="Clear search"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {/* Workspace Navigation */}
      <div className="sidebar-section">
        WORKSPACE
      </div>

      <button
        className={`nav-item ${
          activeView === "projects" ? "nav-active" : ""
        }`}
        onClick={onProjectsClick}
      >
        <FolderKanban size={17} />
        Projects
      </button>

      <button
        className={`nav-item ${
          activeView === "documents" ? "nav-active" : ""
        }`}
        onClick={onDocumentsClick}
      >
        <FileText size={17} />
        Documents
      </button>

      <button
        className={`nav-item ${
          activeView === "coding" ? "nav-active" : ""
        }`}
        onClick={onCodingClick}
      >
        <Code2 size={17} />
        Coding Workspace
      </button>

      <button
        className={`nav-item ${activeView === "models" ? "nav-active" : ""}`}
        onClick={onModelsClick}
        type="button"
        title="Model Manager"
      >
        <Settings2 size={17} />
        Model Manager
      </button>

      <button
        className="nav-item"
        type="button"
        disabled
        title="Coming in a later phase"
      >
        <Images size={17} />
        Image Gallery
      </button>

      {/* Chats / Trash */}
      {!showTrash ? (
        <>
          {/* Pinned Chats */}
          <div className="sidebar-section">
            <Pin size={12} /> PINNED
          </div>

          <div className="recent-list pinned-list">
            {conversations
              .filter((chat) => chat.is_pinned)
              .map(renderChat)}

            {!conversations.some((chat) => chat.is_pinned) && (
              <div className="empty-sidebar">
                No pinned chats
              </div>
            )}
          </div>

          {/* Recent Chats */}
          <div className="sidebar-section">
            RECENT CHATS
          </div>

          <div className="recent-list recent-chats-list">
            {conversations
              .filter((chat) => !chat.is_pinned)
              .map(renderChat)}

            {!loading && conversations.length === 0 && (
              <div className="empty-sidebar">
                No conversations yet
              </div>
            )}
          </div>
        </>
      ) : (
        <>
          {/* Trash */}
          <div className="sidebar-section">
            TRASH
          </div>

          <div className="recent-list trash-list">
            {trash.map((chat) => (
              <div
                className="chat-list-item"
                key={chat.id}
              >
                <div className="chat-select">
                  <MessageSquare size={14} />
                  <span>{chat.title}</span>
                </div>

                <div className="chat-actions">
                  <button
                    title="Restore"
                    onClick={() => onRestore(chat.id)}
                  >
                    <RotateCcw size={13} />
                  </button>

                  <button
                    title="Delete permanently"
                    onClick={() => {
                      if (
                        window.confirm(
                          "Permanently delete this conversation and its messages?"
                        )
                      ) {
                        onPermanentDelete(chat.id);
                      }
                    }}
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            ))}

            {trash.length === 0 && (
              <div className="empty-sidebar">
                Trash is empty
              </div>
            )}
          </div>
        </>
      )}

      {/* Bottom Controls */}
      <div className="sidebar-bottom">

        <button
          className={`nav-item ${
            showTrash ? "nav-active" : ""
          }`}
          onClick={onToggleTrash}
        >
          <Trash2 size={17} />
          {showTrash ? "Back to Chats" : "Trash"}
        </button>

        <button
          className="nav-item"
          type="button"
          disabled
        >
          <Settings size={17} />
          Settings
        </button>

        {/* Profile */}
        <div className="profile">
          <div className="avatar">U</div>

          <div>
            <strong>Local User</strong>
            <small>Personal workspace</small>
          </div>
        </div>

      </div>
    </aside>
  );
}
