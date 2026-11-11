import json
import httpx

from fastapi.responses import StreamingResponse
import os
import sqlite3
import uuid
import subprocess
import queue
import threading
import re
import sys
import time
import asyncio

from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

DATA_ROOT = Path(
    os.environ.get("LOCAL_AI_HOME", r"D:\LocalAIStudio\data")
)

DB_DIR = DATA_ROOT / "database"
DB_PATH = DB_DIR / "local_ai.db"

STORAGE_DIRS = [
    "users", "conversations", "projects",
    "documents", "images", "generated",
    "thumbnails", "indexes"
]

LLAMA_CPP_URL = os.environ.get(
    "LLAMA_CPP_URL",
    "http://127.0.0.1:8081"
)

# Set this to the friendly name of the model currently loaded in llama.cpp.
# Example: $env:LOCAL_AI_MODEL_NAME = "Mistral 7B"
MODEL_NAME = os.environ.get("LOCAL_AI_MODEL_NAME", "Mistral")

# Active coding workspace is held in memory for this app session.
# Phase 6.1 is intentionally read-only; no file-write or command execution routes exist.
ACTIVE_CODING_WORKSPACE: Path | None = None
CODING_IGNORED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", "target", ".next", ".idea", ".vscode"
}
CODING_MAX_FILE_BYTES = 1_000_000
CODING_TERMINAL_TIMEOUT = 120
CODING_TERMINAL_MAX_OUTPUT = 200_000
CODING_DEBUG_SESSIONS = {}
CODING_DEBUG_LOCK = threading.Lock()

# Phase 6.10: separate local coding-model server. Chat and coding models are
# intentionally kept mutually exclusive to protect 16 GB systems from RAM pressure.
CODING_MODEL_PATH = Path(os.environ.get(
    "LOCAL_AI_CODING_MODEL",
    r"D:\LocalAIStudio\models\coding\qwen2.5-coder-3b-instruct-q4_k_m.gguf"
))
LLAMA_SERVER_EXE = Path(os.environ.get(
    "LOCAL_AI_LLAMA_SERVER",
    r"D:\LocalAIStudio\runtime\llama.cpp\llama-server.exe"
))
CODING_LLAMA_URL = os.environ.get("CODING_LLAMA_CPP_URL", "http://127.0.0.1:8082")
CODING_MODEL_PROCESS = None
CODING_MODEL_LOG_HANDLE = None
CODING_MODEL_LOCK = threading.Lock()

def get_time():
    return datetime.now(timezone.utc).isoformat()


def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


# --------------------------------------------------
# DATABASE INITIALIZATION
# --------------------------------------------------

