export interface Conversation {
  id: string;
  title: string;
  is_pinned: number;
  is_deleted: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  model_name?: string | null;
  feedback?: "good" | "bad" | null;
}

export interface ConversationCreate { title?: string; }

export interface Project {
  id: string;
  name: string;
  description?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ProjectMessage extends ChatMessage {
  conversation_title?: string;
}

export interface ModelInfo {
  name?: string;
  model_id?: string | null;
  runtime?: string;
  available?: boolean;
}
