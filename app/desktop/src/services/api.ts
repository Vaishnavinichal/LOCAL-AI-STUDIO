import type { Conversation, ChatMessage, ConversationCreate, Project, ProjectMessage, ModelInfo } from "../types/chat";

const API_BASE = ((import.meta as ImportMeta & { env?: { VITE_API_BASE_URL?: string } }).env?.VITE_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers }
    });
  } catch (error) {
    if (error instanceof TypeError) throw new Error(`Could not reach the Local AI Studio backend at ${API_BASE} while requesting ${endpoint}. Confirm the backend is running and restart it if it was just updated.`);
    throw error;
  }
  if (!response.ok) {
    let message = `Request failed: ${response.status}`;
    try { const error = await response.json(); message = error.detail || message; } catch { /* fallback */ }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

async function consumeStream(url: string, body: unknown, onToken: (token: string) => void, signal?: AbortSignal) {
  const response = await fetch(`${API_BASE}${url}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal
  });
  if (!response.ok) {
    let message = `Chat request failed: ${response.status}`;
    try { const error = await response.json(); message = error.detail || message; } catch { /* fallback */ }
    throw new Error(message);
  }
  if (!response.body) throw new Error("Streaming is not supported by this response.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "", completed = false;
  const processEvent = (event: string) => {
    for (const line of event.split("\n")) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data) continue;
      if (data === "[DONE]") { completed = true; continue; }
      let parsed: { token?: string; error?: string };
      try { parsed = JSON.parse(data); } catch { continue; }
      if (parsed.error) throw new Error(parsed.error);
      if (parsed.token) onToken(parsed.token);
    }
  };
  try {
    while (!completed) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n"); buffer = events.pop() ?? "";
      for (const event of events) { processEvent(event); if (completed) break; }
    }
    if (!completed && buffer.trim()) processEvent(buffer);
    if (!completed) throw new Error("The AI stream ended before completion.");
  } finally { reader.releaseLock(); }
}

export const chatApi = {
  list: () => request<Conversation[]>("/conversations"),
  trash: () => request<Conversation[]>("/conversations?include_trash=true"),
  search: (query: string) => request<Conversation[]>(`/conversations/search?q=${encodeURIComponent(query)}`),
  create: (data: ConversationCreate = {}) => request<Conversation>("/conversations", { method: "POST", body: JSON.stringify(data) }),
  rename: (id: string, title: string) => request<Conversation>(`/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  pin: (id: string, is_pinned: boolean) => request<Conversation>(`/conversations/${id}/pin`, { method: "PATCH", body: JSON.stringify({ is_pinned }) }),
  delete: (id: string) => request<{ message: string }>(`/conversations/${id}`, { method: "DELETE" }),
  restore: (id: string) => request<{ message: string }>(`/conversations/${id}/restore`, { method: "POST" }),
  permanentlyDelete: (id: string) => request<{ message: string }>(`/conversations/${id}/permanent`, { method: "DELETE" }),
  getMessages: (id: string) => request<ChatMessage[]>(`/conversations/${id}/messages`),
  addMessage: (id: string, role: "user" | "assistant" | "system", content: string) =>
    request<ChatMessage>(`/conversations/${id}/messages`, { method: "POST", body: JSON.stringify({ role, content }) }),
  streamChat: (id: string, content: string, onToken: (token: string) => void, signal?: AbortSignal) =>
    consumeStream(`/conversations/${encodeURIComponent(id)}/chat/stream`, { content, temperature: 0.7, max_tokens: 512 }, onToken, signal),
  regenerate: (id: string, messageId: string, onToken: (token: string) => void, signal?: AbortSignal) =>
    consumeStream(`/conversations/${encodeURIComponent(id)}/messages/${encodeURIComponent(messageId)}/regenerate/stream`, { content: "regenerate", temperature: 0.7, max_tokens: 512 }, onToken, signal),
  getModel: () => request<ModelInfo>("/system/model"),
  setFeedback: (messageId: string, rating: "good" | "bad") =>
    request<ChatMessage>(`/messages/${encodeURIComponent(messageId)}/feedback`, { method: "POST", body: JSON.stringify({ rating }) }),
  clearFeedback: (messageId: string) =>
    request<void>(`/messages/${encodeURIComponent(messageId)}/feedback`, { method: "DELETE" }),
  listProjects: () => request<Project[]>("/projects"),
  createProject: (name: string) => request<Project>("/projects", { method: "POST", body: JSON.stringify({ name }) }),
  addToProject: (messageId: string, projectId: string) =>
    request<{ message: string }>(`/messages/${encodeURIComponent(messageId)}/projects/${encodeURIComponent(projectId)}`, { method: "POST" }),
  getProjectMessages: (projectId: string) =>
    request<ProjectMessage[]>(`/projects/${encodeURIComponent(projectId)}/messages`),
  removeFromProject: (messageId: string, projectId: string) =>
    request<{ message: string }>(`/messages/${encodeURIComponent(messageId)}/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" })
};


// Coding workspace APIs. Folder selection is platform-adapted; writes create server-side backups.
export interface CodingWorkspaceInfo {
  selected: boolean;
  path: string | null;
  name: string | null;
}
export interface CodingWorkspaceEntry {
  name: string;
  relative_path: string;
  type: "directory" | "file";
  size: number | null;
}
export interface CodingWorkspaceFile {
  name: string;
  relative_path: string;
  size: number;
  content: string;
}

export const codingApi: any = {
  selectWorkspace: (path: string) => request<CodingWorkspaceInfo>("/coding/workspace/select", { method: "POST", body: JSON.stringify({ path }) }),
  pickWorkspace: () => request<{ path: string | null }>("/coding/workspace/pick", { method: "POST" }),
  getWorkspace: () => request<CodingWorkspaceInfo>("/coding/workspace"),
  listFiles: (relativePath = "") => request<CodingWorkspaceEntry[]>(`/coding/workspace/files?relative_path=${encodeURIComponent(relativePath)}`),
  readFile: (relativePath: string) => request<CodingWorkspaceFile>(`/coding/workspace/read?relative_path=${encodeURIComponent(relativePath)}`),
  writeFile: (relativePath: string, content: string) => request<{saved:boolean}>("/coding/workspace/write", { method:"PUT", body:JSON.stringify({relative_path:relativePath, content}) }),
  createPath: (relativePath: string, kind: "file"|"folder") => request<{created:boolean}>("/coding/workspace/create", { method:"POST", body:JSON.stringify({relative_path:relativePath, kind}) }),
  renamePath: (relativePath: string, newName: string) => request<{renamed:boolean}>("/coding/workspace/rename", { method:"POST", body:JSON.stringify({relative_path:relativePath, new_name:newName}) }),
  deletePath: (relativePath: string) => request<{deleted:boolean}>("/coding/workspace/delete", { method:"POST", body:JSON.stringify({relative_path:relativePath}) }),
};


export interface CodingFileVersion { id: string; relative_path: string; size: number; created_at: string; reason: string; }
export interface CodingTerminalResult { exit_code: number | null; stdout: string; stderr: string; timed_out: boolean; working_directory?: string; }
export interface CodingTerminalProposal { id: string; command: string; working_directory: string; reason: string; timeout_seconds: number; interactive: boolean; may_modify_files: boolean; may_use_network: boolean; approval_required: boolean; warning: string; session_id?: string | null; }
export interface CodingTerminalExecution { execution_id: string; status: string; command: string; working_directory: string; stdout: string; stderr: string; exit_code: number | null; started_at: string; completed_at: string | null; timed_out: boolean; cancelled: boolean; interactive: boolean; stdin_closed: boolean; }
export interface CodingTestSuite { id: string; label: string; framework: string; kind: string; command: string; working_directory?: string; available: boolean; missing_reason: string; test_files: string[]; }
export interface CodingTestFailure { status: string; category: string; category_message: string; message: string; failed_tests: string[]; failed_test: string; test_cases?:Array<{name:string;status:string}>; counts: {passed?:number|null;failed?:number|null;skipped?:number|null;total?:number|null}; location?: {path: string; line: number} | null; stack_trace: string; output: string; related_context?: Array<{path:string;line_start:number;line_end:number;excerpt:string;symbol?:string|null}>; }
export interface CodingTestRun { id: string; project_root: string; suite: CodingTestSuite; command: string; session_id: string | null; task_id: string | null; status: string; created_at: string; updated_at: string; started_at?: string; completed_at?: string; duration_seconds?: number | null; execution_id?: string | null; result?: CodingTestFailure | null; }
export const codingApiExtended = {
  listVersions: (path: string) => request<CodingFileVersion[]>(`/coding/workspace/versions?relative_path=${encodeURIComponent(path)}`),
  readVersion: (path: string, id: string) => request<CodingWorkspaceFile & { id: string; created_at: string; reason: string }>(`/coding/workspace/versions/${encodeURIComponent(id)}?relative_path=${encodeURIComponent(path)}`),
  restoreVersion: (id: string) => request<{ restored: boolean }>(`/coding/workspace/versions/${encodeURIComponent(id)}/restore`, { method: "POST", body: JSON.stringify({ version_id: id }) }),
  runTerminal: (command: string, relativePath: string, approved: boolean) => request<CodingTerminalResult>("/coding/terminal/run", { method: "POST", body: JSON.stringify({ command, relative_path: relativePath, approved }) }),
  runTests: (command: string, relativePath: string, approved: boolean) => request<CodingTerminalResult>("/coding/tests/run", { method: "POST", body: JSON.stringify({ command, relative_path: relativePath, approved }) }),
  previewTerminal: (command: string, relativePath: string, reason: string, timeout = 30, sessionId?: string, interactive = false) => request<CodingTerminalProposal>("/coding/terminal/preview", { method: "POST", body: JSON.stringify({ command, relative_path: relativePath, reason, timeout, session_id: sessionId, interactive }) }),
  rejectTerminalProposal: (proposalId: string) => request<{ id: string; status: "rejected"; executed: false }>(`/coding/terminal/reject/${encodeURIComponent(proposalId)}`, { method: "POST" }),
  executeTerminal: (proposalId: string) => request<CodingTerminalExecution>(`/coding/terminal/execute/${encodeURIComponent(proposalId)}`, { method: "POST" }),
  terminalExecution: (id: string) => request<CodingTerminalExecution>(`/coding/terminal/executions/${encodeURIComponent(id)}`),
  terminalExecutions: () => request<CodingTerminalExecution[]>("/coding/terminal/executions"),
  sendTerminalInput: (id: string, input: string, eof = false) => request<{ accepted: boolean; eof: boolean; execution: CodingTerminalExecution }>(`/coding/terminal/executions/${encodeURIComponent(id)}/input`, { method: "POST", body: JSON.stringify({ input, eof }) }),
  stopTerminal: (id: string) => request<{ stopping: boolean }>(`/coding/terminal/executions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  terminalHistory: () => request<Array<Record<string, unknown>>>("/coding/terminal/history"),
  discoverTests: () => request<{ project_root: string; discovered_at: string; test_files: string[]; suites: CodingTestSuite[] }>("/coding/tests/discovery"),
  testHistory: () => request<CodingTestRun[]>("/coding/tests/history"),
  testReport: (sessionId?: string) => request<{ project_root: string; run_count: number; passed_runs: number; failed_runs: number; runs: CodingTestRun[]; statement: string }>(`/coding/tests/report${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""}`),
  prepareTest: (suiteId: string, sessionId?: string, taskId?: string, repairOfRunId?: string) => request<{ run: CodingTestRun; preview: CodingTerminalProposal }>("/coding/tests/runs/prepare", { method: "POST", body: JSON.stringify({ suite_id: suiteId, session_id: sessionId || null, task_id: taskId || null, repair_of_run_id: repairOfRunId || null }) }),
  executeTest: (runId: string) => request<CodingTestRun>(`/coding/tests/runs/${encodeURIComponent(runId)}/execute`, { method: "POST" }),
  rejectTest: (runId: string) => request<CodingTestRun>(`/coding/tests/runs/${encodeURIComponent(runId)}/reject`, { method: "POST" }),
  getTestRun: (runId: string) => request<CodingTestRun>(`/coding/tests/runs/${encodeURIComponent(runId)}`),
  cancelTest: (runId: string) => request<{ id: string; status: string }>(`/coding/tests/runs/${encodeURIComponent(runId)}`, { method: "DELETE" }),
  startDebug: (relativePath: string, approved: boolean) => request<{ session_id: string; message: string }>("/coding/debug/start", { method: "POST", body: JSON.stringify({ relative_path: relativePath, approved }) }),
  debugCommand: (id: string, command: string) => request<{ sent: boolean }>(`/coding/debug/${encodeURIComponent(id)}/command`, { method: "POST", body: JSON.stringify({ command }) }),
  debugOutput: (id: string) => request<{ output: string; running: boolean; exit_code: number | null }>(`/coding/debug/${encodeURIComponent(id)}/output`),
  stopDebug: (id: string) => request<{ stopped: boolean }>(`/coding/debug/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

Object.assign(codingApi, codingApiExtended);


export interface CodingModelStatus {
  available: boolean;
  process_running: boolean;
  model_id: string | null;
  model_path: string;
  model_exists: boolean;
  runtime_url: string;
  message: string;
}
export interface CodingAssistantResponse {
  content: string;
  task: string;
  proposal: boolean;
  model: string;
}
export const codingAiApi = {
  modelStatus: () => request<CodingModelStatus>("/coding/model/status"),
  startModel: () => request<{ started: boolean; message: string }>("/coding/model/start", { method: "POST" }),
  stopModel: () => request<{ stopped: boolean; message: string }>("/coding/model/stop", { method: "POST" }),
  ask: (data: { task: string; prompt: string; relative_path: string; code: string; selection?: string }) =>
    request<CodingAssistantResponse>("/coding/assistant", { method: "POST", body: JSON.stringify(data) }),
};

export interface AgentSession { id: string; project_key: string; project_root: string; mode: "ask" | "planning"; status: string; requirement: string; model_id: string | null; created_at: string; updated_at: string; error: string; plan_status?: string | null; title: string; title_manually_edited: boolean; }
export interface AgentTask { id: string; parent_id: string | null; title: string; description: string; status: string; acceptance_criteria: string[]; dependencies: string[]; position: number; }
export interface AgentPlan { objective: string; requirements: string[]; assumptions: string[]; technology_stack: string[]; architecture_overview: string; modules: unknown[]; project_structure?: Array<{path:string;purpose?:string}>; commands?: Array<{command:string;reason?:string}>; tasks: Array<{ id: string; title: string; description?: string; depends_on: string[]; acceptance_criteria: string[] }>; risks: string[]; estimated_complexity: string; verification_command?: string; }
export interface AgentFileChange { id: string; operation: "create" | "modify" | "delete" | "rename"; path: string; new_path: string | null; summary: string; task_ids: string[]; original_content: string | null; content: string | null; diff: string; status: "pending" | "approved" | "rejected" | "applied" | "rolled_back"; }
export interface AgentChangeSet { id: string; session_id: string; status: string; created_at: string; updated_at: string; changes: AgentFileChange[]; events: Array<{ id: string; type: string; message: string; change_id?: string | null; created_at: string }>; applied_change_ids: string[]; snapshot_created_at: string | null; }
export interface ProjectIntelligenceStatus { status: string; files_indexed: number; indexed_at: string | null; summary?: Record<string, unknown> | null; scan?: { embedding?: { status: string; model_id?: string | null; total_embeddings?: number; reason?: string } }; error?: string; exclusions_json?: string; }
export interface ManagedRuntimeStatus { role: "chat"|"coding"; name: string; available: boolean; reachable: boolean; process_running: boolean; managed_by_app: boolean; model_id: string|null; model_path: string; model_exists: boolean; runtime_url: string; message: string; }
export const agentApi = {
  discoverModels: () => request<{ discovered_count: number; models: Array<{ id: string; name: string; runtime: string }>; connections: Array<{ name: string; status?: string; error?: string }> }>("/models/discover", { method: "POST" }),
  sessions: () => request<AgentSession[]>("/agent/sessions"),
  session: (id: string) => request<AgentSession>(`/agent/sessions/${encodeURIComponent(id)}`),
  renameSession: (id: string, title: string) => request<AgentSession>(`/agent/sessions/${encodeURIComponent(id)}/title`, { method: "PATCH", body: JSON.stringify({ title }) }),
  deleteSession: (id: string) => request<{ deleted: boolean }>(`/agent/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  create: (mode: "ask" | "planning", model_id?: string | null) => request<AgentSession>("/agent/sessions", { method: "POST", body: JSON.stringify({ mode, model_id }) }),
  plan: (id: string, requirement: string, model_id?: string | null, signal?: AbortSignal) => request<{ session: AgentSession; plan?: AgentPlan; tasks: AgentTask[]; clarification_questions?: string[] }>(`/agent/sessions/${encodeURIComponent(id)}/plan`, { method: "POST", body: JSON.stringify({ requirement, model_id }), signal }),
  retryPlan: (id: string) => request<{ session: AgentSession; plan?: AgentPlan; tasks: AgentTask[]; clarification_questions?: string[] }>(`/agent/sessions/${encodeURIComponent(id)}/plan/retry`, { method: "POST" }),
  ask: (id: string, requirement: string, model_id?: string | null, signal?: AbortSignal) => request<{ session: AgentSession; answer: string; context: { items: Array<{ path: string; line_start: number; line_end: number; excerpt: string }> } }>(`/agent/sessions/${encodeURIComponent(id)}/ask`, { method: "POST", body: JSON.stringify({ requirement, model_id }), signal }),
  getPlan: (id: string) => request<{ plan: AgentPlan; tasks: AgentTask[]; status: string }>(`/agent/sessions/${encodeURIComponent(id)}/plan`),
  events: (id: string) => request<Array<{ id: string; event_type: string; message: string; created_at: string }>>(`/agent/sessions/${encodeURIComponent(id)}/events`),
  history: (id: string) => request<Array<{ id: string; role: "user"|"assistant"; content: string; model_id: string | null; created_at: string }>>(`/agent/sessions/${encodeURIComponent(id)}/history`),
  approve: (id: string, approved: boolean) => request<{ session: AgentSession; plan_status: string }>(`/agent/sessions/${encodeURIComponent(id)}/approve-plan`, { method: "POST", body: JSON.stringify({ approved }) }),
  generateChanges: (id: string, model_id: string | null, unsaved_paths: string[] = []) => request<AgentChangeSet>(`/agent/sessions/${encodeURIComponent(id)}/changes/generate`, { method: "POST", body: JSON.stringify({ model_id, unsaved_paths }) }),
  changes: (id: string) => request<AgentChangeSet[]>(`/agent/sessions/${encodeURIComponent(id)}/changes`),
  reviewChange: (sessionId: string, changesetId: string, changeId: string, decision: "approved" | "rejected") => request<AgentChangeSet>(`/agent/sessions/${encodeURIComponent(sessionId)}/changes/${encodeURIComponent(changesetId)}/${encodeURIComponent(changeId)}/review`, { method: "POST", body: JSON.stringify({ decision }) }),
  reviewAllChanges: (sessionId: string, changesetId: string, decision: "approved" | "rejected") => request<AgentChangeSet>(`/agent/sessions/${encodeURIComponent(sessionId)}/changes/${encodeURIComponent(changesetId)}/review-all`, { method: "POST", body: JSON.stringify({ decision }) }),
  applyChanges: (sessionId: string, changesetId: string, unsaved_paths: string[] = []) => request<AgentChangeSet>(`/agent/sessions/${encodeURIComponent(sessionId)}/changes/${encodeURIComponent(changesetId)}/apply`, { method: "POST", body: JSON.stringify({ unsaved_paths }) }),
  rollbackChanges: (sessionId: string, changesetId: string, unsaved_paths: string[] = []) => request<AgentChangeSet>(`/agent/sessions/${encodeURIComponent(sessionId)}/changes/${encodeURIComponent(changesetId)}/rollback`, { method: "POST", body: JSON.stringify({ unsaved_paths }) }),
  proposeDebugFix: (sessionId: string, runId: string, taskId?: string, unsaved_paths: string[] = [], max_attempts = 2) => request<{ diagnosis: { root_cause: string; confidence: number; alternatives: string[]; evidence: Array<{path:string;line_start:number;line_end:number;excerpt:string}> }; changeset: AgentChangeSet | null; repair_attempt?: number; repair_attempts_remaining?: number; repair_stopped: boolean; reason?: string }>(`/agent/sessions/${encodeURIComponent(sessionId)}/debug/fix`, { method: "POST", body: JSON.stringify({ run_id: runId, task_id: taskId || null, unsaved_paths, max_attempts }) }),
  control: (id: string, action: "pause" | "resume" | "cancel") => request<AgentSession>(`/agent/sessions/${encodeURIComponent(id)}/${action}`, { method: "POST" }),
  modelRoles: () => request<{ models: Array<{ id: string; name: string; runtime: string; status: string; source_type?: string; endpoint_url?: string; external_model_id?: string; ownership?: string }>; roles: Record<string, string>; benchmarks: Array<{ id: string; model_name: string; created_at: string; results: Record<string, unknown> }> }>("/agent/models/roles"),
  managedRuntimes: () => request<{ chat: ManagedRuntimeStatus; coding: ManagedRuntimeStatus }>("/models/managed-runtimes"),
  startManagedRuntime: (role: "chat"|"coding") => request<{ started: boolean; available: boolean; managed_by_app: boolean; message: string }>(`/models/managed-runtimes/${role}/start`, { method: "POST" }),
  stopManagedRuntime: (role: "chat"|"coding") => request<{ stopped: boolean; message: string }>(`/models/managed-runtimes/${role}/stop`, { method: "POST" }),
  setRole: (role: string, model_id: string | null) => request<{ role: string; model_id: string | null }>("/agent/models/roles", { method: "PUT", body: JSON.stringify({ role, model_id }) }),
  benchmark: (model_id: string) => request<{ id: string; model_name: string; results: Record<string, { status: string; response_ms?: number; tokens_per_second?: number; token_rate_measured?: boolean; error?: string }>; note: string }>("/agent/models/benchmark", { method: "POST", body: JSON.stringify({ model_id }) }),
  projectStatus: () => request<ProjectIntelligenceStatus>("/project-intelligence/status"),
  scanProject: (exclude_paths: string[] = []) => request<{ job_id: string; status: string; completed: number; total: number | null }>("/project-intelligence/scan", { method: "POST", body: JSON.stringify({ exclude_paths }) }),
  scanStatus: (id: string) => request<{ job_id: string; status: string; completed: number; total: number | null; current_file: string; error: string; result?: { status: string; files_indexed: number; symbols_indexed: number; summary: Record<string, unknown>; embedding: { status: string; model_id?: string | null; total_embeddings?: number; reason?: string } } }>(`/project-intelligence/scan/${encodeURIComponent(id)}`),
  cancelScan: (id: string) => request<{ job_id: string; status: string }>(`/project-intelligence/scan/${encodeURIComponent(id)}`, { method: "DELETE" }),
  projectSearch: (q: string, mode: "keyword"|"semantic"|"hybrid"="keyword") => request<{ query: string; results: Array<{ type: string; path: string; name?: string; line_start?: number; excerpt: string; score: number }>; mode: string }>(`/project-intelligence/search?q=${encodeURIComponent(q)}&mode=${mode}`),
  projectSummary: () => request<Record<string, unknown>>("/project-intelligence/summary"),
  projectDependencies: () => request<{ dependencies: Array<{ source_path: string; target: string; kind: string; resolution: string }>; counts: Record<string, number> }>("/project-intelligence/dependencies"),
};


// Phase 7.1 — local document management
export interface LocalDocument {
  id: string;
  filename: string;
  file_type: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  status: string;
  uploaded_at: string;
  page_count?: number | null;
  extracted_chars?: number;
}

export const documentsApi = {
  list: () => request<LocalDocument[]>("/documents"),
  importPath: (path: string) => request<LocalDocument>("/documents/import-path", { method: "POST", body: JSON.stringify({ path }) }),
  getText: (id: string) => request<{ filename: string; text: string; status: string; page_count: number | null }>(`/documents/${encodeURIComponent(id)}/text`),
  runOcr: (id: string) => request<{ status: string; pages_processed: number; extracted_chars: number }>(`/documents/${encodeURIComponent(id)}/ocr`, { method: "POST" }),
  getFile: async (id: string): Promise<Blob> => {
    const response = await fetch(`${API_BASE}/documents/${encodeURIComponent(id)}/file`);
    if (!response.ok) { let message = `Unable to open document: ${response.status}`; try { const error = await response.json(); message = error.detail || message; } catch { /* fallback */ } throw new Error(message); }
    return response.blob();
  },
  upload: async (file: File): Promise<LocalDocument> => {
    const form = new FormData();
    form.append("file", file);
    const response = await fetch(`${API_BASE}/documents/upload`, { method: "POST", body: form });
    if (!response.ok) {
      let message = `Upload failed: ${response.status}`;
      try { const error = await response.json(); message = error.detail || message; } catch { /* fallback */ }
      throw new Error(message);
    }
    return response.json();
  },
  delete: (id: string) => request<{ deleted: boolean; id: string }>(`/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),
};


// Phase 7 — document-grounded study APIs
export interface StudyQuestion { question: string; type: string; difficulty: string; answer: string; explanation: string; page: number | null; options: string[]; }
export interface StudyFlashcard { front: string; back: string; page?: number | null; }
export interface StudyGlossaryItem { term: string; definition: string; page?: number | null; }
export interface StudyMaterial { id: string; material_type: string; title: string; content: string; created_at: string; }
export const documentStudyApi = {
  ask: (id: string, question: string) => request<{ answer: string; sources: number[]; grounded: boolean }>(`/documents/${encodeURIComponent(id)}/ask`, { method: "POST", body: JSON.stringify({ question }) }),
  synopsis: (id: string, style: "short" | "detailed" | "chapter" | "topic") => request<{ synopsis: string; style: string }>(`/documents/${encodeURIComponent(id)}/synopsis`, { method: "POST", body: JSON.stringify({ style }) }),
  questions: (id: string, count: number, difficulty: string, question_type: string) => request<{ questions: StudyQuestion[]; filename: string }>(`/documents/${encodeURIComponent(id)}/questions`, { method: "POST", body: JSON.stringify({ count, difficulty, question_type }) }),
  flashcards: (id: string, count: number) => request<{ flashcards: StudyFlashcard[] }>(`/documents/${encodeURIComponent(id)}/flashcards`, { method: "POST", body: JSON.stringify({ count, difficulty: "mixed", question_type: "mixed" }) }),
  glossary: (id: string) => request<{ glossary: StudyGlossaryItem[] }>(`/documents/${encodeURIComponent(id)}/glossary`, { method: "POST" }),
  conceptMap: (id: string) => request<{ concept_map: string }>(`/documents/${encodeURIComponent(id)}/concept-map`, { method: "POST" }),
  materials: (id: string) => request<StudyMaterial[]>(`/documents/${encodeURIComponent(id)}/materials`),
  saveMaterial: (id: string, material_type: string, title: string, content: string) => request<StudyMaterial>(`/documents/${encodeURIComponent(id)}/materials`, { method: "POST", body: JSON.stringify({ material_type, title, content }) }),
  deleteMaterial: (id: string, materialId: string) => request<{ deleted: boolean }>(`/documents/${encodeURIComponent(id)}/materials/${encodeURIComponent(materialId)}`, { method: "DELETE" }),
};
