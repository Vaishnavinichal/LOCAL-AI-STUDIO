
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import type { editor } from "monaco-editor";
import Editor, { DiffEditor } from "@monaco-editor/react";

import {
  Code2,
  ChevronDown, ChevronUp,
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
  Brain,
  ShieldCheck, Undo2, Redo2, History, Play, RotateCcw, Square,
  Search, Settings, PanelLeftClose,
} from "lucide-react";

import {
  codingApi,
  agentApi,
  type CodingWorkspaceEntry,
  type CodingWorkspaceInfo,
  type CodingWorkspaceFile,
  type CodingFileVersion,
  type CodingTestSuite,
  type CodingTestRun,
  type AgentTask,
  type AgentChangeSet,
  type CodingTerminalProposal,
} from "../../services/api";

import "./coding.css";
import AgentWorkspace from "./AgentWorkspace";
import { loadTerminalUiSnapshot, persistTerminalUiSnapshot, terminalCanReceiveInput, terminalInputPayload } from "./terminalSession";
import { isTauriRuntime, selectProjectDirectory } from "../../services/platform";

type Tab = CodingWorkspaceFile & {
  dirty: boolean;
};

interface Props {
  onError: (message: string) => void;
  onSuccess: (message: string) => void;
  theme: "dark" | "light";
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
  theme,
}: Props) {
  const editorTheme = theme === "light" ? "vs" : "vs-dark";
  const [workspace, setWorkspace] =
    useState<CodingWorkspaceInfo | null>(null);
  const [agentRecoveryOpen, setAgentRecoveryOpen] = useState(false);

  const [entries, setEntries] =
    useState<CodingWorkspaceEntry[]>([]);

  const [folder, setFolder] = useState("");
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [active, setActive] = useState("");
  const [busy, setBusy] = useState(false);

  const [bottom, setBottom] = useState("Problems");
  const [reviewOpen, setReviewOpen] = useState(false);
  const [proposalContent, setProposalContent] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [versions, setVersions] = useState<CodingFileVersion[]>([]);
  const [selectedVersion, setSelectedVersion] = useState("");
  const [versionContent, setVersionContent] = useState("");
  const [terminalSaved] = useState(() => loadTerminalUiSnapshot(window.localStorage));
  const [terminalCommand, setTerminalCommand] = useState("");
  const [terminalInteractive, setTerminalInteractive] = useState(terminalSaved.interactive);
  const [terminalInput, setTerminalInput] = useState("");
  const [terminalInputBusy, setTerminalInputBusy] = useState(false);
  const [terminalStdinClosed, setTerminalStdinClosed] = useState(terminalSaved.stdinClosed);
  const [terminalOutput, setTerminalOutput] = useState(terminalSaved.output);
  const [terminalBusy, setTerminalBusy] = useState(false);
  const [terminalExecutionId, setTerminalExecutionId] = useState(terminalSaved.executionId);
  const [terminalPreview, setTerminalPreview] = useState<CodingTerminalProposal | null>(null);
  const [terminalPreviewError, setTerminalPreviewError] = useState("");
  const [terminalStatus, setTerminalStatus] = useState(terminalSaved.status);
  const [terminalOwnerSession, setTerminalOwnerSession] = useState("");
  const [terminalHistory, setTerminalHistory] = useState<Array<Record<string, unknown>>>([]);
  const [testDiscovery, setTestDiscovery] = useState<{project_root:string;discovered_at:string;test_files:string[];suites:CodingTestSuite[]}|null>(null);
  const [testRuns, setTestRuns] = useState<CodingTestRun[]>([]);
  const [activeTestRun, setActiveTestRun] = useState<CodingTestRun|null>(null);
  const [testPreview, setTestPreview] = useState<{run:CodingTestRun;preview:any}|null>(null);
  const [testBusy, setTestBusy] = useState(false);
  const [testTaskId, setTestTaskId] = useState("");
  const [repairLimit, setRepairLimit] = useState(2);
  const [agentTasks, setAgentTasks] = useState<AgentTask[]>([]);
  const [testReport, setTestReport] = useState<any>(null);
  const [selectedTestSuiteId, setSelectedTestSuiteId] = useState("");
  const [agentFocusChange, setAgentFocusChange] = useState<{sessionId:string;changesetId:string}|null>(null);
  const [debugFixBusy, setDebugFixBusy] = useState(false);
  const [debugStage, setDebugStage] = useState("");
  const [debugDiagnosis, setDebugDiagnosis] = useState<any>(null);
  const [debugSession, setDebugSession] = useState<string | null>(null);
  const [debugOutput, setDebugOutput] = useState("");
  const [debugCommand, setDebugCommand] = useState("");
  const [bottomHeight, setBottomHeight] = useState(() => Number(localStorage.getItem("las.ide.bottomHeight")) || 230);
  const [bottomCollapsed, setBottomCollapsed] = useState(() => localStorage.getItem("las.ide.bottomCollapsed") === "true");
  const [agentLogs, setAgentLogs] = useState<Array<{id:string;event_type:string;message:string;created_at:string}>>([]);
  const [explorerWidth, setExplorerWidth] = useState(() => Number(localStorage.getItem("las.ide.explorerWidth")) || 220);
  const [assistantWidth, setAssistantWidth] = useState(() => Number(localStorage.getItem("las.ide.assistantWidth")) || 400);
  const [explorerCollapsed, setExplorerCollapsed] = useState(true);
  const [assistantCollapsed, setAssistantCollapsed] = useState(() => localStorage.getItem("las.ide.assistantCollapsed") === "true");
  const [assistantFocused, setAssistantFocused] = useState(() => localStorage.getItem("las.ide.agentFocused") === "true");
  const [editorFocused, setEditorFocused] = useState(() => localStorage.getItem("las.ide.editorFocused") === "true");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [resizingPane, setResizingPane] = useState<"explorer"|"assistant"|null>(null);
  const [resizingBottom, setResizingBottom] = useState(false);
  const resizeStart = useRef<{x:number;y:number;explorer:number;assistant:number;bottom:number}|null>(null);
  const [explorerError, setExplorerError] = useState("");
  const [fileFilter, setFileFilter] = useState("");
  const [cursorPosition, setCursorPosition] = useState({ line: 1, column: 1 });
  const [agentRunning, setAgentRunning] = useState(false);
  const [agentSessionId, setAgentSessionId] = useState("");
  const terminalOutputRef = useRef<HTMLPreElement | null>(null);

  useEffect(() => {
    persistTerminalUiSnapshot(window.localStorage, { executionId: terminalExecutionId, output: terminalOutput, status: terminalStatus, interactive: terminalInteractive, stdinClosed: terminalStdinClosed });
  }, [terminalExecutionId, terminalOutput, terminalStatus, terminalInteractive, terminalStdinClosed]);
  useEffect(() => {
    if (terminalExecutionId) { setBottom("Terminal"); setBottomCollapsed(false); }
  }, [terminalExecutionId]);

  const activeTab = tabs.find(
    (tab) => tab.relative_path === active
  );

  // Refresh current folder
  const refresh = useCallback(
    async (path: string = folder) => {
      try {
        const files = await codingApi.listFiles(path);
        setEntries(files);
        setExplorerError("");
      } catch (error) {
        setExplorerError(String(error));
        onError(String(error));
      }
    },
    [folder, onError]
  );

  const reloadAfterAgentChanges = useCallback(async (_changeset?: AgentChangeSet) => {
    setDebugStage("Approved changes applied; retest to verify the correction");
    await refresh();
    const refreshed = await Promise.all(tabs.map(async tab => {
      if (tab.dirty) return tab;
      try { return { ...(await codingApi.readFile(tab.relative_path)), dirty: false }; }
      catch { return null; }
    }));
    setTabs(refreshed.filter((tab): tab is Tab => tab !== null));
    if (active && !refreshed.some(tab => tab?.relative_path === active)) setActive("");
  }, [active, refresh, tabs]);

  // Load previously selected workspace
  const loadWorkspace = useCallback(async () => {
    try {
      const result = await codingApi.getWorkspace();

      setWorkspace(result);

      if (result.selected) {
        setFolder("");
        setFileFilter("");
        const files = await codingApi.listFiles("");
        setEntries(files);
        setExplorerError("");
      } else {
        setEntries([]);
        setExplorerError("");
      }
    } catch (error) {
      setExplorerError(String(error));
      onError(String(error));
    }
  }, [onError]);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  // Select project folder
  const choose = async () => {
    try {
      const selected = isTauriRuntime()
        ? await selectProjectDirectory()
        : (await codingApi.pickWorkspace()).path;

      if (!selected) return;

      setBusy(true);

      const result = await codingApi.selectWorkspace(selected);

      setWorkspace(result);
      setFolder("");
      setFileFilter("");
      setTabs([]);
      setActive("");

      const files = await codingApi.listFiles("");
      setEntries(files);
      setExplorerError("");

      onSuccess("Project folder opened");
    } catch (error) {
      const detail = String(error);
      const message = !isTauriRuntime() && /\b404\b|not found/i.test(detail)
        ? "The folder picker is missing from the running backend. Restart FastAPI, then choose the folder again."
        : detail;
      setExplorerError(message);
      onError(message);
    } finally {
      setBusy(false);
    }
  };

  // Open file or navigate into folder
  const openFile = async (entry: CodingWorkspaceEntry) => {
    if (entry.type === "directory") {
      setFolder(entry.relative_path);
      setFileFilter("");

      try {
        const files = await codingApi.listFiles(
          entry.relative_path
        );

        setEntries(files);
        setExplorerError("");
      } catch (error) {
        setExplorerError(String(error));
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

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (event.shiftKey) void Promise.all(tabs.filter(tab => tab.dirty).map(tab => save(tab)));
        else void save();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [tabs, activeTab]);

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
    setTerminalBusy(true); setTerminalOutput(`Preparing approval for: ${command}\n`);
    try {
      const proposal = await codingApi.previewTerminal(command, folder, "User approved terminal request from Coding Workspace", terminalInteractive ? 0 : 120, agentSessionId || undefined, terminalInteractive);
      const confirmed = window.confirm(`Review exact PowerShell command before execution\n\n${proposal.command}\n\nWorking directory: ${proposal.working_directory}\nReason: ${proposal.reason}\nMay modify files: ${proposal.may_modify_files ? "Yes" : "Not detected"}\nMay use network: ${proposal.may_use_network ? "Yes" : "Not detected"}\nMode: ${proposal.interactive ? "Interactive (no automatic timeout)" : "Non-interactive"}\nTimeout: ${proposal.timeout_seconds ? `${proposal.timeout_seconds}s` : "No automatic timeout"}\n\n${proposal.warning}\n\nApprove and execute?`);
      if (!confirmed) { setTerminalOutput("Command rejected. Nothing was executed.\n"); setTerminalBusy(false); return; }
      const started = await codingApi.executeTerminal(proposal.id);
      setTerminalExecutionId(started.execution_id); setTerminalOwnerSession(agentSessionId); setTerminalStatus(started.status); setTerminalStdinClosed(started.stdin_closed); setTerminalInput(""); setTerminalOutput(`PS> ${proposal.command}\n[${started.status}] ${started.working_directory}\n`); setTerminalCommand(""); setBottom("Terminal");
    }
    catch (error) { setTerminalOutput(prev => prev + String(error) + "\n"); setTerminalBusy(false); }
  };
  const previewAgentCommand = async (command: string, sessionId: string) => {
    if (terminalBusy || !command.trim()) return;
    setTerminalBusy(true); setTerminalPreviewError(""); setTerminalOutput("Preparing command preview. Nothing will execute until you approve it.\n");
    try {
      const preview = await codingApi.previewTerminal(command.trim(), ".", "Verification command from approved Agent plan", 120, sessionId);
      setTerminalPreview(preview); setTerminalCommand(preview.command); setAgentSessionId(sessionId);
      setBottom("Terminal"); setBottomCollapsed(false); setAssistantCollapsed(false);
    } catch (error) { const message = String(error); setTerminalPreviewError(message); setTerminalOutput(message + "\n"); onError(message); }
    finally { setTerminalBusy(false); }
  };
  const approveTerminalPreview = async () => {
    if (!terminalPreview || terminalBusy) return;
    setTerminalBusy(true); setTerminalPreviewError("");
    try {
      const started = await codingApi.executeTerminal(terminalPreview.id);
      setTerminalOwnerSession(terminalPreview.session_id || ""); setTerminalExecutionId(started.execution_id); setTerminalStatus(started.status); setTerminalInteractive(started.interactive); setTerminalStdinClosed(started.stdin_closed); setTerminalInput("");
      setTerminalOutput(`PS> ${terminalPreview.command}\n[${started.status}] ${terminalPreview.working_directory}\n`); setTerminalPreview(null); setTerminalCommand("");
    } catch (error) { const message = String(error); setTerminalPreviewError(message); onError(message); }
    finally { setTerminalBusy(false); }
  };
  const rejectTerminalPreview = async () => {
    if (!terminalPreview || terminalBusy) return;
    setTerminalBusy(true); setTerminalPreviewError("");
    try { await codingApi.rejectTerminalProposal(terminalPreview.id); setTerminalPreview(null); setTerminalOutput("Command rejected. Nothing was executed.\n"); }
    catch (error) { const message = String(error); setTerminalPreviewError(message); onError(message); }
    finally { setTerminalBusy(false); }
  };
  const restoreDefaultLayout = () => { setExplorerWidth(220); setAssistantWidth(400); setBottomHeight(230); setExplorerCollapsed(true); setAssistantCollapsed(false); setBottomCollapsed(false); setAssistantFocused(false); setEditorFocused(false); };
  const stopTerminal = async () => { if (!terminalExecutionId) return; try { await codingApi.stopTerminal(terminalExecutionId); setTerminalStatus("stopping"); } catch (error) { onError(String(error)); } };
  const sendTerminalInput = async (eof = false) => {
    if (!terminalExecutionId || terminalInputBusy || !terminalCanReceiveInput(terminalStatus, terminalInteractive, terminalStdinClosed)) return;
    const text = terminalInput;
    if (!eof && !text) return;
    setTerminalInputBusy(true);
    try {
      const payload = terminalInputPayload(text);
      const result = await codingApi.sendTerminalInput(terminalExecutionId, payload, eof);
      setTerminalStdinClosed(result.execution.stdin_closed);
      if (!eof) setTerminalInput("");
    } catch (error) { onError(String(error)); }
    finally { setTerminalInputBusy(false); }
  };
  useEffect(() => {
    if (!terminalExecutionId) return;
    let alive = true;
    const poll = async () => {
      try {
        const result = await codingApi.terminalExecution(terminalExecutionId);
        if (!alive) return;
        setTerminalOutput(`PS> ${result.command}\n${result.stdout}${result.stderr ? `\n${result.stderr}` : ""}\n[${result.status}${result.exit_code === null ? "" : ` · exit ${result.exit_code}`} · ${result.working_directory}]`);
        setTerminalStatus(result.status);
        setTerminalStdinClosed(result.stdin_closed);
        if (result.status !== "running" && result.status !== "cancel_requested") {
          setTerminalBusy(false); setTerminalHistory(await codingApi.terminalHistory()); setTerminalExecutionId("");
        }
      } catch (error) { if (alive) { const detail=String(error);setTerminalBusy(false);setTerminalStatus("unavailable");if(/404|not found/i.test(detail))setTerminalExecutionId("");onError(detail); } }
    };
    const timer = window.setInterval(() => void poll(), 250); void poll();
    return () => { alive = false; window.clearInterval(timer); };
  }, [terminalExecutionId]);
  useEffect(() => {
    localStorage.setItem("las.ide.explorerWidth", String(explorerWidth));
    localStorage.setItem("las.ide.assistantWidth", String(assistantWidth));
    localStorage.setItem("las.ide.bottomHeight", String(bottomHeight));
    localStorage.setItem("las.ide.explorerCollapsed", String(explorerCollapsed));
    localStorage.setItem("las.ide.assistantCollapsed", String(assistantCollapsed));
    localStorage.setItem("las.ide.bottomCollapsed", String(bottomCollapsed));
    localStorage.setItem("las.ide.agentFocused", String(assistantFocused));
    localStorage.setItem("las.ide.editorFocused", String(editorFocused));
  }, [explorerWidth, assistantWidth, bottomHeight, explorerCollapsed, assistantCollapsed, bottomCollapsed, assistantFocused, editorFocused]);
  const refreshTestHistory = async () => { try { const rows:CodingTestRun[]=await codingApi.testHistory();setTestRuns(rows);if(activeTestRun){const selected=rows.find((row:CodingTestRun)=>row.id===activeTestRun.id);if(selected)setActiveTestRun(selected);} } catch(error) { onError(String(error)); } };
  const discoverTests = async () => { setTestBusy(true);try{const result=await codingApi.discoverTests();setTestDiscovery(result);setSelectedTestSuiteId(current=>current||result.suites[0]?.id||"");await refreshTestHistory();}catch(error){onError(String(error));}finally{setTestBusy(false);} };
  const prepareTestRun = async (suite: CodingTestSuite, repairOf?: string) => {
    if(!suite.available){onError(suite.missing_reason||"This test command is unavailable");return;}
    setTestBusy(true);setBottom("Tests");setBottomCollapsed(false);setDebugStage(repairOf?"Preparing retest for approved repair":"Preparing test command for approval");
    try{const result=await codingApi.prepareTest(suite.id,agentSessionId||undefined,testTaskId||undefined,repairOf);setTestPreview(result);setActiveTestRun(result.run);}
    catch(error){onError(String(error));}finally{setTestBusy(false);}
  };
  const approveTestRun = async () => { if(!testPreview)return;setTestBusy(true);setTerminalBusy(true);setDebugStage("Executing approved test command");try{const run=await codingApi.executeTest(testPreview.run.id);setActiveTestRun(run);if(run.execution_id){setTerminalExecutionId(run.execution_id);setTerminalOwnerSession(agentSessionId);setTerminalStatus("running");}setTestPreview(null);}catch(error){setTerminalBusy(false);onError(String(error));}finally{setTestBusy(false);} };
  const rejectTestRun = async () => { if(!testPreview)return;setTestBusy(true);try{const run=await codingApi.rejectTest(testPreview.run.id);setActiveTestRun(run);setTestPreview(null);await refreshTestHistory();}catch(error){onError(String(error));}finally{setTestBusy(false);} };
  const cancelTestRun = async () => { if(!activeTestRun)return;try{await codingApi.cancelTest(activeTestRun.id);setActiveTestRun({...activeTestRun,status:"cancelling"});}catch(error){onError(String(error));} };
  const openFailureFile = async (relativePath: string) => { try{const file=await codingApi.readFile(relativePath);setTabs(current=>current.some(tab=>tab.relative_path===file.relative_path)?current.map(tab=>tab.relative_path===file.relative_path?{...file,dirty:tab.dirty}:tab):[...current,{...file,dirty:false}]);setActive(file.relative_path);}catch(error){onError(String(error));} };
  const proposeDebugFix = async () => {
    if(!activeTestRun?.result||!(activeTestRun.result.status==="failed"||(activeTestRun.result.status==="error"&&["syntax_error","import_error","type_error","runtime_exception"].includes(activeTestRun.result.category)))){onError("Select a completed failed test or source error first");return;}
    if(!agentSessionId){onError("Select an approved Agent planning session before requesting a correction");return;}
    setDebugFixBusy(true);
    try{setDebugStage("Retrieving source evidence and diagnosing failure");const result=await agentApi.proposeDebugFix(agentSessionId,activeTestRun.id,testTaskId||undefined,tabs.filter(tab=>tab.dirty).map(tab=>tab.relative_path),repairLimit);setDebugDiagnosis(result.diagnosis);if(result.changeset){setDebugStage(`Correction proposal ${result.repair_attempt}/${repairLimit} waiting for review`);setAssistantCollapsed(false);setAgentFocusChange({sessionId:agentSessionId,changesetId:result.changeset.id});}else{setDebugStage("Repair stopped: diagnosis was uncertain or unsupported");onError(result.reason||result.diagnosis.root_cause);}}
    catch(error){onError(String(error));}finally{setDebugFixBusy(false);}
  };
  const loadTestReport = async () => { try{setTestReport(await codingApi.testReport(agentSessionId||undefined));}catch(error){onError(String(error));} };
  useEffect(()=>{if(!workspace?.selected)return;void discoverTests();},[workspace?.path]);
  useEffect(()=>{
    if(!activeTestRun||!(activeTestRun.status==="running"||activeTestRun.status==="cancelling"))return;
    let alive=true;
    const poll=async()=>{try{const run=await codingApi.getTestRun(activeTestRun.id);if(!alive)return;setActiveTestRun(run);if(run.status!=="running"&&run.status!=="cancelling"){setDebugStage(run.result?.status==="passed"?"Tests passed; repair loop stopped":"Test run finished; inspect results before continuing");await refreshTestHistory();}}catch(error){if(alive)onError(String(error));}};
    const timer=window.setInterval(()=>void poll(),700);void poll();return()=>{alive=false;window.clearInterval(timer);};
  },[activeTestRun?.id,activeTestRun?.status]);
  const startDebug = async () => {
    if (!activeTab || !activeTab.name.toLowerCase().endsWith(".py")) { onError("Open a Python (.py) file to debug"); return; }
    if (activeTab.dirty) { onError("Save the file before starting the debugger"); return; }
    if (!window.confirm(`Start Python debugger for ${activeTab.relative_path}? This executes the file.`)) return;
    try { const result = await codingApi.startDebug(activeTab.relative_path, true); setDebugSession(result.session_id); setDebugOutput(result.message + "\n"); setBottom("Debug Console"); }
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
    void codingApi.terminalHistory().then(setTerminalHistory).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!resizingBottom) return;
    const move = (event: PointerEvent) => {
      const start = resizeStart.current;
      if (!start) return;
      const shell = document.querySelector<HTMLElement>(".ide-shell");
      const max = Math.round((shell?.getBoundingClientRect().height || window.innerHeight) * 0.62);
      const next = Math.max(130, Math.min(max, start.bottom + start.y - event.clientY));
      setBottomHeight(next);
    };
    const stop = () => { setResizingBottom(false); resizeStart.current = null; };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
    window.addEventListener("pointercancel", stop, { once: true });
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); window.removeEventListener("pointercancel", stop); };
  }, [resizingBottom]);
  useEffect(() => {
    if (!resizingPane) return;
    const move = (event: PointerEvent) => {
      const start = resizeStart.current;
      if (!start) return;
      const delta = event.clientX - start.x;
      if (resizingPane === "explorer") setExplorerWidth(Math.max(150, Math.min(420, start.explorer + delta)));
      else setAssistantWidth(Math.max(240, Math.min(520, start.assistant - delta)));
    };
    const stop = () => { setResizingPane(null); resizeStart.current = null; };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
    window.addEventListener("pointercancel", stop, { once: true });
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); window.removeEventListener("pointercancel", stop); };
  }, [resizingPane]);
  useEffect(() => {
    const output = terminalOutputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }, [terminalOutput, debugOutput]);

  return (
    <div className={`ide-shell ${editorFocused?"editor-focused":""}`}>

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

          <button onClick={() => { void refresh(); void loadWorkspace(); }} title="Refresh project files and status">
            <RefreshCw size={15} /> Refresh
          </button>
          <button onClick={() => setExplorerCollapsed(value => !value)} title={explorerCollapsed ? "Show Explorer" : "Hide Explorer"}><Folder size={15} /> {explorerCollapsed ? "Explorer" : "Hide Explorer"}</button>

          <button
            onClick={() => void choose()}
            disabled={busy}
          >
            {workspace?.selected
              ? "Change Folder"
              : "Select Folder"}
          </button>
          <button onClick={() => setAssistantCollapsed(false)} disabled={!workspace?.selected} title="Open Autonomous AI Engineer"><Brain size={15} /> Agent</button>
          <button onClick={() => setSettingsOpen(value => !value)} title="Coding workspace settings"><Settings size={15} /> Settings</button>
          <button onClick={restoreDefaultLayout} title="Restore default workspace layout">Reset layout</button>
        </div>
      </header>
      {settingsOpen&&<div className="ide-settings-popover" role="dialog" aria-label="Coding workspace settings"><strong>Workspace settings</strong><label><input type="checkbox" checked={!explorerCollapsed} onChange={event=>setExplorerCollapsed(!event.target.checked)}/> Show Explorer panel</label><label><input type="checkbox" checked={!assistantCollapsed} onChange={event=>setAssistantCollapsed(!event.target.checked)}/> Show Agent panel</label><button onClick={()=>{restoreDefaultLayout();setSettingsOpen(false);}}>Restore default layout</button></div>}

      {/* EMPTY STATE */}
      {!workspace?.selected ? (
        <div className={`ide-empty ${agentRecoveryOpen?"agent-session-recovery":""}`}>
          <div className="ide-empty-default" style={{display:agentRecoveryOpen?"none":"flex"}}>
            <Folder size={42} />
            <h2>Open a project folder</h2>
            <p>Select a local folder to start editing files.</p>
            <button onClick={() => void choose()} disabled={busy}>Select Project Folder</button>
            <button className="ide-recovery-open" onClick={()=>setAgentRecoveryOpen(true)}>View saved Agent sessions</button>
          </div>
          <div className="ide-agent-recovery-host" style={{display:agentRecoveryOpen?"flex":"none"}}>
            <AgentWorkspace theme={theme} embedded initialMode="planning" focused={false} running={agentRunning} onToggleFocus={()=>{}} onClose={()=>setAgentRecoveryOpen(false)} onError={onError} onBusyChange={setAgentRunning} onSessionChange={setAgentSessionId} onTasksChange={setAgentTasks} onAgentLogs={setAgentLogs}/>
          </div>
        </div>
      ) : (
        <div className={`ide-main ${explorerCollapsed?"explorer-collapsed":""} ${assistantCollapsed?"assistant-collapsed":""} ${assistantFocused?"agent-focused":""}`} style={{"--explorer-width":`${explorerCollapsed?0:explorerWidth}px`,"--assistant-width":`${assistantCollapsed?0:assistantWidth}px`} as CSSProperties}>

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

            <div className="ide-root" title={workspace.path || workspace.name || "Selected project"}>
              <Folder size={14} />
              <span className="ide-root-name">{workspace.name || workspace.path?.split(/[\\/]/).filter(Boolean).pop() || workspace.path || "Selected project"}</span>
              <button className="ide-collapse-pane" title="Collapse Explorer" aria-label="Collapse Explorer" onClick={()=>setExplorerCollapsed(true)}><PanelLeftClose size={14}/></button>
            </div>

            <div className="ide-explorer-search-wrap"><Search size={13}/><input className="ide-explorer-search" value={fileFilter} onChange={event => setFileFilter(event.target.value)} placeholder="Filter files" aria-label="Filter files"/></div>

            <div className="ide-file-list">
              {explorerError&&<div className="ide-explorer-error" role="alert"><span>{explorerError}</span><button onClick={()=>void refresh()}>Retry</button></div>}
              {!explorerError&&entries.length===0&&<div className="ide-explorer-empty">{folder?"This folder is empty.":"No visible files in this project."}</div>}
              {!explorerError&&entries.length>0&&entries.filter(entry => `${entry.name} ${entry.relative_path}`.toLowerCase().includes(fileFilter.toLowerCase())).length===0&&<div className="ide-explorer-empty">No files match “{fileFilter}”.</div>}
              {folder && (
                <button
                  className="ide-file"
                  onClick={() => void goUp()}
                >
                  ↰ ..
                </button>
              )}

              {entries.filter(entry => `${entry.name} ${entry.relative_path}`.toLowerCase().includes(fileFilter.toLowerCase())).map((entry) => (
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

          {!explorerCollapsed&&<div className="ide-v-resize" role="separator" aria-orientation="vertical" aria-label="Resize Explorer" onPointerDown={event=>{event.preventDefault();resizeStart.current={x:event.clientX,y:event.clientY,explorer:explorerWidth,assistant:assistantWidth,bottom:bottomHeight};setResizingPane("explorer");}}/>}

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
                    onMount={(editor) => { editorRef.current = editor; editor.onDidChangeCursorPosition(position => setCursorPosition({ line: position.position.lineNumber, column: position.position.column })); }}
                    theme={editorTheme}
                    options={{
                      fontSize: 14,
                      minimap: { enabled: true, renderCharacters: false, scale: 1 },
                      lineNumbers: "on",
                      folding: true,
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

          {!assistantCollapsed&&<div className="ide-v-resize" role="separator" aria-orientation="vertical" aria-label="Resize AI panel" onPointerDown={event=>{event.preventDefault();resizeStart.current={x:event.clientX,y:event.clientY,explorer:explorerWidth,assistant:assistantWidth,bottom:bottomHeight};setResizingPane("assistant");}}/>}

          {/* RIGHT AI ASSISTANT */}
          <aside className="ide-assistant" style={{display:assistantCollapsed?"none":undefined}}>
            <div className="ide-agent-host"><AgentWorkspace theme={theme} embedded initialMode="planning" focused={assistantFocused} running={agentRunning||terminalStatus==="running"} onToggleFocus={()=>setAssistantFocused(value=>!value)} unsavedPaths={tabs.filter(tab=>tab.dirty).map(tab=>tab.relative_path)} onFilesApplied={reloadAfterAgentChanges} onAgentLogs={setAgentLogs} onBusyChange={setAgentRunning} onSessionChange={setAgentSessionId} onTasksChange={setAgentTasks} debugStage={debugStage} debugDiagnosis={debugDiagnosis} focusSessionId={agentFocusChange?.sessionId} focusChangesetId={agentFocusChange?.changesetId} onSessionControl={(action,sessionId)=>{if((action==="pause"||action==="cancel")&&terminalExecutionId&&terminalOwnerSession===sessionId)void stopTerminal();}} onProposeCommand={(command,sessionId)=>void previewAgentCommand(command,sessionId)} onClose={()=>setAssistantCollapsed(true)} onError={onError}/></div>
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
                theme={editorTheme}
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

      {historyOpen && activeTab && <div className="ide-review-backdrop"><section className="ide-history-dialog" role="dialog" aria-modal="true"><header className="ide-review-header"><div><strong>File version history</strong><span>{activeTab.relative_path} · Last 100 snapshots</span></div><button onClick={() => setHistoryOpen(false)}><X size={18}/></button></header><div className="ide-history-body"><div className="ide-history-list">{versions.length === 0 ? <p>No saved versions yet. Save after editing to create a snapshot.</p> : versions.map(version => <button key={version.id} className={selectedVersion === version.id ? "selected" : ""} onClick={() => void chooseVersion(version.id)}><strong>{new Date(version.created_at).toLocaleString()}</strong><small>{version.reason} · {version.size.toLocaleString()} bytes</small></button>)}</div><div className="ide-history-diff">{selectedVersion ? <DiffEditor height="100%" language={languageFor(activeTab.name)} original={activeTab.content} modified={versionContent} theme={editorTheme} options={{readOnly:true,renderSideBySide:true,minimap:{enabled:false},automaticLayout:true,originalEditable:false}}/> : <p>Select a version to compare with the current file.</p>}</div></div><footer className="ide-review-footer"><button className="ide-review-reject" onClick={() => setHistoryOpen(false)}>Close</button><button className="ide-review-approve" disabled={!selectedVersion} onClick={() => void restoreVersion()}><RotateCcw size={14}/> Restore selected version</button></footer></section></div>}

      {/* RESIZABLE BOTTOM PANEL */}
      <div className="ide-bottom-resize" onPointerDown={(event) => { event.preventDefault(); resizeStart.current={x:event.clientX,y:event.clientY,explorer:explorerWidth,assistant:assistantWidth,bottom:bottomHeight}; setBottomCollapsed(false); setResizingBottom(true); }} title="Drag to resize terminal and output panel" />
      <footer className={`ide-bottom ${bottomCollapsed ? "collapsed" : ""}`} style={{ height: bottomCollapsed ? 38 : bottomHeight }}>
        <div className="ide-bottom-tabs">
          {["Problems", "Terminal", "Output", "Debug Console", "Tests", "Agent Logs", "Command History"].map(
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

        {!bottomCollapsed && (bottom === "Terminal" ? <div className="ide-tool-panel"><div className="ide-tool-toolbar"><span>PowerShell · {terminalStatus || "idle"}</span><span/>{terminalExecutionId&&<button onClick={()=>void stopTerminal()}><Square size={13}/> Stop process</button>}<button onClick={()=>setTerminalOutput("")}>Clear</button><button onClick={()=>void navigator.clipboard.writeText(terminalOutput)}>Copy output</button></div>{terminalPreview&&<section className="ide-terminal-preview"><header><strong>Command preview · not executed</strong><span>Awaiting explicit approval</span></header><code>{terminalPreview.command}</code><small>Working directory: {terminalPreview.working_directory}</small><small>Reason: {terminalPreview.reason}</small><small>Timeout: {terminalPreview.timeout_seconds}s · May modify files: {terminalPreview.may_modify_files?"Yes":"Not detected"} · May use network: {terminalPreview.may_use_network?"Yes":"Not detected"}</small><p>{terminalPreview.warning}</p>{terminalPreviewError&&<p role="alert">{terminalPreviewError}</p>}<footer><button onClick={()=>void rejectTerminalPreview()} disabled={terminalBusy}>Reject</button><button onClick={()=>void approveTerminalPreview()} disabled={terminalBusy}><ShieldCheck size={13}/>{terminalBusy?"Please wait…":"Approve & Execute"}</button></footer></section>}{terminalPreviewError&&!terminalPreview&&<p className="ide-terminal-error" role="alert">{terminalPreviewError}</p>}<pre ref={terminalOutputRef}>{terminalOutput}</pre>{terminalExecutionId&&terminalInteractive&&<div className="ide-terminal-session-input"><span>Input</span><textarea aria-label="Interactive process input" rows={2} value={terminalInput} onChange={event=>setTerminalInput(event.target.value)} placeholder="Enter one or more lines; Send input submits them to the process." disabled={terminalStdinClosed||terminalStatus!=="running"||terminalInputBusy}/><button onClick={()=>void sendTerminalInput()} disabled={terminalStdinClosed||terminalStatus!=="running"||terminalInputBusy||!terminalInput.trim()}>Send input</button><button onClick={()=>void sendTerminalInput(true)} disabled={terminalStdinClosed||terminalStatus!=="running"||terminalInputBusy}>Send EOF</button></div>}<div className="ide-tool-row">{!terminalExecutionId&&<label className="ide-terminal-interactive-toggle"><input type="checkbox" checked={terminalInteractive} onChange={event=>setTerminalInteractive(event.target.checked)} disabled={terminalBusy||!!terminalPreview}/>Interactive session</label>}<span>PS</span><input value={terminalCommand} onChange={e => setTerminalCommand(e.target.value)} onKeyDown={e => { if (e.key === "Enter") void runTerminal(); }} placeholder="PowerShell command (approval required)"/><button onClick={() => void runTerminal()} disabled={terminalBusy || !!terminalPreview || !terminalCommand.trim()}><Play size={14}/> Review & Run</button></div></div>
        : bottom === "Tests" ? <div className="ide-tests-panel">
          <div className="ide-tests-toolbar"><button onClick={()=>void discoverTests()} disabled={testBusy}><RefreshCw size={13}/>{testBusy?"Discovering…":"Discover suites"}</button><label>Agent task<select value={testTaskId} onChange={event=>setTestTaskId(event.target.value)} disabled={!agentSessionId}><option value="">Not linked</option>{agentTasks.map(task=><option key={task.id} value={task.id}>{task.title} · {task.status}</option>)}</select></label><button onClick={()=>void loadTestReport()}>Final report</button></div>
          {!testDiscovery?<div className="ide-tests-empty"><strong>Discover this project’s test tools</strong><span>Reads project manifests and test files. It will not install dependencies.</span><button onClick={()=>void discoverTests()} disabled={testBusy}>Discover tests</button></div>:<div className="ide-tests-layout">
            <section className="ide-test-suites"><header><strong>Discovered suites</strong><small>{testDiscovery.test_files.length} test files</small></header>{testDiscovery.suites.map(suite=><article key={suite.id} className={selectedTestSuiteId===suite.id?"selected":""} onClick={()=>setSelectedTestSuiteId(suite.id)}><div><strong>{suite.label}</strong><small>{suite.test_files.length} related file(s) · {suite.framework}</small></div><button disabled={!suite.available||testBusy||terminalBusy} onClick={event=>{event.stopPropagation();setSelectedTestSuiteId(suite.id);void prepareTestRun(suite);}} title={suite.available?suite.command:suite.missing_reason}><Play size={12}/> Prepare</button>{!suite.available&&<p className="ide-test-missing">{suite.missing_reason} No dependencies were installed.</p>}</article>)}<div className="ide-test-files"><strong>Discovered test files</strong>{testDiscovery.test_files.length?testDiscovery.test_files.map(path=><button key={path} onClick={()=>void openFailureFile(path)}>{path}</button>):<small>No matching test files were found.</small>}</div></section>
            <section className="ide-test-details">
              {testPreview&&<article className="ide-test-approval"><strong>Approve test execution</strong><code>{testPreview.preview.command}</code><small>Why: {testPreview.preview.reason}</small><small>Working directory: {testPreview.preview.working_directory}</small><small>Timeout: {testPreview.preview.timeout_seconds}s · May modify files: {testPreview.preview.may_modify_files?"Yes":"Not detected"} · May use network: {testPreview.preview.may_use_network?"Yes":"Not detected"}</small><p>{testPreview.preview.warning}</p><div><button onClick={()=>void rejectTestRun()} disabled={testBusy}>Reject</button><button onClick={()=>void approveTestRun()} disabled={testBusy}><ShieldCheck size={13}/> Approve and run</button></div></article>}
              {testRuns.length>0&&<div className="ide-test-history"><strong>Run history</strong>{testRuns.map(run=><button key={run.id} className={activeTestRun?.id===run.id?"selected":""} onClick={()=>setActiveTestRun(run)}><span className={`test-state ${((run.result?.status||run.status)==="passed")?"passed":((run.result?.status||run.status)==="failed")?"failed":((run.result?.status||run.status)==="error")?"error":"pending"}`}>{run.result?.status||run.status}</span><span>{run.suite.label}</span><small>{new Date(run.created_at).toLocaleString()}</small></button>)}</div>}
              {activeTestRun&&<article className="ide-test-result"><header><div><strong>{activeTestRun.suite.label}</strong><small>{activeTestRun.status}{activeTestRun.duration_seconds!==undefined?` · ${activeTestRun.duration_seconds?.toFixed(2)}s`:""} · exit {activeTestRun.result?.status||"pending"}</small></div>{activeTestRun.status==="running"&&<button onClick={()=>void cancelTestRun()}><Square size={13}/> Cancel</button>}</header>
                {activeTestRun.result&&<><div className={`ide-test-summary ${activeTestRun.result.status}`}><strong>{activeTestRun.result.status.toUpperCase()} · {activeTestRun.result.category.replace(/_/g," ")}</strong><span>{activeTestRun.result.message}</span><small>Passed {activeTestRun.result.counts.passed??"?"} · Failed {activeTestRun.result.counts.failed??"?"} · Skipped {activeTestRun.result.counts.skipped??"?"}</small></div>
                  {(activeTestRun.result.test_cases||[]).length>0&&<div className="ide-test-cases">{activeTestRun.result.test_cases?.map((test,index)=><div key={`${test.name}-${index}`}><i className={test.status}/><span>{test.name}</span><small>{test.status}</small></div>)}</div>}
                  {activeTestRun.result.location&&<button className="ide-test-location" onClick={()=>void openFailureFile(activeTestRun.result!.location!.path)}>Open {activeTestRun.result.location.path}:{activeTestRun.result.location.line}</button>}
                  {activeTestRun.result.related_context?.map(item=><button className="ide-test-location" key={item.path} onClick={()=>void openFailureFile(item.path)}>Open related source: {item.path}:{item.line_start}-{item.line_end}</button>)}
                  {activeTestRun.result.stack_trace&&<details><summary>Stack trace</summary><pre>{activeTestRun.result.stack_trace}</pre></details>}
                  {activeTestRun.result.output&&<details><summary>Captured output</summary><pre>{activeTestRun.result.output}</pre></details>}
                  {(activeTestRun.result.status==="failed"||(activeTestRun.result.status==="error"&&["syntax_error","import_error","type_error","runtime_exception"].includes(activeTestRun.result.category)))&&<div className="ide-test-actions"><label>Repair attempts<select value={repairLimit} onChange={event=>setRepairLimit(Number(event.target.value))}><option value={1}>1 attempt</option><option value={2}>2 attempts</option><option value={3}>3 attempts</option><option value={4}>4 attempts</option><option value={5}>5 attempts</option></select></label><button disabled={debugFixBusy||!agentSessionId} onClick={()=>void proposeDebugFix()}><Brain size={13}/>{debugFixBusy?"Diagnosing…":"Diagnose and propose fix"}</button><button disabled={!testDiscovery?.suites.some(suite=>suite.id===activeTestRun.suite.id&&suite.available)||testBusy||terminalBusy} onClick={()=>{const suite=testDiscovery?.suites.find(item=>item.id===activeTestRun.suite.id);if(suite)void prepareTestRun(suite,activeTestRun.id);}}>Prepare retest</button></div>}
                </>}
              </article>}
              {testReport&&<article className="ide-test-report"><strong>Testing report · {testReport.run_count} completed runs</strong><span>{testReport.passed_runs} passed · {testReport.passed_with_skips_runs||0} passed with skips · {testReport.failed_runs} failed · {testReport.error_runs||0} execution errors</span><small>{testReport.statement}</small></article>}
            </section>
          </div>}
        </div>
        : bottom === "Debug Console" ? <div className="ide-tool-panel"><div className="ide-tool-row"><span>{debugSession ? "Python debugger active" : "Open a Python file and click Debug"}</span>{debugSession && <><button onClick={() => void sendDebug("continue")}>Continue</button><button onClick={() => void sendDebug("next")}>Step over</button><button onClick={() => void sendDebug("step")}>Step into</button><button onClick={() => void sendDebug("where")}>Call stack</button><button onClick={() => void sendDebug("p locals()")}>Inspect variables</button><button onClick={() => void stopDebug()}><Square size={13}/> Stop</button></>}</div>{debugSession && <div className="ide-tool-row"><input value={debugCommand} onChange={e => setDebugCommand(e.target.value)} onKeyDown={e => { if (e.key === "Enter") { void sendDebug(debugCommand); setDebugCommand(""); } }} placeholder="Pdb command: break 20 / p variable"/><button onClick={() => { void sendDebug(debugCommand); setDebugCommand(""); }}>Send</button></div>}<pre>{debugOutput || "Pdb commands: break 20, next, step, continue, where, p locals()."}</pre></div>
        : bottom === "Agent Logs" ? <div className="ide-agent-logs">{agentLogs.length?agentLogs.map(item=><p key={item.id}><time>{new Date(item.created_at).toLocaleTimeString()}</time><strong>{item.event_type.replace(/_/g," ")}</strong>{item.message}</p>):<span>Agent activity will appear here after opening a session.</span>}</div>
        : bottom === "Command History" ? <div className="ide-terminal-history">{terminalHistory.length?terminalHistory.map((entry,index)=><article key={`${String(entry.execution_id||entry.started_at)}-${index}`}><strong>{String(entry.status||"command")}</strong><code>{String(entry.command||"")}</code><small>{String(entry.started_at||"")} · {String(entry.working_directory||"")}</small></article>):<p>Approved terminal commands will appear here.</p>}</div>
        : bottom === "Output" ? <div className="ide-tool-panel"><pre>{terminalOutput || "No workspace output yet."}</pre></div>
        : <div className="ide-bottom-content">{bottom === "Problems" ? "Test and editor diagnostics will appear here." : "Workspace output will appear here."}</div>)}
      </footer>
      <div className="ide-statusbar"><span>{activeTab?.dirty ? "Modified" : activeTab ? "Saved" : "Ready"}</span><span>{activeTab ? languageFor(activeTab.name) : "Plain Text"}</span><span>UTF-8</span><span>{activeTab?.content.includes("\r\n") ? "CRLF" : "LF"}</span><span>Ln {cursorPosition.line}, Col {cursorPosition.column}</span><span>{agentRunning||terminalStatus==="running" ? "Agent working" : "Agent ready"}</span></div>
    </div>
  );
}
