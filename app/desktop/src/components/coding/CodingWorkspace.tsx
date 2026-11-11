
import { useCallback, useEffect, useRef, useState } from "react";
import type { editor } from "monaco-editor";
import Editor, { DiffEditor } from "@monaco-editor/react";
import { open } from "@tauri-apps/plugin-dialog";

import {
  Code2,
  Cpu, ChevronDown, ChevronUp,
  Folder,
  File,
  FolderPlus,
  FilePlus,
  Save,
  RefreshCw,
  Trash2,
  Pencil,
  X,
  Terminal,
  Bug,
  MessageSquare,
  ShieldCheck, Undo2, Redo2, History, Play, RotateCcw, Square,
} from "lucide-react";

import {
  codingApi,
  codingAiApi,
  type CodingWorkspaceEntry,
  type CodingWorkspaceInfo,
  type CodingWorkspaceFile,
  type CodingFileVersion,
} from "../../services/api";

import "./coding.css";

type Tab = CodingWorkspaceFile & {
  dirty: boolean;
};

interface Props {
  onError: (message: string) => void;
  onSuccess: (message: string) => void;
}

const languageFor = (name: string): string => {
  const extension = name.split(".").pop()?.toLowerCase() || "";

  const languages: Record<string, string> = {
    py: "python",
    tsx: "typescript",
    ts: "typescript",
    jsx: "javascript",
    js: "javascript",
    json: "json",
    html: "html",
    css: "css",
    md: "markdown",
    yml: "yaml",
    yaml: "yaml",
    rs: "rust",
    sql: "sql",
    sh: "shell",
    ps1: "powershell",
    toml: "ini",
  };

  return languages[extension] || "plaintext";
};

