import type { Conversation, ChatMessage, ConversationCreate, Project, ProjectMessage, ModelInfo } from "../types/chat";

const API_BASE = "http://127.0.0.1:8000";

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers }
  });
  if (!response.ok) {
    let message = `Request failed: ${response.status}`;
    try { const error = await response.json(); message = error.detail || message; } catch { /* fallback */ }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

async function consumeStream(url: string, body: unknown, onToken: (token: string) => void) {
  const response = await fetch(`${API_BASE}${url}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
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
  streamChat: (id: string, content: string, onToken: (token: string) => void) =>
    consumeStream(`/conversations/${encodeURIComponent(id)}/chat/stream`, { content, temperature: 0.7, max_tokens: 512 }, onToken),
  regenerate: (id: string, messageId: string, onToken: (token: string) => void) =>
    consumeStream(`/conversations/${encodeURIComponent(id)}/messages/${encodeURIComponent(messageId)}/regenerate/stream`, { content: "regenerate", temperature: 0.7, max_tokens: 512 }, onToken),
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


// Coding workspace APIs. Folder selection is initiated by the Tauri dialog; writes create server-side backups.
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
export const codingApiExtended = {
  listVersions: (path: string) => request<CodingFileVersion[]>(`/coding/workspace/versions?relative_path=${encodeURIComponent(path)}`),
  readVersion: (path: string, id: string) => request<CodingWorkspaceFile & { id: string; created_at: string; reason: string }>(`/coding/workspace/versions/${encodeURIComponent(id)}?relative_path=${encodeURIComponent(path)}`),
  restoreVersion: (id: string) => request<{ restored: boolean }>(`/coding/workspace/versions/${encodeURIComponent(id)}/restore`, { method: "POST", body: JSON.stringify({ version_id: id }) }),
  runTerminal: (command: string, relativePath: string, approved: boolean) => request<CodingTerminalResult>("/coding/terminal/run", { method: "POST", body: JSON.stringify({ command, relative_path: relativePath, approved }) }),
  runTests: (command: string, relativePath: string, approved: boolean) => request<CodingTerminalResult>("/coding/tests/run", { method: "POST", body: JSON.stringify({ command, relative_path: relativePath, approved }) }),
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