def initialize_storage():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)

    for folder in STORAGE_DIRS:
        (DATA_ROOT / folder).mkdir(parents=True, exist_ok=True)

    with get_connection() as db:

        db.execute("""
            CREATE TABLE IF NOT EXISTS app_metadata (
                id INTEGER PRIMARY KEY,
                app_version TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            INSERT INTO app_metadata (id, app_version)
            VALUES (1, '0.2.0')
            ON CONFLICT(id) DO NOTHING
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New Chat',
                is_pinned INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,

                FOREIGN KEY (conversation_id)
                REFERENCES conversations(id)
                ON DELETE CASCADE
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(conversation_id, created_at)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_updated
            ON conversations(is_deleted, updated_at)
        """)

        # Safe, additive migrations for databases created by earlier versions.
        message_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "model_name" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN model_name TEXT")
        if "feedback" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN feedback TEXT")

        db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS message_projects (
                message_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (message_id, project_id),
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS coding_file_versions (
                id TEXT PRIMARY KEY,
                workspace_path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content BLOB NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT 'save'
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_coding_versions_file
            ON coding_file_versions(workspace_path, relative_path, created_at DESC)
        """)


# --------------------------------------------------
# REQUEST SCHEMAS
# --------------------------------------------------

class ConversationCreate(BaseModel):
    title: str = Field(default="New Chat", min_length=1, max_length=150)


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=150)


class PinUpdate(BaseModel):
    is_pinned: bool


class MessageCreate(BaseModel):
    role: str = Field(default="user")
    content: str = Field(min_length=1, max_length=100000)

class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=2048)


class FeedbackCreate(BaseModel):
    rating: str = Field(pattern="^(good|bad)$")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class CodingWorkspaceSelect(BaseModel):
    path: str = Field(min_length=1, max_length=4096)

# --------------------------------------------------
# FASTAPI APPLICATION
# --------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_storage()
    yield


app = FastAPI(
    title="Local AI Studio",
    version="0.2.0",
    description="Local AI Studio Chat Management API",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
        "http://tauri.localhost"
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


# --------------------------------------------------
# HEALTH AND STORAGE
# --------------------------------------------------

@app.get("/")
def root():
    return {
        "application": "Local AI Studio",
        "status": "operational"
    }


@app.get("/health")
def health():
    return {
        "application": "Local AI Studio",
        "version": "0.2.0",
        "status": "running",
        "database_exists": DB_PATH.exists()
    }


@app.get("/system/storage")
def storage_status():
    return {
        "storage_directory": str(DATA_ROOT),
        "database": str(DB_PATH),
        "database_exists": DB_PATH.exists()
    }


# --------------------------------------------------
# CREATE CONVERSATION
# --------------------------------------------------

@app.post("/conversations")
def create_conversation(data: ConversationCreate):

    conversation_id = str(uuid.uuid4())
    now = get_time()

    with get_connection() as db:
        db.execute("""
            INSERT INTO conversations (
                id, title, is_pinned, is_deleted,
                created_at, updated_at
            )
            VALUES (?, ?, 0, 0, ?, ?)
        """, (
            conversation_id,
            data.title.strip(),
            now,
            now
        ))

        result = db.execute("""
            SELECT * FROM conversations WHERE id = ?
        """, (conversation_id,)).fetchone()

    return dict(result)


# --------------------------------------------------
# LIST CONVERSATIONS
# --------------------------------------------------

@app.get("/conversations")
def list_conversations(
    include_trash: bool = False
):

    with get_connection() as db:

        if include_trash:
            rows = db.execute("""
                SELECT * FROM conversations
                WHERE is_deleted = 1
                ORDER BY deleted_at DESC
            """).fetchall()

        else:
            rows = db.execute("""
                SELECT * FROM conversations
                WHERE is_deleted = 0
                ORDER BY is_pinned DESC, updated_at DESC
            """).fetchall()

    return [dict(row) for row in rows]


# --------------------------------------------------
# SEARCH CONVERSATIONS AND MESSAGES
# --------------------------------------------------

@app.get("/conversations/search")
def search_conversations(q: str = Query(min_length=1)):

    search_term = f"%{q.strip()}%"

    with get_connection() as db:

        rows = db.execute("""
            SELECT DISTINCT c.*
            FROM conversations c
            LEFT JOIN messages m
                ON c.id = m.conversation_id
            WHERE c.is_deleted = 0
              AND (
                c.title LIKE ?
                OR m.content LIKE ?
              )
            ORDER BY c.is_pinned DESC, c.updated_at DESC
        """, (search_term, search_term)).fetchall()

    return [dict(row) for row in rows]


# --------------------------------------------------
# RENAME CONVERSATION
# --------------------------------------------------

@app.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    data: ConversationRename
):

    with get_connection() as db:

        cursor = db.execute("""
            UPDATE conversations
            SET title = ?, updated_at = ?
            WHERE id = ? AND is_deleted = 0
        """, (
            data.title.strip(),
            get_time(),
            conversation_id
        ))

        if cursor.rowcount == 0:
            raise HTTPException(404, "Conversation not found")

        result = db.execute("""
            SELECT * FROM conversations WHERE id = ?
        """, (conversation_id,)).fetchone()

    return dict(result)


# --------------------------------------------------
# PIN / UNPIN CONVERSATION
# --------------------------------------------------

@app.patch("/conversations/{conversation_id}/pin")
def pin_conversation(
    conversation_id: str,
    data: PinUpdate
):

    with get_connection() as db:

        cursor = db.execute("""
            UPDATE conversations
            SET is_pinned = ?, updated_at = ?
            WHERE id = ? AND is_deleted = 0
        """, (
            int(data.is_pinned),
            get_time(),
            conversation_id
        ))

        if cursor.rowcount == 0:
            raise HTTPException(404, "Conversation not found")

        result = db.execute("""
            SELECT * FROM conversations WHERE id = ?
        """, (conversation_id,)).fetchone()

    return dict(result)


# --------------------------------------------------
# MOVE TO TRASH
# --------------------------------------------------

@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):

    with get_connection() as db:

        cursor = db.execute("""
            UPDATE conversations
            SET is_deleted = 1,
                is_pinned = 0,
                deleted_at = ?,
                updated_at = ?
            WHERE id = ? AND is_deleted = 0
        """, (
            get_time(),
            get_time(),
            conversation_id
        ))

        if cursor.rowcount == 0:
            raise HTTPException(404, "Conversation not found")

    return {"message": "Conversation moved to trash"}


# --------------------------------------------------
# RESTORE FROM TRASH
# --------------------------------------------------

@app.post("/conversations/{conversation_id}/restore")
def restore_conversation(conversation_id: str):

    with get_connection() as db:

        cursor = db.execute("""
            UPDATE conversations
            SET is_deleted = 0,
                deleted_at = NULL,
                updated_at = ?
            WHERE id = ? AND is_deleted = 1
        """, (
            get_time(),
            conversation_id
        ))

        if cursor.rowcount == 0:
            raise HTTPException(404, "Deleted conversation not found")

    return {"message": "Conversation restored"}


# --------------------------------------------------
# PERMANENTLY DELETE
# --------------------------------------------------

@app.delete("/conversations/{conversation_id}/permanent")
def permanently_delete_conversation(conversation_id: str):

    with get_connection() as db:

        cursor = db.execute("""
            DELETE FROM conversations
            WHERE id = ? AND is_deleted = 1
        """, (conversation_id,))

        if cursor.rowcount == 0:
            raise HTTPException(404, "Deleted conversation not found")

    return {"message": "Conversation permanently deleted"}


# --------------------------------------------------
# ADD MESSAGE
# --------------------------------------------------

@app.post("/conversations/{conversation_id}/messages")
def add_message(
    conversation_id: str,
    data: MessageCreate
):

    if data.role not in ("user", "assistant", "system"):
        raise HTTPException(400, "Invalid message role")

    content = data.content.strip()

    if not content:
        raise HTTPException(400, "Message cannot be empty")

    message_id = str(uuid.uuid4())
    now = get_time()

    with get_connection() as db:

        conversation = db.execute("""
            SELECT * FROM conversations
            WHERE id = ? AND is_deleted = 0
        """, (conversation_id,)).fetchone()

        if not conversation:
            raise HTTPException(404, "Conversation not found")

        db.execute("""
            INSERT INTO messages (
                id, conversation_id, role, content, created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            message_id,
            conversation_id,
            data.role,
            content,
            now
        ))

        title = conversation["title"]

        if data.role == "user" and title == "New Chat":
            title = content[:50]

            db.execute("""
                UPDATE conversations
                SET title = ?, updated_at = ?
                WHERE id = ?
            """, (title, now, conversation_id))

        else:
            db.execute("""
                UPDATE conversations
                SET updated_at = ?
                WHERE id = ?
            """, (now, conversation_id))

        result = db.execute("""
            SELECT * FROM messages WHERE id = ?
        """, (message_id,)).fetchone()

    return dict(result)


# --------------------------------------------------
# GET CONVERSATION MESSAGES
# --------------------------------------------------

@app.get("/conversations/{conversation_id}/messages")
def get_messages(conversation_id: str):

    with get_connection() as db:

        conversation = db.execute("""
            SELECT id FROM conversations
            WHERE id = ? AND is_deleted = 0
        """, (conversation_id,)).fetchone()

        if not conversation:
            raise HTTPException(404, "Conversation not found")

        rows = db.execute("""
            SELECT * FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC, rowid ASC
        """, (conversation_id,)).fetchall()

    return [dict(row) for row in rows]

# --------------------------------------------------
# AI CHAT - STREAMING LLAMA.CPP
# --------------------------------------------------

@app.post("/conversations/{conversation_id}/chat/stream")
async def stream_chat(
    conversation_id: str,
    data: ChatRequest
):
    user_content = data.content.strip()

    if not user_content:
        raise HTTPException(400, "Message cannot be empty")

    user_message_id = str(uuid.uuid4())
    now = get_time()

    # Save user message and retrieve conversation history
    with get_connection() as db:

        conversation = db.execute("""
            SELECT * FROM conversations
            WHERE id = ? AND is_deleted = 0
        """, (conversation_id,)).fetchone()

        if not conversation:
            raise HTTPException(404, "Conversation not found")

        db.execute("""
            INSERT INTO messages (
                id, conversation_id, role, content, created_at
            )
            VALUES (?, ?, 'user', ?, ?)
        """, (
            user_message_id,
            conversation_id,
            user_content,
            now
        ))

        if conversation["title"] == "New Chat":
            db.execute("""
                UPDATE conversations
                SET title = ?, updated_at = ?
                WHERE id = ?
            """, (
                user_content[:50],
                now,
                conversation_id
            ))
        else:
            db.execute("""
                UPDATE conversations
                SET updated_at = ?
                WHERE id = ?
            """, (now, conversation_id))

        rows = db.execute("""
            SELECT role, content
            FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC, rowid ASC
        """, (conversation_id,)).fetchall()

        messages = [
            {
                "role": row["role"],
                "content": row["content"]
            }
            for row in rows
        ]

    async def generate():

        assistant_content = ""

        payload = {
            "messages": messages,
            "temperature": data.temperature,
            "max_tokens": data.max_tokens,
            "stream": True
        }

        try:
            async with httpx.AsyncClient(timeout=None) as client:

                async with client.stream(
                    "POST",
                    f"{LLAMA_CPP_URL}/v1/chat/completions",
                    json=payload
                ) as response:

                    if response.status_code != 200:
                        error_body = await response.aread()

                        yield (
                            "data: " +
                            json.dumps({
                                "error": error_body.decode(
                                    "utf-8",
                                    errors="replace"
                                )
                            }) +
                            "\n\n"
                        )
                        return

                    async for line in response.aiter_lines():

                        if not line.startswith("data:"):
                            continue

                        chunk = line[5:].strip()

                        if chunk == "[DONE]":
                            break

                        try:
                            parsed = json.loads(chunk)

                            delta = (
                                parsed.get("choices", [{}])[0]
                                .get("delta", {})
                            )

                            token = delta.get("content", "")

                            if token:
                                assistant_content += token

                                yield (
                                    "data: " +
                                    json.dumps({
                                        "token": token
                                    }) +
                                    "\n\n"
                                )

                        except json.JSONDecodeError:
                            continue

            # Save completed assistant response
            if assistant_content.strip():

                assistant_id = str(uuid.uuid4())

                with get_connection() as db:

                    db.execute("""
                        INSERT INTO messages (
                            id, conversation_id,
                            role, content, created_at, model_name
                        )
                        VALUES (?, ?, 'assistant', ?, ?, ?)
                    """, (
                        assistant_id,
                        conversation_id,
                        assistant_content,
                        get_time(),
                        MODEL_NAME
                    ))

                    db.execute("""
                        UPDATE conversations
                        SET updated_at = ?
                        WHERE id = ?
                    """, (
                        get_time(),
                        conversation_id
                    ))

            yield "data: [DONE]\n\n"

        except httpx.RequestError as exc:

            yield (
                "data: " +
                json.dumps({
                    "error": f"Could not connect to llama.cpp: {str(exc)}"
                }) +
                "\n\n"
            )

        except Exception as exc:

            yield (
                "data: " +
                json.dumps({
                    "error": str(exc)
                }) +
                "\n\n"
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

# --------------------------------------------------
# CODING WORKSPACE - PHASE 6.1 (READ-ONLY)
# --------------------------------------------------

def get_active_coding_workspace() -> Path:
    if ACTIVE_CODING_WORKSPACE is None:
        raise HTTPException(400, "Select a coding project folder first")
    root = ACTIVE_CODING_WORKSPACE.resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(404, "The selected coding workspace no longer exists")
    return root


def resolve_workspace_path(relative_path: str) -> Path:
    root = get_active_coding_workspace()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(403, "Access outside the selected workspace is not allowed")
    # Reject symlink traversal, including symlinks that resolve back inside the root.
    current = root
    for part in Path(relative_path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise HTTPException(403, "Parent-directory traversal is not allowed")
        current = current / part
        if current.is_symlink():
            raise HTTPException(403, "Symbolic links are not allowed in the coding workspace")
    return candidate


@app.post("/coding/workspace/select")
def select_coding_workspace(data: CodingWorkspaceSelect):
    global ACTIVE_CODING_WORKSPACE
    raw_path = Path(data.path).expanduser()
    try:
        selected = raw_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(400, "The selected folder could not be accessed")
    if not selected.exists() or not selected.is_dir():
        raise HTTPException(400, "Please select an existing folder")
    ACTIVE_CODING_WORKSPACE = selected
    return {"selected": True, "path": str(selected), "name": selected.name or str(selected)}


@app.get("/coding/workspace")
def coding_workspace_status():
    if ACTIVE_CODING_WORKSPACE is None:
        return {"selected": False, "path": None, "name": None}
    root = ACTIVE_CODING_WORKSPACE
    if not root.exists() or not root.is_dir():
        return {"selected": False, "path": None, "name": None}
    return {"selected": True, "path": str(root), "name": root.name or str(root)}


@app.get("/coding/workspace/files")
def list_coding_workspace_files(relative_path: str = Query(default="", max_length=4096)):
    folder = resolve_workspace_path(relative_path)
    if not folder.exists() or not folder.is_dir():
        raise HTTPException(404, "Folder not found in the selected workspace")
    entries = []
    try:
        children = sorted(folder.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        for item in children:
            if item.is_symlink() or item.name in CODING_IGNORED_DIRS or item.name.startswith("."):
                continue
            try:
                if item.is_dir():
                    entries.append({
                        "name": item.name,
                        "relative_path": item.relative_to(get_active_coding_workspace()).as_posix(),
                        "type": "directory",
                        "size": None
                    })
                elif item.is_file():
                    entries.append({
                        "name": item.name,
                        "relative_path": item.relative_to(get_active_coding_workspace()).as_posix(),
                        "type": "file",
                        "size": item.stat().st_size
                    })
            except OSError:
                continue
            if len(entries) >= 500:
                break
    except OSError:
        raise HTTPException(403, "Unable to list this folder")
    return entries


@app.get("/coding/workspace/read")
def read_coding_workspace_file(relative_path: str = Query(min_length=1, max_length=4096)):
    file_path = resolve_workspace_path(relative_path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File not found in the selected workspace")
    try:
        size = file_path.stat().st_size
        if size > CODING_MAX_FILE_BYTES:
            raise HTTPException(413, "File is larger than the 1 MB preview limit")
        raw = file_path.read_bytes()
    except HTTPException:
        raise
    except OSError:
        raise HTTPException(403, "Unable to read this file")
    if b"\\x00" in raw:
        raise HTTPException(415, "Binary files cannot be previewed as text")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(415, "This file is not valid UTF-8 text")
    return {
        "name": file_path.name,
        "relative_path": relative_path,
        "size": size,
        "content": content
    }


class CodingFileWrite(BaseModel):
    relative_path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=1_000_000)

class CodingPathCreate(BaseModel):
    relative_path: str = Field(min_length=1, max_length=4096)
    kind: str = Field(pattern="^(file|folder)$")

class CodingPathRename(BaseModel):
    relative_path: str = Field(min_length=1, max_length=4096)
    new_name: str = Field(min_length=1, max_length=255)

class CodingPathDelete(BaseModel):
    relative_path: str = Field(min_length=1, max_length=4096)


def validate_new_name(name: str):
    if name in (".", "..") or any(ch in name for ch in ('/', '\\', ':')) or name.strip() != name:
        raise HTTPException(400, "Enter a valid file or folder name")
    if any(ord(ch) < 32 for ch in name):
        raise HTTPException(400, "The name contains invalid characters")


def create_coding_version(target: Path, relative_path: str, reason: str = "save") -> str | None:
    """Persist a pre-change snapshot in the app database, outside the project."""
    if not target.exists() or not target.is_file():
        return None
    raw = target.read_bytes()
    if len(raw) > CODING_MAX_FILE_BYTES:
        raise HTTPException(413, "File exceeds the version-history limit")
    version_id = str(uuid.uuid4())
    with get_connection() as db:
        db.execute("""
            INSERT INTO coding_file_versions
                (id, workspace_path, relative_path, content, size, created_at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (version_id, str(get_active_coding_workspace()), relative_path, raw, len(raw), get_time(), reason))
    return version_id


class CodingVersionRestore(BaseModel):
    version_id: str = Field(min_length=1, max_length=64)

class CodingTerminalRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    relative_path: str = Field(default="", max_length=4096)
    approved: bool = False
    timeout: int = Field(default=30, ge=1, le=CODING_TERMINAL_TIMEOUT)

class CodingDebugStart(BaseModel):
    relative_path: str = Field(min_length=1, max_length=4096)
    approved: bool = False

class CodingDebugCommand(BaseModel):
    command: str = Field(min_length=1, max_length=500)


class CodingAssistantRequest(BaseModel):
    task: str = Field(pattern="^(ask|explain|debug|review|generate|fix|refactor)$")
    prompt: str = Field(min_length=1, max_length=3000)
    relative_path: str = Field(min_length=1, max_length=4096)
    code: str = Field(default="", max_length=8000)
    selection: str = Field(default="", max_length=4000)


def _workspace_cwd(relative_path: str = "") -> Path:
    cwd = resolve_workspace_path(relative_path)
    if not cwd.exists() or not cwd.is_dir():
        raise HTTPException(400, "Terminal working directory must be an existing folder")
    return cwd


@app.put("/coding/workspace/write")
def write_coding_workspace_file(data: CodingFileWrite):
    target = resolve_workspace_path(data.relative_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")
    if target.stat().st_size > CODING_MAX_FILE_BYTES:
        raise HTTPException(413, "File exceeds the 1 MB editing limit")
    if target.suffix.lower() in {".exe", ".dll", ".so", ".bin", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".7z", ".db", ".sqlite"}:
        raise HTTPException(415, "This file type is not supported by the text editor")
    try:
        # Persist a queryable version before every overwrite (including approved diffs).
        create_coding_version(target, data.relative_path, "save")
        # Keep a timestamped raw safety copy outside the selected project as well.
        backup_dir = DATA_ROOT / "coding_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        backup_name = f"{stamp}_{uuid.uuid4().hex}_{target.name}.bak"
        backup_path = backup_dir / backup_name
        backup_path.write_bytes(target.read_bytes())
        target.write_text(data.content, encoding="utf-8", newline="")
    except OSError:
        raise HTTPException(403, "Unable to save the file or create its safety backup")
    return {"saved": True, "relative_path": data.relative_path, "size": target.stat().st_size, "backup_created": True}


@app.post("/coding/workspace/create")
def create_coding_workspace_path(data: CodingPathCreate):
    target = resolve_workspace_path(data.relative_path)
    if target.exists():
        raise HTTPException(409, "A file or folder with this name already exists")
    if target.parent != target.parent.resolve() or not target.parent.exists() or not target.parent.is_dir():
        raise HTTPException(400, "Parent folder does not exist")
    if data.kind == "folder":
        target.mkdir()
    else:
        target.parent.mkdir(parents=False, exist_ok=True)
        target.touch(exist_ok=False)
    return {"created": True, "relative_path": data.relative_path, "kind": data.kind}


@app.post("/coding/workspace/rename")
def rename_coding_workspace_path(data: CodingPathRename):
    source = resolve_workspace_path(data.relative_path)
    validate_new_name(data.new_name)
    if not source.exists():
        raise HTTPException(404, "File or folder not found")
    destination = resolve_workspace_path(str(Path(data.relative_path).parent / data.new_name))
    if destination.exists():
        raise HTTPException(409, "An item with that name already exists")
    try:
        source.rename(destination)
    except OSError:
        raise HTTPException(403, "Unable to rename this item")
    return {"renamed": True, "relative_path": str(Path(data.relative_path).parent / data.new_name)}


@app.post("/coding/workspace/delete")
def delete_coding_workspace_path(data: CodingPathDelete):
    target = resolve_workspace_path(data.relative_path)
    root = get_active_coding_workspace()
    if target == root:
        raise HTTPException(403, "The selected workspace root cannot be deleted")
    if not target.exists():
        raise HTTPException(404, "File or folder not found")
    try:
        if target.is_dir():
            import shutil
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError:
        raise HTTPException(403, "Unable to delete this item")
    return {"deleted": True, "relative_path": data.relative_path}


# --------------------------------------------------
# MODEL INFORMATION
# --------------------------------------------------

@app.get("/system/model")
async def current_model():
    """Return configured model name and, when available, llama.cpp model details."""
    model_id = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{LLAMA_CPP_URL}/v1/models")
            if response.status_code == 200:
                payload = response.json()
                model_items = payload.get("data", [])
                if model_items:
                    model_id = model_items[0].get("id")
    except (httpx.RequestError, ValueError):
        pass

    return {
        "name": MODEL_NAME,
        "model_id": model_id,
        "runtime": "llama.cpp",
        "available": model_id is not None
    }


# --------------------------------------------------
# MESSAGE FEEDBACK
# --------------------------------------------------

@app.post("/messages/{message_id}/feedback")
def save_message_feedback(message_id: str, data: FeedbackCreate):
    with get_connection() as db:
        cursor = db.execute("""
            UPDATE messages
            SET feedback = ?
            WHERE id = ? AND role = 'assistant'
        """, (data.rating, message_id))

        if cursor.rowcount == 0:
            raise HTTPException(404, "Assistant message not found")

        row = db.execute("""
            SELECT id, feedback FROM messages WHERE id = ?
        """, (message_id,)).fetchone()

    return dict(row)


@app.delete("/messages/{message_id}/feedback")
def clear_message_feedback(message_id: str):
    with get_connection() as db:
        cursor = db.execute("""
            UPDATE messages
            SET feedback = NULL
            WHERE id = ? AND role = 'assistant'
        """, (message_id,))

        if cursor.rowcount == 0:
            raise HTTPException(404, "Assistant message not found")

    return {"message": "Feedback cleared"}


# --------------------------------------------------
# PROJECTS
# --------------------------------------------------

@app.post("/projects")
def create_project(data: ProjectCreate):
    project_id = str(uuid.uuid4())
    now = get_time()
    name = data.name.strip()

    if not name:
        raise HTTPException(400, "Project name cannot be empty")

    with get_connection() as db:
        db.execute("""
            INSERT INTO projects (id, name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (project_id, name, data.description.strip(), now, now))
        row = db.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()

    return dict(row)


@app.get("/projects")
def list_projects():
    with get_connection() as db:
        rows = db.execute("""
            SELECT p.*,
                   COUNT(mp.message_id) AS message_count
            FROM projects p
            LEFT JOIN message_projects mp ON mp.project_id = p.id
            GROUP BY p.id
            ORDER BY p.updated_at DESC
        """).fetchall()

    return [dict(row) for row in rows]


@app.post("/messages/{message_id}/projects/{project_id}")
def add_message_to_project(message_id: str, project_id: str):
    now = get_time()

    with get_connection() as db:
        message = db.execute("""
            SELECT id FROM messages
            WHERE id = ? AND role = 'assistant'
        """, (message_id,)).fetchone()

        if not message:
            raise HTTPException(404, "Assistant message not found")

        project = db.execute("""
            SELECT id FROM projects WHERE id = ?
        """, (project_id,)).fetchone()

        if not project:
            raise HTTPException(404, "Project not found")

        db.execute("""
            INSERT OR IGNORE INTO message_projects
                (message_id, project_id, created_at)
            VALUES (?, ?, ?)
        """, (message_id, project_id, now))

        db.execute("""
            UPDATE projects SET updated_at = ? WHERE id = ?
        """, (now, project_id))

    return {
        "message": "Assistant response added to project",
        "message_id": message_id,
        "project_id": project_id
    }


@app.get("/projects/{project_id}/messages")
def get_project_messages(project_id: str):
    with get_connection() as db:
        project = db.execute(
            "SELECT id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()

        if not project:
            raise HTTPException(404, "Project not found")

        rows = db.execute("""
            SELECT m.*
            FROM messages m
            INNER JOIN message_projects mp ON mp.message_id = m.id
            WHERE mp.project_id = ?
            ORDER BY mp.created_at DESC
        """, (project_id,)).fetchall()

    return [dict(row) for row in rows]


@app.delete("/messages/{message_id}/projects/{project_id}")
def remove_message_from_project(message_id: str, project_id: str):
    with get_connection() as db:
        cursor = db.execute("""
            DELETE FROM message_projects
            WHERE message_id = ? AND project_id = ?
        """, (message_id, project_id))

        if cursor.rowcount == 0:
            raise HTTPException(404, "Project association not found")

    return {"message": "Assistant response removed from project"}


# --------------------------------------------------
# REGENERATE AN EXISTING ASSISTANT RESPONSE
# --------------------------------------------------

@app.post(
    "/conversations/{conversation_id}/messages/{assistant_message_id}/regenerate/stream"
)
async def regenerate_assistant_response(
    conversation_id: str,
    assistant_message_id: str,
    data: ChatRequest
):
    """
    Regenerate an existing assistant message using the user prompt immediately
    preceding it. This does not insert another user message or assistant row.
    """
    with get_connection() as db:
        conversation = db.execute("""
            SELECT id FROM conversations
            WHERE id = ? AND is_deleted = 0
        """, (conversation_id,)).fetchone()

        if not conversation:
            raise HTTPException(404, "Conversation not found")

        target = db.execute("""
            SELECT rowid, id FROM messages
            WHERE id = ? AND conversation_id = ? AND role = 'assistant'
        """, (assistant_message_id, conversation_id)).fetchone()

        if not target:
            raise HTTPException(404, "Assistant message not found")

        preceding_user = db.execute("""
            SELECT rowid, id
            FROM messages
            WHERE conversation_id = ? AND role = 'user' AND rowid < ?
            ORDER BY rowid DESC
            LIMIT 1
        """, (conversation_id, target["rowid"])).fetchone()

        if not preceding_user:
            raise HTTPException(400, "No preceding user prompt found")

        history_rows = db.execute("""
            SELECT role, content
            FROM messages
            WHERE conversation_id = ? AND rowid <= ?
              AND role IN ('user', 'assistant', 'system')
            ORDER BY rowid ASC
        """, (conversation_id, preceding_user["rowid"])).fetchall()

        history = [
            {"role": row["role"], "content": row["content"]}
            for row in history_rows
        ]

    async def generate():
        assistant_content = ""
        payload = {
            "messages": history,
            "temperature": data.temperature,
            "max_tokens": data.max_tokens,
            "stream": True
        }

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{LLAMA_CPP_URL}/v1/chat/completions",
                    json=payload
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        yield "data: " + json.dumps({
                            "error": body.decode("utf-8", errors="replace")
                        }) + "\n\n"
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue

                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break

                        try:
                            parsed = json.loads(chunk)
                            delta = parsed.get("choices", [{}])[0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                assistant_content += token
                                yield "data: " + json.dumps({"token": token}) + "\n\n"
                        except (json.JSONDecodeError, IndexError, AttributeError):
                            continue

            if assistant_content.strip():
                with get_connection() as db:
                    db.execute("""
                        UPDATE messages
                        SET content = ?, created_at = ?, model_name = ?, feedback = NULL
                        WHERE id = ? AND conversation_id = ?
                    """, (
                        assistant_content,
                        get_time(),
                        MODEL_NAME,
                        assistant_message_id,
                        conversation_id
                    ))
                    db.execute("""
                        UPDATE conversations SET updated_at = ? WHERE id = ?
                    """, (get_time(), conversation_id))

            yield "data: [DONE]\n\n"

        except httpx.RequestError as exc:
            yield "data: " + json.dumps({
                "error": f"Could not connect to llama.cpp: {str(exc)}"
            }) + "\n\n"
        except Exception as exc:
            yield "data: " + json.dumps({"error": str(exc)}) + "\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# --------------------------------------------------
# CODING MODEL + AI ASSISTANT - PHASE 6.10
# --------------------------------------------------

@app.get("/coding/model/status")
async def coding_model_status():
    process = CODING_MODEL_PROCESS
    running_process = process is not None and process.poll() is None
    available = False
    model_id = None
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{CODING_LLAMA_URL}/health")
            available = response.status_code == 200
            if available:
                models = await client.get(f"{CODING_LLAMA_URL}/v1/models")
                if models.status_code == 200:
                    items = models.json().get("data", [])
                    model_id = items[0].get("id") if items else None
    except (httpx.RequestError, ValueError):
        pass
    return {
        "available": available,
        "process_running": running_process,
        "model_id": model_id,
        "model_path": str(CODING_MODEL_PATH),
        "model_exists": CODING_MODEL_PATH.is_file(),
        "runtime_url": CODING_LLAMA_URL,
        "message": "Coding model is ready" if available else "Coding model is unloaded"
    }


@app.post("/coding/model/start")
async def start_coding_model():
    global CODING_MODEL_PROCESS, CODING_MODEL_LOG_HANDLE
    with CODING_MODEL_LOCK:
        if CODING_MODEL_PROCESS is not None and CODING_MODEL_PROCESS.poll() is None:
            return {"started": True, "message": "Coding model process is already running"}
        if not CODING_MODEL_PATH.is_file():
            raise HTTPException(404, f"Coding model file not found: {CODING_MODEL_PATH}")
        if not LLAMA_SERVER_EXE.is_file():
            raise HTTPException(404, f"llama-server.exe not found: {LLAMA_SERVER_EXE}")
        # Do not silently run two large models on a 16 GB machine.
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                chat_response = await client.get(f"{LLAMA_CPP_URL}/v1/models")
                if chat_response.status_code == 200 and chat_response.json().get("data"):
                    raise HTTPException(409, "Unload/stop the Chat model on port 8081 before starting the Coding model. This avoids loading both models into RAM.")
        except httpx.RequestError:
            pass
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                existing = await client.get(f"{CODING_LLAMA_URL}/health")
                if existing.status_code == 200:
                    return {"started": True, "message": "A coding model server is already available"}
        except httpx.RequestError:
            pass
        log_dir = DATA_ROOT.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "coding-model.log"
        try:
            CODING_MODEL_LOG_HANDLE = open(log_path, "a", encoding="utf-8", errors="replace")
            CODING_MODEL_PROCESS = subprocess.Popen(
                [str(LLAMA_SERVER_EXE), "--model", str(CODING_MODEL_PATH),
                 "--host", "127.0.0.1", "--port", "8082", "--ctx-size", "4096",
                 "--threads", "6", "--threads-batch", "6", "--n-gpu-layers", "0"],
                stdin=subprocess.DEVNULL, stdout=CODING_MODEL_LOG_HANDLE,
                stderr=subprocess.STDOUT, cwd=str(LLAMA_SERVER_EXE.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        except OSError as exc:
            raise HTTPException(500, f"Could not start coding model runtime: {exc}")
    # Wait briefly for model load; keep this endpoint bounded and report log location.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if CODING_MODEL_PROCESS is None or CODING_MODEL_PROCESS.poll() is not None:
            raise HTTPException(500, f"Coding model process exited. Check {log_path}")
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                response = await client.get(f"{CODING_LLAMA_URL}/health")
                if response.status_code == 200:
                    return {"started": True, "message": "Qwen2.5-Coder 3B is ready", "runtime_url": CODING_LLAMA_URL}
        except httpx.RequestError:
            pass
        await asyncio.sleep(1)
    raise HTTPException(504, f"Coding model is still loading. Check runtime log: {log_path}")


@app.post("/coding/model/stop")
def stop_coding_model():
    global CODING_MODEL_PROCESS, CODING_MODEL_LOG_HANDLE
    with CODING_MODEL_LOCK:
        process = CODING_MODEL_PROCESS
        if process is None or process.poll() is not None:
            CODING_MODEL_PROCESS = None
            return {"stopped": True, "message": "Coding model is already unloaded"}
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        CODING_MODEL_PROCESS = None
        if CODING_MODEL_LOG_HANDLE:
            CODING_MODEL_LOG_HANDLE.close()
            CODING_MODEL_LOG_HANDLE = None
    return {"stopped": True, "message": "Coding model unloaded"}


@app.post("/coding/assistant")
async def coding_assistant(data: CodingAssistantRequest):
    resolve_workspace_path(data.relative_path)
    if not data.code.strip():
        raise HTTPException(400, "Open a text file before asking the coding assistant")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=3.0)) as client:
            health = await client.get(f"{CODING_LLAMA_URL}/health")
            if health.status_code != 200:
                raise HTTPException(409, "Load the Qwen2.5-Coder model first using the Model button")
            if data.task in {"generate", "fix", "refactor"}:
                system = (
                    "You are a careful local coding assistant. Return the COMPLETE updated file content only, "
                    "without Markdown fences or explanations. Preserve unrelated behavior and imports. "
                    "Do not claim to have executed or tested code. Never include secrets."
                )
            else:
                system = (
                    "You are a careful local coding assistant. Analyze only the supplied file and request. "
                    "Explain uncertainty, identify likely issues, and provide practical suggestions. "
                    "Do not claim code was executed or changed."
                )
            task_labels = {
                "ask": "Answer the user's question about this code.",
                "explain": "Explain this code clearly, including important functions and flow.",
                "debug": "Find likely bugs and explain fixes; do not change the file.",
                "review": "Review the code for correctness, security, performance and maintainability.",
                "generate": "Modify or generate code according to the request and return the complete updated file.",
                "fix": "Fix the requested bugs and return the complete updated file.",
                "refactor": "Refactor the code as requested and return the complete updated file."
            }
            user_content = (
                f"Task: {task_labels[data.task]}\nFile: {data.relative_path}\n"
                f"User request: {data.prompt}\n"
                + (f"Selected code:\n{data.selection}\n" if data.selection.strip() else "")
                + f"Complete current file content:\n```\n{data.code}\n```"
            )
            response = await client.post(
                f"{CODING_LLAMA_URL}/v1/chat/completions",
                json={"messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user_content}],
                      "temperature": 0.2 if data.task in {"generate", "fix", "refactor"} else 0.4,
                      "max_tokens": 2048, "stream": False}
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            return {"content": content, "task": data.task,
                    "proposal": data.task in {"generate", "fix", "refactor"},
                    "model": "Qwen2.5-Coder 3B Instruct"}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:1000] if exc.response is not None else str(exc)
        raise HTTPException(502, f"Coding model request failed: {detail}")
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Could not connect to coding model: {exc}")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise HTTPException(502, f"Unexpected coding model response: {exc}")


# --------------------------------------------------
# CODING WORKSPACE - PHASES 6.7 / 6.8 / 6.9
# --------------------------------------------------

@app.get("/coding/workspace/versions")
def list_coding_file_versions(relative_path: str = Query(min_length=1, max_length=4096)):
    resolve_workspace_path(relative_path)
    root = str(get_active_coding_workspace())
    with get_connection() as db:
        rows = db.execute("""
            SELECT id, relative_path, size, created_at, reason
            FROM coding_file_versions
            WHERE workspace_path = ? AND relative_path = ?
            ORDER BY created_at DESC
            LIMIT 100
        """, (root, relative_path)).fetchall()
    return [dict(row) for row in rows]


@app.get("/coding/workspace/versions/{version_id}")
def read_coding_file_version(version_id: str, relative_path: str = Query(min_length=1, max_length=4096)):
    resolve_workspace_path(relative_path)
    with get_connection() as db:
        row = db.execute("""
            SELECT id, relative_path, content, size, created_at, reason
            FROM coding_file_versions
            WHERE id = ? AND workspace_path = ? AND relative_path = ?
        """, (version_id, str(get_active_coding_workspace()), relative_path)).fetchone()
    if not row:
        raise HTTPException(404, "File version not found")
    try:
        content = bytes(row["content"]).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(415, "This saved version is not valid UTF-8 text")
    return {"id": row["id"], "relative_path": row["relative_path"], "content": content,
            "size": row["size"], "created_at": row["created_at"], "reason": row["reason"]}


@app.post("/coding/workspace/versions/{version_id}/restore")
def restore_coding_file_version(version_id: str, data: CodingVersionRestore):
    # The URL and body IDs must agree; this prevents accidental stale UI actions.
    if data.version_id != version_id:
        raise HTTPException(400, "Version identifier mismatch")
    row = read_version_row(version_id)
    target = resolve_workspace_path(row["relative_path"])
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "The current file no longer exists; restore is limited to existing files")
    if target.stat().st_size > CODING_MAX_FILE_BYTES:
        raise HTTPException(413, "Current file exceeds the restore limit")
    create_coding_version(target, row["relative_path"], "before-restore")
    try:
        target.write_bytes(bytes(row["content"]))
    except OSError:
        raise HTTPException(403, "Unable to restore this file version")
    return {"restored": True, "relative_path": row["relative_path"], "size": target.stat().st_size}


def read_version_row(version_id: str):
    with get_connection() as db:
        row = db.execute("""
            SELECT * FROM coding_file_versions
            WHERE id = ? AND workspace_path = ?
        """, (version_id, str(get_active_coding_workspace()))).fetchone()
    if not row:
        raise HTTPException(404, "File version not found")
    return row


@app.post("/coding/terminal/run")
def run_coding_terminal(data: CodingTerminalRequest):
    if not data.approved:
        raise HTTPException(403, "Command execution requires explicit approval")
    cwd = _workspace_cwd(data.relative_path)
    command = data.command.strip()
    # Block common destructive/system-wide operations. Project commands still require
    # the user's explicit approval in the UI before this endpoint is called.
    blocked = [r"(?i)\b(format|diskpart|shutdown|restart-computer|stop-computer)\b",
              r"(?i)\b(remove-item|del|erase|rmdir|rd)\b[^\n]*(?:-recurse|/s|/q)",
              r"(?i)\b(reg\s+(?:delete|add)|bcdedit|cipher\s+/w)\b"]
    if any(re.search(pattern, command) for pattern in blocked):
        raise HTTPException(403, "This potentially destructive command is blocked by the IDE safety policy")
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=str(cwd), capture_output=True, text=True, errors="replace",
            timeout=data.timeout, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        stdout = (completed.stdout or "")[-CODING_TERMINAL_MAX_OUTPUT:]
        stderr = (completed.stderr or "")[-CODING_TERMINAL_MAX_OUTPUT:]
        return {"exit_code": completed.returncode, "stdout": stdout, "stderr": stderr,
                "timed_out": False, "working_directory": str(cwd)}
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {"exit_code": None, "stdout": out[-CODING_TERMINAL_MAX_OUTPUT:],
                "stderr": (err + "\nCommand timed out.")[-CODING_TERMINAL_MAX_OUTPUT:],
                "timed_out": True, "working_directory": str(cwd)}
    except OSError as exc:
        raise HTTPException(500, f"Could not start terminal command: {exc}")


@app.post("/coding/tests/run")
def run_coding_tests(data: CodingTerminalRequest):
    if not data.approved:
        raise HTTPException(403, "Test execution requires explicit approval")
    cwd = _workspace_cwd(data.relative_path)
    # Only the supported test entry points are accepted; arbitrary shell text is not.
    command = data.command.strip().lower()
    if command not in ("pytest", "python -m pytest", "python -m unittest", "unittest"):
        raise HTTPException(400, "Choose pytest or unittest; custom commands belong in the approved Terminal")
    args = ([sys.executable, "-m", "pytest", "-q"] if command in ("pytest", "python -m pytest")
            else [sys.executable, "-m", "unittest", "discover", "-v"])
    try:
        completed = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                                   errors="replace", timeout=data.timeout, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"exit_code": completed.returncode, "stdout": (completed.stdout or "")[-CODING_TERMINAL_MAX_OUTPUT:],
                "stderr": (completed.stderr or "")[-CODING_TERMINAL_MAX_OUTPUT:], "timed_out": False}
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": None, "stdout": str(exc.stdout or ""),
                "stderr": "Test run timed out.", "timed_out": True}


@app.post("/coding/debug/start")
def start_coding_debug(data: CodingDebugStart):
    if not data.approved:
        raise HTTPException(403, "Running a debug session requires explicit approval")
    target = resolve_workspace_path(data.relative_path)
    if not target.exists() or not target.is_file() or target.suffix.lower() != ".py":
        raise HTTPException(400, "The debugger currently supports existing Python (.py) files")
    cwd = target.parent
    session_id = str(uuid.uuid4())
    try:
        # A small Pdb wrapper keeps stdin/stdout attached to pipes so the desktop
        # UI can send debugger commands and display output without opening a console.
        debugger_code = (
            "import pdb, runpy, sys; "
            "debugger = pdb.Pdb(stdin=sys.stdin, stdout=sys.stdout); "
            "debugger.runcall(runpy.run_path, sys.argv[1], run_name='__main__')"
        )
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", debugger_code, str(target)], cwd=str(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    except OSError as exc:
        raise HTTPException(500, f"Could not start Python debugger: {exc}")
    output_queue = queue.Queue()
    def pump():
        try:
            for line in process.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(None)
    threading.Thread(target=pump, daemon=True).start()
    with CODING_DEBUG_LOCK:
        CODING_DEBUG_SESSIONS[session_id] = {"process": process, "queue": output_queue,
                                             "relative_path": data.relative_path}
    return {"session_id": session_id, "relative_path": data.relative_path,
            "message": "Python debugger started. Use break <line>, next, step, continue, where, or p locals()."}


@app.post("/coding/debug/{session_id}/command")
def command_coding_debug(session_id: str, data: CodingDebugCommand):
    with CODING_DEBUG_LOCK:
        session = CODING_DEBUG_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(404, "Debug session not found")
    process = session["process"]
    if process.poll() is not None or process.stdin is None:
        raise HTTPException(409, "Debug session has ended")
    command = data.command.strip()
    if not command or len(command) > 500:
        raise HTTPException(400, "Invalid debugger command")
    try:
        process.stdin.write(command + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        raise HTTPException(409, "Debugger input is no longer available")
    return {"sent": True}


@app.get("/coding/debug/{session_id}/output")
def get_coding_debug_output(session_id: str):
    with CODING_DEBUG_LOCK:
        session = CODING_DEBUG_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(404, "Debug session not found")
    lines = []
    ended = False
    q = session["queue"]
    while len(lines) < 500:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if item is None:
            ended = True
            break
        lines.append(item)
    process = session["process"]
    if process.poll() is not None:
        ended = True
    return {"output": "".join(lines), "running": not ended, "exit_code": process.poll()}


@app.delete("/coding/debug/{session_id}")
def stop_coding_debug(session_id: str):
    with CODING_DEBUG_LOCK:
        session = CODING_DEBUG_SESSIONS.pop(session_id, None)
    if not session:
        raise HTTPException(404, "Debug session not found")
    process = session["process"]
    if process.poll() is None:
        try:
            process.stdin.write("q\n")
            process.stdin.flush()
            process.wait(timeout=2)
        except Exception:
            process.kill()
    return {"stopped": True}