export default function CodingWorkspace({
  onError,
  onSuccess,
}: Props) {
  const [workspace, setWorkspace] =
    useState<CodingWorkspaceInfo | null>(null);

  const [entries, setEntries] =
    useState<CodingWorkspaceEntry[]>([]);

  const [folder, setFolder] = useState("");
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [active, setActive] = useState("");
  const [busy, setBusy] = useState(false);

  const [bottom, setBottom] = useState("Problems");
  const [assistantPrompt, setAssistantPrompt] = useState("");
  const [reviewOpen, setReviewOpen] = useState(false);
  const [proposalContent, setProposalContent] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [versions, setVersions] = useState<CodingFileVersion[]>([]);
  const [selectedVersion, setSelectedVersion] = useState("");
  const [versionContent, setVersionContent] = useState("");
  const [terminalCommand, setTerminalCommand] = useState("");
  const [terminalOutput, setTerminalOutput] = useState("Terminal ready. Commands require confirmation.\n");
  const [terminalBusy, setTerminalBusy] = useState(false);
  const [testRunner, setTestRunner] = useState("pytest");
  const [debugSession, setDebugSession] = useState<string | null>(null);
  const [debugOutput, setDebugOutput] = useState("");
  const [debugCommand, setDebugCommand] = useState("");
  const [codingModelStatus, setCodingModelStatus] = useState<any>(null);
  const [modelBusy, setModelBusy] = useState(false);
  const [assistantTask, setAssistantTask] = useState("ask");
  const [assistantResponse, setAssistantResponse] = useState("");
  const [assistantBusy, setAssistantBusy] = useState(false);
  const [bottomHeight, setBottomHeight] = useState(230);
  const [bottomCollapsed, setBottomCollapsed] = useState(false);
  const [resizingBottom, setResizingBottom] = useState(false);
  const terminalOutputRef = useRef<HTMLPreElement | null>(null);

  const activeTab = tabs.find(
    (tab) => tab.relative_path === active
  );

  // Refresh current folder
  const refresh = useCallback(
    async (path: string = folder) => {
      try {
        const files = await codingApi.listFiles(path);
        setEntries(files);
      } catch (error) {
        onError(String(error));
      }
    },
    [folder, onError]
  );

  // Load previously selected workspace
  const loadWorkspace = useCallback(async () => {
    try {
      const result = await codingApi.getWorkspace();

      setWorkspace(result);

      if (result.selected) {
        setFolder("");
        const files = await codingApi.listFiles("");
        setEntries(files);
      }
    } catch (error) {
      onError(String(error));
    }
  }, [onError]);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  // Select project folder
  const choose = async () => {
    try {
      const selected = await open({
        directory: true,
        multiple: false,
      });

      if (typeof selected !== "string") return;

      setBusy(true);

      const result = await codingApi.selectWorkspace(selected);

      setWorkspace(result);
      setFolder("");
      setTabs([]);
      setActive("");

      const files = await codingApi.listFiles("");
      setEntries(files);

      onSuccess("Project folder opened");
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  };

  // Open file or navigate into folder
  const openFile = async (entry: CodingWorkspaceEntry) => {
    if (entry.type === "directory") {
      setFolder(entry.relative_path);

      try {
        const files = await codingApi.listFiles(
          entry.relative_path
        );

        setEntries(files);
      } catch (error) {
        onError(String(error));
      }

      return;
    }

    const existing = tabs.find(
      (tab) => tab.relative_path === entry.relative_path
    );

    if (existing) {
      setActive(existing.relative_path);
      return;
    }

    try {
      const file = await codingApi.readFile(
        entry.relative_path
      );

      setTabs((previous) => [
        ...previous,
        {
          ...file,
          dirty: false,
        },
      ]);

      setActive(file.relative_path);
    } catch (error) {
      onError(String(error));
    }
  };

  // Save file
  const save = async (tab: Tab | undefined = activeTab) => {
    if (!tab) return;

    try {
      await codingApi.writeFile(
        tab.relative_path,
        tab.content
      );

      setTabs((previous) =>
        previous.map((item) =>
          item.relative_path === tab.relative_path
            ? { ...item, dirty: false }
            : item
        )
      );

      onSuccess(`Saved ${tab.name}`);
    } catch (error) {
      onError(String(error));
    }
  };

  // Editor changes
  const edit = (value: string | undefined) => {
    if (!activeTab) return;

    setTabs((previous) =>
      previous.map((tab) =>
        tab.relative_path === active
          ? {
              ...tab,
              content: value ?? "",
              dirty: true,
            }
          : tab
      )
    );
  };

  // Create file or folder
  const create = async (kind: "file" | "folder") => {
    const name = window.prompt(`Enter ${kind} name`);

    if (!name?.trim()) return;

    const path = [folder, name.trim()]
      .filter(Boolean)
      .join("/");

    try {
      await codingApi.createPath(path, kind);
      await refresh();

      onSuccess(`${kind} created`);
    } catch (error) {
      onError(String(error));
    }
  };

  // Rename active file
  const rename = async () => {
    if (!activeTab) return;

    const name = window.prompt(
      "New file name",
      activeTab.name
    );

    if (!name || name === activeTab.name) return;

    try {
      await codingApi.renamePath(active, name);

      setTabs((previous) =>
        previous.filter(
          (tab) => tab.relative_path !== active
        )
      );

      setActive("");

      await refresh();

      onSuccess("File renamed");
    } catch (error) {
      onError(String(error));
    }
  };

  // Delete active file
  const remove = async () => {
    if (!activeTab) return;

    const target = activeTab.relative_path;

    const confirmed = window.confirm(
      `Delete ${activeTab.name}? This cannot be undone.`
    );

    if (!confirmed) return;

    try {
      await codingApi.deletePath(target);

      setTabs((previous) =>
        previous.filter(
          (tab) => tab.relative_path !== target
        )
      );

      setActive("");

      await refresh();

      onSuccess("File deleted");
    } catch (error) {
      onError(String(error));
    }
  };

  // Navigate to parent folder
  const goUp = async () => {
    const parent = folder.includes("/")
      ? folder.slice(0, folder.lastIndexOf("/"))
      : "";

    setFolder(parent);

    await refresh(parent);
  };

  const openReview = () => {
    if (!activeTab) return;
    setProposalContent(activeTab.content);
    setReviewOpen(true);
  };

  const applyApprovedChange = async () => {
    if (!activeTab || !reviewOpen) return;
    if (!window.confirm(`Apply the reviewed changes to ${activeTab.relative_path}? The current file will be backed up first.`)) return;
    setReviewing(true);
    try {
      await codingApi.writeFile(activeTab.relative_path, proposalContent);
      setTabs(previous => previous.map(tab => tab.relative_path === activeTab.relative_path
        ? { ...tab, content: proposalContent, dirty: false }
        : tab));
      setReviewOpen(false);
      onSuccess(`Approved changes applied to ${activeTab.name}`);
    } catch (error) {
      onError(String(error));
    } finally {
      setReviewing(false);
    }
  };

  const loadHistory = async () => {
    if (!activeTab) return;
    try { setVersions(await codingApi.listVersions(activeTab.relative_path)); setSelectedVersion(""); setVersionContent(""); setHistoryOpen(true); }
    catch (error) { onError(String(error)); }
  };
  const chooseVersion = async (id: string) => {
    if (!activeTab) return;
    try { const version = await codingApi.readVersion(activeTab.relative_path, id); setSelectedVersion(id); setVersionContent(version.content); }
    catch (error) { onError(String(error)); }
  };
  const restoreVersion = async () => {
    if (!activeTab || !selectedVersion) return;
    if (!window.confirm("Restore this version? Current disk content will be saved as a new version first.")) return;
    try { await codingApi.restoreVersion(selectedVersion); const file = await codingApi.readFile(activeTab.relative_path); setTabs(prev => prev.map(t => t.relative_path === activeTab.relative_path ? { ...file, dirty: false } : t)); setHistoryOpen(false); onSuccess("Version restored"); }
    catch (error) { onError(String(error)); }
  };
  const runTerminal = async () => {
    if (!terminalCommand.trim()) return;
    const command = terminalCommand.trim();
    if (!window.confirm(`Run this command in the selected project?\n\n${command}\n\nIt may execute programs or modify files.`)) return;
    setTerminalBusy(true); setTerminalOutput(prev => prev + `\nPS> ${command}\n`);
    try { const result = await codingApi.runTerminal(command, folder, true); setTerminalOutput(prev => prev + (result.stdout || "") + (result.stderr ? `\n${result.stderr}` : "") + `\n[exit code: ${result.exit_code ?? "timeout"}]\n`); setTerminalCommand(""); }
    catch (error) { setTerminalOutput(prev => prev + String(error) + "\n"); }
    finally { setTerminalBusy(false); }
  };
  const runTests = async () => {
    if (!window.confirm(`Run ${testRunner} tests in the selected folder? Test code will execute locally.`)) return;
    setTerminalBusy(true); setBottom("Tests"); setTerminalOutput(`Running ${testRunner}...\n`);
    try { const result = await codingApi.runTests(testRunner, folder, true); setTerminalOutput((result.stdout || "") + (result.stderr ? `\n${result.stderr}` : "") + `\n[exit code: ${result.exit_code ?? "timeout"}]`); }
    catch (error) { setTerminalOutput(String(error)); }
    finally { setTerminalBusy(false); }
  };
  const startDebug = async () => {
    if (!activeTab || !activeTab.name.toLowerCase().endsWith(".py")) { onError("Open a Python (.py) file to debug"); return; }
    if (activeTab.dirty) { onError("Save the file before starting the debugger"); return; }
    if (!window.confirm(`Start Python debugger for ${activeTab.relative_path}? This executes the file.`)) return;
    try { const result = await codingApi.startDebug(activeTab.relative_path, true); setDebugSession(result.session_id); setDebugOutput(result.message + "\n"); setBottom("Debugger"); }
    catch (error) { onError(String(error)); }
  };
  useEffect(() => {
    if (!debugSession) return;
    let alive = true;
    const poll = async () => { try { const result = await codingApi.debugOutput(debugSession); if (alive && result.output) setDebugOutput(prev => prev + result.output); if (alive && !result.running) setDebugSession(null); } catch { if (alive) setDebugSession(null); } };
    const timer = window.setInterval(() => void poll(), 700); void poll();
    return () => { alive = false; window.clearInterval(timer); };
  }, [debugSession]);
  const sendDebug = async (command: string) => { if (!debugSession || !command.trim()) return; try { await codingApi.debugCommand(debugSession, command); setDebugOutput(prev => prev + `\n(Pdb) ${command}\n`); } catch (error) { onError(String(error)); } };
  const stopDebug = async () => { if (!debugSession) return; try { await codingApi.stopDebug(debugSession); } catch (error) { onError(String(error)); } setDebugSession(null); };

  useEffect(() => {
    void codingAiApi.modelStatus().then(setCodingModelStatus).catch(() => setCodingModelStatus(null));
  }, []);
  useEffect(() => {
    if (!resizingBottom) return;
    const move = (event: PointerEvent) => {
      const next = Math.max(130, Math.min(Math.round(window.innerHeight * 0.62), window.innerHeight - event.clientY));
      setBottomHeight(next);
    };
    const stop = () => setResizingBottom(false);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
  }, [resizingBottom]);
  useEffect(() => {
    const output = terminalOutputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }, [terminalOutput, debugOutput]);

  const toggleCodingModel = async () => {
    setModelBusy(true);
    try {
      if (codingModelStatus?.available || codingModelStatus?.process_running) {
        const result = await codingAiApi.stopModel();
        onSuccess(result.message);
      } else {
        const result = await codingAiApi.startModel();
        onSuccess(result.message);
      }
      setCodingModelStatus(await codingAiApi.modelStatus());
    } catch (error) {
      onError(String(error));
      try { setCodingModelStatus(await codingAiApi.modelStatus()); } catch { /* keep last status */ }
    } finally { setModelBusy(false); }
  };

  const askCodingAssistant = async () => {
    if (!activeTab) { onError("Open a source file first"); return; }
    if (!assistantPrompt.trim()) { onError("Enter a request for the coding assistant"); return; }
    if (!codingModelStatus?.available) { onError("Load Qwen2.5-Coder using the Model button first"); return; }
    setAssistantBusy(true); setAssistantResponse("");
    try {
      const selection = editorRef.current?.getModel() && editorRef.current?.getSelection()
        ? editorRef.current.getModel()!.getValueInRange(editorRef.current.getSelection()!) : "";
      const result = await codingAiApi.ask({ task: assistantTask, prompt: assistantPrompt.trim(), relative_path: activeTab.relative_path, code: activeTab.content, selection });
      if (result.proposal) {
        let proposed = result.content.trim();
        const fenced = proposed.match(/^```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?)\n```$/);
        if (fenced) proposed = fenced[1];
        setProposalContent(proposed);
        setReviewOpen(true);
        setAssistantResponse("A proposed full-file change is ready. Review the diff and approve it only if it looks correct.");
      } else {
        setAssistantResponse(result.content);
      }
      setAssistantPrompt("");
    } catch (error) { onError(String(error)); }
    finally { setAssistantBusy(false); }
  };

  return (
    <div className="ide-shell">

      {/* TOP BAR */}
      <header className="ide-topbar">
        <div className="ide-brand">
          <Code2 size={18} />
          <strong>Coding Workspace</strong>
          <span>
            {workspace?.name || "No project selected"}
          </span>
        </div>

        <div className="ide-actions">
          <button title="Undo (Ctrl+Z)" disabled={!activeTab} onClick={() => editorRef.current?.trigger("toolbar", "undo", null)}><Undo2 size={15} /> Undo</button>
          <button title="Redo (Ctrl+Y)" disabled={!activeTab} onClick={() => editorRef.current?.trigger("toolbar", "redo", null)}><Redo2 size={15} /> Redo</button>
          <button
            onClick={() => void save()}
            disabled={!activeTab || !activeTab.dirty}
          >
            <Save size={15} /> Save
          </button>

          <button onClick={() => void refresh()}>
            <RefreshCw size={15} /> Refresh
          </button>

          <button
            onClick={() => void choose()}
            disabled={busy}
          >
            {workspace?.selected
              ? "Change Folder"
              : "Select Folder"}
          </button>
        </div>
      </header>

      {/* EMPTY STATE */}
      {!workspace?.selected ? (
        <div className="ide-empty">
          <Folder size={42} />

          <h2>Open a project folder</h2>

          <p>
            Select a local folder to start editing files.
          </p>

          <button
            onClick={() => void choose()}
            disabled={busy}
          >
            Select Project Folder
          </button>
        </div>
      ) : (
        <div className="ide-main">

          {/* LEFT EXPLORER */}
          <aside className="ide-explorer">
            <div className="ide-pane-title">
              EXPLORER

              <span>
                <button
                  title="New file"
                  onClick={() => void create("file")}
                >
                  <FilePlus size={15} />
                </button>

                <button
                  title="New folder"
                  onClick={() => void create("folder")}
                >
                  <FolderPlus size={15} />
                </button>

                <button
                  title="Refresh"
                  onClick={() => void refresh()}
                >
                  <RefreshCw size={14} />
                </button>
              </span>
            </div>

            <div className="ide-root">
              <Folder size={14} />
              {workspace.name}
            </div>

            <div className="ide-file-list">
              {folder && (
                <button
                  className="ide-file"
                  onClick={() => void goUp()}
                >
                  ↰ ..
                </button>
              )}

              {entries.map((entry) => (
                <button
                  className={`ide-file ${
                    active === entry.relative_path
                      ? "selected"
                      : ""
                  }`}
                  key={entry.relative_path}
                  onClick={() => void openFile(entry)}
                >
                  {entry.type === "directory" ? (
                    <Folder size={14} />
                  ) : (
                    <File size={14} />
                  )}

                  <span>{entry.name}</span>
                </button>
              ))}
            </div>

            <div className="ide-safe">
              <ShieldCheck size={14} />
              Files stay inside selected folder
            </div>
          </aside>

          {/* CENTRE EDITOR */}
          <section className="ide-editor-area">

            <div className="ide-tabs">
              {tabs.map((tab) => (
                <button
                  key={tab.relative_path}
                  className={
                    active === tab.relative_path
                      ? "active"
                      : ""
                  }
                  onClick={() =>
                    setActive(tab.relative_path)
                  }
                >
                  {tab.name}
                  {tab.dirty ? " ●" : ""}

                  <X
                    size={13}
                    onClick={(event) => {
                      event.stopPropagation();

                      if (
                        tabs.some(
                          (item) =>
                            item.relative_path ===
                              tab.relative_path &&
                            item.dirty
                        )
                      ) {
                        const confirmed = window.confirm(
                          "This file has unsaved changes. Close without saving?"
                        );

                        if (!confirmed) return;
                      }

                      setTabs((previous) =>
                        previous.filter(
                          (item) =>
                            item.relative_path !==
                            tab.relative_path
                        )
                      );

                      if (active === tab.relative_path) {
                        setActive(
                          tabs.find(
                            (item) =>
                              item.relative_path !==
                              tab.relative_path
                          )?.relative_path || ""
                        );
                      }
                    }}
                  />
                </button>
              ))}
            </div>

            {activeTab ? (
              <>
                <div className="ide-file-toolbar">
                  <span>{activeTab.relative_path}</span>

                  <div>
                    <button onClick={() => void loadHistory()} title="File version history"><History size={14} /> History</button>
                    <button onClick={() => void startDebug()} title="Start Python debugger"><Bug size={14} /> Debug</button>
                    <button
                      onClick={() => void rename()}
                      title="Rename"
                    >
                      <Pencil size={14} />
                    </button>

                    <button
                      onClick={() => void remove()}
                      title="Delete"
                    >
                      <Trash2 size={14} />
                    </button>

                    <button
                      onClick={openReview}
                      title="Preview proposed changes before applying"
                    >
                      Review change
                    </button>
                    <button
                      onClick={() => void save()}
                      disabled={!activeTab.dirty}
                    >
                      <Save size={14} /> Save
                    </button>
                  </div>
                </div>

                <div className="ide-monaco">
                  <Editor
                    height="100%"
                    path={activeTab.relative_path}
                    language={languageFor(activeTab.name)}
                    value={activeTab.content}
                    onChange={edit}
                    onMount={(editor) => { editorRef.current = editor; }}
                    theme="vs-dark"
                    options={{
                      fontSize: 14,
                      minimap: { enabled: false },
                      wordWrap: "on",
                      automaticLayout: true,
                      scrollBeyondLastLine: false,
                      tabSize: 4,
                      formatOnPaste: true,
                      formatOnType: false,
                    }}
                  />
                </div>
              </>
            ) : (
              <div className="ide-editor-empty">
                <Code2 size={34} />
                <p>Select a file from Explorer to edit</p>
              </div>
            )}
          </section>

          {/* RIGHT AI ASSISTANT */}
          <aside className="ide-assistant">
            <div className="ide-pane-title">
              AI CODING ASSISTANT
              <span className="ide-model-state">{codingModelStatus?.available ? "MODEL READY" : "MODEL UNLOADED"}</span>
            </div>
            <div className="ide-model-control">
              <div><Cpu size={15}/><span>Qwen2.5-Coder 3B</span></div>
              <button onClick={() => void toggleCodingModel()} disabled={modelBusy || !codingModelStatus?.model_exists}>
                {modelBusy ? "Please wait…" : codingModelStatus?.available || codingModelStatus?.process_running ? "Unload model" : "Load model"}
              </button>
            </div>
            <div className="ide-assistant-body">
              <MessageSquare size={24}/>
              <strong>Work with your open file</strong>
              <p>Ask questions or request a code change. Proposed edits open in Diff Preview and are never saved without your approval.</p>
              {assistantResponse && <pre className="ide-assistant-response">{assistantResponse}</pre>}
            </div>
            <div className="ide-assistant-compose">
              <select value={assistantTask} onChange={e => setAssistantTask(e.target.value)}>
                <option value="ask">Ask about code</option><option value="explain">Explain code</option><option value="debug">Find bugs</option><option value="review">Review code</option><option value="generate">Generate / modify code</option><option value="fix">Fix bugs (propose edit)</option><option value="refactor">Refactor (propose edit)</option>
              </select>
              <textarea placeholder="Describe what you want the assistant to do…" value={assistantPrompt} onChange={e => setAssistantPrompt(e.target.value)} disabled={assistantBusy}/>
              <button onClick={() => void askCodingAssistant()} disabled={assistantBusy || !assistantPrompt.trim() || !activeTab}>
                {assistantBusy ? "Thinking…" : "Send to local model"}
              </button>
            </div>
          </aside>
        </div>
      )}

      {reviewOpen && activeTab && (
        <div className="ide-review-backdrop" role="presentation">
          <section className="ide-review-dialog" role="dialog" aria-modal="true" aria-label="Review proposed code changes">
            <header className="ide-review-header">
              <div>
                <strong>Review proposed changes</strong>
                <span>{activeTab.relative_path} · Original vs proposed</span>
              </div>
              <button onClick={() => setReviewOpen(false)} disabled={reviewing} aria-label="Close review"><X size={18} /></button>
            </header>
            <div className="ide-review-help">
              The AI-generated proposal is shown on the right. You can edit it before approval. Green lines are additions and red lines are removals. Nothing is saved until you approve it.
            </div>
            <div className="ide-review-editor">
              <DiffEditor
                height="100%"
                language={languageFor(activeTab.name)}
                original={activeTab.content}
                modified={proposalContent}
                onMount={(editor) => {
                    const modifiedEditor = editor.getModifiedEditor();

                    modifiedEditor.onDidChangeModelContent(() => {
                        setProposalContent(modifiedEditor.getValue());
                    });
                }}
                theme="vs-dark"
                options={{
                    readOnly: false,
                    renderSideBySide: true,
                    minimap: { enabled: false },
                    automaticLayout: true,
                    scrollBeyondLastLine: false,
                    wordWrap: "on",
                    originalEditable: false,
                    diffWordWrap: "on",
                }}
            />
            </div>
            <footer className="ide-review-footer">
              <button className="ide-review-reject" onClick={() => setReviewOpen(false)} disabled={reviewing}>Reject / Close</button>
              <button className="ide-review-approve" onClick={() => void applyApprovedChange()} disabled={reviewing || proposalContent === activeTab.content}>
                <ShieldCheck size={15} /> {reviewing ? "Applying..." : "Approve and apply"}
              </button>
            </footer>
          </section>
        </div>
      )}

      {historyOpen && activeTab && <div className="ide-review-backdrop"><section className="ide-history-dialog" role="dialog" aria-modal="true"><header className="ide-review-header"><div><strong>File version history</strong><span>{activeTab.relative_path} · Last 100 snapshots</span></div><button onClick={() => setHistoryOpen(false)}><X size={18}/></button></header><div className="ide-history-body"><div className="ide-history-list">{versions.length === 0 ? <p>No saved versions yet. Save after editing to create a snapshot.</p> : versions.map(version => <button key={version.id} className={selectedVersion === version.id ? "selected" : ""} onClick={() => void chooseVersion(version.id)}><strong>{new Date(version.created_at).toLocaleString()}</strong><small>{version.reason} · {version.size.toLocaleString()} bytes</small></button>)}</div><div className="ide-history-diff">{selectedVersion ? <DiffEditor height="100%" language={languageFor(activeTab.name)} original={activeTab.content} modified={versionContent} theme="vs-dark" options={{readOnly:true,renderSideBySide:true,minimap:{enabled:false},automaticLayout:true,originalEditable:false}}/> : <p>Select a version to compare with the current file.</p>}</div></div><footer className="ide-review-footer"><button className="ide-review-reject" onClick={() => setHistoryOpen(false)}>Close</button><button className="ide-review-approve" disabled={!selectedVersion} onClick={() => void restoreVersion()}><RotateCcw size={14}/> Restore selected version</button></footer></section></div>}

      {/* RESIZABLE BOTTOM PANEL */}
      <div className="ide-bottom-resize" onPointerDown={(event) => { event.preventDefault(); setBottomCollapsed(false); setResizingBottom(true); }} title="Drag to resize terminal and output panel" />
      <footer className={`ide-bottom ${bottomCollapsed ? "collapsed" : ""}`} style={{ height: bottomCollapsed ? 38 : bottomHeight }}>
        <div className="ide-bottom-tabs">
          {["Problems", "Terminal", "Output", "Tests", "Debugger"].map(
            (item) => (
              <button
                className={
                  bottom === item ? "active" : ""
                }
                key={item}
                onClick={() => setBottom(item)}
              >
                {item === "Problems" ? (
                  <Bug size={14} />
                ) : (
                  <Terminal size={14} />
                )}

                {item}
              </button>
            )
          )}
          <span className="ide-bottom-spacer" />
          <button className="ide-bottom-collapse" onClick={() => setBottomCollapsed(value => !value)} title={bottomCollapsed ? "Expand panel" : "Collapse panel"}>
            {bottomCollapsed ? <ChevronUp size={14}/> : <ChevronDown size={14}/>}
          </button>
        </div>

        {!bottomCollapsed && (bottom === "Terminal" ? <div className="ide-tool-panel"><pre ref={terminalOutputRef}>{terminalOutput}</pre><div className="ide-tool-row"><span>PS</span><input value={terminalCommand} onChange={e => setTerminalCommand(e.target.value)} onKeyDown={e => { if (e.key === "Enter") void runTerminal(); }} placeholder="PowerShell command (approval required)"/><button onClick={() => void runTerminal()} disabled={terminalBusy || !terminalCommand.trim()}><Play size={14}/> Run</button></div></div>
        : bottom === "Tests" ? <div className="ide-tool-panel"><div className="ide-tool-row"><select value={testRunner} onChange={e => setTestRunner(e.target.value)}><option value="pytest">pytest</option><option value="python -m unittest">unittest</option></select><button onClick={() => void runTests()} disabled={terminalBusy}><Play size={14}/> Run tests (approval required)</button></div><pre>{terminalOutput}</pre></div>
        : bottom === "Debugger" ? <div className="ide-tool-panel"><div className="ide-tool-row"><span>{debugSession ? "Python debugger active" : "Open a Python file and click Debug"}</span>{debugSession && <><button onClick={() => void sendDebug("continue")}>Continue</button><button onClick={() => void sendDebug("next")}>Step over</button><button onClick={() => void sendDebug("step")}>Step into</button><button onClick={() => void sendDebug("where")}>Call stack</button><button onClick={() => void sendDebug("p locals()")}>Inspect variables</button><button onClick={() => void stopDebug()}><Square size={13}/> Stop</button></>}</div>{debugSession && <div className="ide-tool-row"><input value={debugCommand} onChange={e => setDebugCommand(e.target.value)} onKeyDown={e => { if (e.key === "Enter") { void sendDebug(debugCommand); setDebugCommand(""); } }} placeholder="Pdb command: break 20 / p variable"/><button onClick={() => { void sendDebug(debugCommand); setDebugCommand(""); }}>Send</button></div>}<pre>{debugOutput || "Pdb commands: break 20, next, step, continue, where, p locals()."}</pre></div>
        : <div className="ide-bottom-content">{bottom === "Problems" ? "Use the debugger and test runner to inspect project issues." : "Editor and workspace output will appear here."}</div>)}
      </footer>
    </div>
  );
}
