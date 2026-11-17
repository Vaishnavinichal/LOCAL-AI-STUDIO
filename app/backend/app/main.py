import json
import httpx

from fastapi.responses import StreamingResponse, FileResponse
import os
import sqlite3
import uuid
import subprocess
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import sys
import shutil
import hashlib
import mimetypes
import time
from html.parser import HTMLParser

from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import pytesseract

# Tesseract OCR configuration
TESSERACT_EXE = os.environ.get(
    "TESSERACT_CMD",
    r"D:\Tesseract\tesseract.exe"
)

pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE

os.environ["TESSDATA_PREFIX"] = os.environ.get(
    "TESSDATA_PREFIX",
    r"D:\Tesseract\tessdata"
)

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
# --------------------------------------------------
# CODING MODEL CONFIGURATION
# --------------------------------------------------

CODING_MODEL_PATH = Path(os.environ.get(
    "LOCAL_AI_CODING_MODEL",
    r"D:\LOCAL-AI-STUDIO\models\coding\qwen2.5-coder-7b.gguf"
))

LLAMA_SERVER_EXE = Path(os.environ.get(
    "LOCAL_AI_LLAMA_SERVER",
    r"D:\LOCAL-AI-STUDIO\runtime\llama.cpp\llama-server.exe"
))

CODING_LLAMA_URL = os.environ.get(
    "CODING_LLAMA_CPP_URL",
    "http://127.0.0.1:8082"
)

CODING_MODEL_NAME = "Qwen2.5-Coder 7B"

CODING_MODEL_PROCESS = None
CODING_MODEL_LOCK = threading.Lock()
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
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                file_type TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'uploaded',
                uploaded_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_documents_uploaded ON documents(uploaded_at DESC)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS document_materials (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                material_type TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_document_materials_doc ON document_materials(document_id, created_at DESC)")
        # SQLite FTS5 provides a persistent local lexical retrieval index for document RAG.
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS document_passages_fts
                      USING fts5(document_id UNINDEXED, page UNINDEXED, content, tokenize='unicode61')""")
        document_columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)").fetchall()}
        for column, definition in {
            "extracted_text": "TEXT NOT NULL DEFAULT ''",
            "extraction_status": "TEXT NOT NULL DEFAULT 'pending'",
            "page_count": "INTEGER",
            "extracted_at": "TEXT",
            "extraction_error": "TEXT"
        }.items():
            if column not in document_columns:
                db.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")

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
        # PHASE 9: LOCAL MODEL MANAGER
        # --------------------------------------------------

        db.execute("""
            CREATE TABLE IF NOT EXISTS model_registry (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                family TEXT NOT NULL DEFAULT 'Unknown',
                version TEXT NOT NULL DEFAULT '',
                format TEXT NOT NULL DEFAULT 'unknown',
                runtime TEXT NOT NULL DEFAULT 'unknown',
                source_type TEXT NOT NULL DEFAULT 'local_file',
                file_path TEXT,
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                status TEXT NOT NULL DEFAULT 'discovered',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_registry_path
            ON model_registry(file_path)
            WHERE file_path IS NOT NULL
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_model_registry_status
            ON model_registry(status, name)
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS model_endpoints (
                id TEXT PRIMARY KEY,
                model_id TEXT,
                name TEXT NOT NULL,
                runtime TEXT NOT NULL,
                endpoint_url TEXT NOT NULL,
                external_model_id TEXT NOT NULL DEFAULT '',
                ownership TEXT NOT NULL DEFAULT 'external',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                last_status TEXT NOT NULL DEFAULT 'unknown',
                last_checked TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(model_id)
                    REFERENCES model_registry(id)
                    ON DELETE SET NULL
            )
        """)

        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_endpoint_url
            ON model_endpoints(endpoint_url, external_model_id)
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS model_capability_assignments (
                capability TEXT PRIMARY KEY,
                model_id TEXT,
                endpoint_id TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(model_id)
                    REFERENCES model_registry(id)
                    ON DELETE SET NULL,
                FOREIGN KEY(endpoint_id)
                    REFERENCES model_endpoints(id)
                    ON DELETE SET NULL
            )
        """)

        db.execute("""CREATE TABLE IF NOT EXISTS model_download_jobs (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, model_name TEXT NOT NULL,
            version TEXT NOT NULL, status TEXT NOT NULL, downloaded_bytes INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER, destination TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")

        # Preserve installation time independently from discovery/last_seen.
        model_columns = {row["name"] for row in db.execute("PRAGMA table_info(model_registry)").fetchall()}
        if "installed_at" not in model_columns:
            db.execute("ALTER TABLE model_registry ADD COLUMN installed_at TEXT")
        if "installed_at_source" not in model_columns:
            db.execute("ALTER TABLE model_registry ADD COLUMN installed_at_source TEXT")
        job_columns = {row["name"] for row in db.execute("PRAGMA table_info(model_download_jobs)").fetchall()}
        if "message" not in job_columns:
            db.execute("ALTER TABLE model_download_jobs ADD COLUMN message TEXT NOT NULL DEFAULT ''")
        if "speed_bytes_per_second" not in job_columns:
            db.execute("ALTER TABLE model_download_jobs ADD COLUMN speed_bytes_per_second INTEGER NOT NULL DEFAULT 0")

# ============================================================
# PHASE 9: LOCAL MODEL MANAGER - MODEL DISCOVERY
# ============================================================

MODEL_DISCOVERY_ENDPOINTS = [
    {
        "name": "Ollama",
        "runtime": "ollama",
        "url": os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
    },
    {
        "name": "llama.cpp Chat",
        "runtime": "llama.cpp",
        "url": os.environ.get("LLAMA_CPP_URL", "http://127.0.0.1:8081").rstrip("/"),
    },
    {
        "name": "llama.cpp Coding",
        "runtime": "llama.cpp",
        "url": os.environ.get("CODING_LLAMA_CPP_URL", "http://127.0.0.1:8082").rstrip("/"),
    },
]


def infer_model_capabilities(name, details):
    """Suggest capabilities only when model naming/metadata provides a clue."""
    lowered = name.lower()
    capabilities = []
    if "embed" in lowered or details.get("embedding_length"):
        capabilities.append("embedding")
    if any(term in lowered for term in ("vl", "vision", "llava", "gemma3")):
        capabilities.append("vision")
    if "coder" in lowered or "code" in lowered:
        capabilities.append("coding")
    if not capabilities and details.get("family"):
        capabilities.append("completion")
    return capabilities


def discover_local_models():
    """Discover models exposed by local Ollama and llama.cpp servers."""

    now = get_time()
    discovered = []
    connections = []

    with get_connection() as db:

        for server in MODEL_DISCOVERY_ENDPOINTS:
            base_url = server["url"]
            runtime = server["runtime"]

            try:
                if runtime == "ollama":
                    response = httpx.get(
                        f"{base_url}/api/tags",
                        timeout=5.0,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    # Ollama's tags endpoint is normally {models: [...]}; accept
                    # the wrapped data/value forms returned by compatible servers too.
                    models = payload.get("models", payload.get("data", payload.get("value", []))) if isinstance(payload, dict) else payload
                    if not isinstance(models, list): models = []
                    seen_ollama_names = set()

                    for model in models:
                        name = model.get("name", "Unknown")
                        details = model.get("details") or {}
                        digest = model.get("digest", "")
                        size = int(model.get("size") or 0)

                        # The full Ollama name includes the exact tag/variant. A
                        # digest is content identity and can change on replacement.
                        canonical_name = name.strip()
                        seen_ollama_names.add(canonical_name.casefold())
                        identity = "ollama:" + canonical_name.casefold()
                        model_id = "ollama-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                        existing = db.execute("SELECT id,installed_at,installed_at_source FROM model_registry WHERE source_type='ollama' AND lower(name)=lower(?) LIMIT 1", (canonical_name,)).fetchone()
                        if existing:
                            model_id = existing["id"]
                        modified_at = model.get("modified_at")
                        installed_at = existing["installed_at"] if existing else modified_at
                        installed_at_source = existing["installed_at_source"] if existing and existing["installed_at"] else ("provider_estimate" if modified_at else None)

                        metadata = {
                            "ollama_name": name,
                            "is_cloud": bool(model.get("remote") or model.get("cloud") or name.casefold().endswith(":cloud")),
                            "digest": digest,
                            "capabilities": model.get("capabilities") or infer_model_capabilities(name, details),
                            "capabilities_inferred": not bool(model.get("capabilities")),
                            "parameter_size": details.get("parameter_size", ""),
                            "quantization": details.get("quantization_level", ""),
                            "context_length": details.get("context_length"),
                            "embedding_length": details.get("embedding_length"),
                        }

                        db.execute("""
                            INSERT INTO model_registry (
                                id, name, family, version, format,
                                runtime, source_type, file_path,
                                file_size_bytes, sha256, status,
                                metadata_json, first_seen, last_seen,
                                created_at, updated_at, installed_at, installed_at_source
                            )
                            VALUES (?, ?, ?, '', ?, ?, 'ollama',
                                    NULL, ?, ?, 'installed', ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET
                                name=excluded.name,
                                family=excluded.family,
                                file_size_bytes=excluded.file_size_bytes,
                                metadata_json=excluded.metadata_json,
                                last_seen=excluded.last_seen,
                                updated_at=excluded.updated_at,
                                status='installed',
                                installed_at=COALESCE(model_registry.installed_at, excluded.installed_at),
                                installed_at_source=COALESCE(model_registry.installed_at_source, excluded.installed_at_source)
                        """, (
                            model_id, name,
                            details.get("family", "Unknown"),
                            details.get("format", "ollama"),
                            runtime, size, digest,
                            json.dumps(metadata),
                            now, now, now, now, installed_at, installed_at_source
                        ))
                        # Coalesce legacy digest-keyed rows for this exact tag.
                        db.execute("DELETE FROM model_registry WHERE source_type='ollama' AND lower(name)=lower(?) AND id<>?", (canonical_name, model_id))

                        discovered.append({
                            "id": model_id,
                            "name": name,
                            "runtime": runtime,
                        })

                    # This provider response is authoritative for Ollama. Remove
                    # stale local registrations; never touch Hugging Face records.
                    registered = db.execute("SELECT id,name,metadata_json FROM model_registry WHERE source_type='ollama' AND status='installed'").fetchall()
                    for stale in registered:
                        if stale["name"].casefold() not in seen_ollama_names:
                            try: registered_meta = json.loads(stale["metadata_json"] or "{}")
                            except (TypeError, json.JSONDecodeError): registered_meta = {}
                            if stale["name"].casefold().endswith(":cloud") or registered_meta.get("is_cloud"):
                                continue
                            db.execute("DELETE FROM model_registry WHERE id=?", (stale["id"],))

                else:
                    response = httpx.get(
                        f"{base_url}/v1/models",
                        timeout=5.0,
                    )
                    response.raise_for_status()
                    payload = response.json()

                    # llama.cpp exposes model metadata in data.
                    models = payload.get("data", payload.get("models", payload.get("value", []))) if isinstance(payload, dict) else payload
                    if not isinstance(models, list): models = []

                    for model in models:
                        external_id = model.get("id", "")
                        meta = model.get("meta") or {}
                        model_name = Path(external_id).name or external_id

                        # Exact identity only; never infer associations from file size.
                        match = db.execute("SELECT id FROM model_registry WHERE lower(name)=?", (external_id.strip().lower(),)).fetchone()
                        linked_model_id = match["id"] if match else None

                        endpoint_id = (
                            "endpoint-" + hashlib.sha256(
                                (base_url + "|" + external_id).encode()
                            ).hexdigest()[:24]
                        )

                        db.execute("""
                            INSERT INTO model_endpoints (
                                id, model_id, name, runtime,
                                endpoint_url, external_model_id,
                                ownership, is_enabled, last_status,
                                last_checked, created_at, updated_at
                            )
                            VALUES (?, ?, ?, 'llama.cpp', ?, ?,
                                    'external', 1, 'online', ?, ?, ?)
                            ON CONFLICT(endpoint_url, external_model_id)
                            DO UPDATE SET
                                last_status='online',
                                last_checked=excluded.last_checked,
                                updated_at=excluded.updated_at
                        """, (
                            endpoint_id, linked_model_id,
                            server["name"], base_url,
                            external_id, now, now, now
                        ))

                        connections.append({
                            "name": server["name"],
                            "url": base_url,
                            "model": model_name,
                            "linked": bool(linked_model_id),
                        })

            except Exception as exc:
                connections.append({
                    "name": server["name"],
                    "url": base_url,
                    "status": "offline",
                    "error": str(exc),
                })

        db.commit()

    unique = {item["id"]: item for item in discovered}
    return {
        "discovered_count": len(unique),
        "models": list(unique.values()),
        "connections": connections,
    }

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

class DocumentImportPath(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


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


@app.post("/models/discover")
def models_discover():
    return discover_local_models()


@app.get("/models")
def models_list():
    with get_connection() as db:
        rows = db.execute("""
            SELECT * FROM model_registry
            ORDER BY name COLLATE NOCASE
        """).fetchall()

    return [dict(row) for row in rows]


@app.delete("/models/{model_id}")
def uninstall_model(model_id: str):
    with get_connection() as db:
        row = db.execute("SELECT * FROM model_registry WHERE id=?", (model_id,)).fetchone()
        if not row: raise HTTPException(status_code=404, detail="Model registration not found.")
        model = dict(row)
        try: model_metadata = json.loads(model.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError): model_metadata = {}
        is_cloud = model["source_type"] == "ollama" and (model["name"].casefold().endswith(":cloud") or bool(model_metadata.get("is_cloud")))
        if model["status"] not in {"installed", "downloaded"}:
            raise HTTPException(status_code=409, detail="This model is not removable through its provider.")
        if model["source_type"] == "ollama":
            name = model["name"]
            try:
                base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
                with httpx.Client(timeout=10) as client:
                    running = client.get(f"{base}/api/ps")
                    running.raise_for_status()
                    running_payload = running.json()
                    running_models = running_payload.get("models", []) if isinstance(running_payload, dict) else []
                    if any(m.get("name") == name for m in running_models):
                        raise HTTPException(status_code=409, detail="This Ollama model is currently loaded. Unload it in Ollama, then retry removal.")
                    deleted = client.request("DELETE", f"{base}/api/delete", json={"name": name})
                    deleted.raise_for_status()
            except HTTPException: raise
            except Exception as exc: raise HTTPException(status_code=502, detail=f"Ollama could not remove {name}: {exc}")
        elif model["source_type"] == "huggingface":
            path_text = model.get("file_path")
            if not path_text: raise HTTPException(status_code=409, detail="No app-managed model file is registered.")
            path = Path(path_text).resolve()
            root = (MODEL_STORAGE_DIR / "huggingface").resolve()
            if root not in path.parents or not path.is_file():
                raise HTTPException(status_code=409, detail="The registered file is outside app-managed Hugging Face storage; it was left untouched.")
            with get_connection() as db:
                active = db.execute("SELECT id FROM model_endpoints WHERE (model_id=? OR lower(external_model_id)=lower(?)) AND last_status='online'", (model_id, str(path))).fetchone()
                if active: raise HTTPException(status_code=409, detail="A runtime endpoint is using this model. Stop using it before removal.")
            for server in MODEL_DISCOVERY_ENDPOINTS:
                if server["runtime"] != "llama.cpp": continue
                try:
                    response = httpx.get(f"{server['url']}/v1/models", timeout=3.0)
                    response.raise_for_status()
                    payload = response.json()
                    for loaded in (payload.get("data", []) if isinstance(payload, dict) else []):
                        external = str(loaded.get("id", ""))
                        try: loaded_path = Path(external).resolve()
                        except OSError: continue
                        if loaded_path == path:
                            raise HTTPException(status_code=409, detail="A llama.cpp runtime is currently using this model. Stop using it before removal.")
                except HTTPException: raise
                except Exception: continue
            try: path.unlink()
            except OSError as exc: raise HTTPException(status_code=500, detail=f"Could not remove the model file: {exc}")
        else:
            raise HTTPException(status_code=409, detail="This provider does not support safe removal.")
        db.execute("DELETE FROM model_registry WHERE id=?", (model_id,))
        return {"deleted": True, "id": model_id, "storage_impact": "Ollama cloud registration removed; cloud model weights were not deleted" if is_cloud else "Ollama removed the local model and managed data" if model["source_type"] == "ollama" else "App-managed GGUF file removed"}


@app.get("/models/endpoints")
def models_endpoints():
    with get_connection() as db:
        rows = db.execute("""
            SELECT * FROM model_endpoints
            ORDER BY name
        """).fetchall()

    return [dict(row) for row in rows]


# Phase 9.5 provider adapters: public catalogs only; installation remains local.
class MarketplaceLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.text = []
        self._href = None
        self._text = []
        self._h2 = False
        self.headings = []

    def handle_starttag(self, tag, attrs):
        if tag == "h2": self._h2 = True
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if data.strip(): self.text.append(data.strip())
        if self._h2 and data.strip(): self.headings.append(data.strip())
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "h2": self._h2 = False
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
            self._href, self._text = None, []


class OllamaTagPageParser(HTMLParser):
    """Capture a complete tag row; Ollama renders size beside, not inside, its link."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.depth = 0
        self.current_hrefs = []
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "div":
            if self.depth == 0 and "group" in (attributes.get("class") or "").split():
                self.depth = 1
                self.current_hrefs, self.current_text = [], []
            elif self.depth:
                self.depth += 1
        elif tag == "a" and self.depth and attributes.get("href"):
            self.current_hrefs.append(attributes["href"])

    def handle_data(self, data):
        if self.depth and data.strip(): self.current_text.append(data.strip())

    def handle_endtag(self, tag):
        if tag != "div" or not self.depth: return
        self.depth -= 1
        if self.depth == 0:
            row_text = " ".join(self.current_text)
            self.rows.extend((href, row_text) for href in self.current_hrefs)
            self.current_hrefs, self.current_text = [], []


OLLAMA_LIBRARY_CACHE = {}
OLLAMA_LIBRARY_CACHE_LOCK = threading.Lock()
OLLAMA_LIBRARY_CACHE_SECONDS = 600
_OLLAMA_SIZE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?\s*(?:KB|MB|GB|TB))(?:\s*[-–]\s*(?P<high>\d+(?:\.\d+)?\s*(?:KB|MB|GB|TB)))?", re.I)


def _ollama_size_bytes(value):
    if not value: return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)\s*", value, re.I)
    if not match: return None
    return int(float(match.group(1)) * (1024 ** {"KB": 1, "MB": 2, "GB": 3, "TB": 4}[match.group(2).upper()]))


def _ollama_library_metadata(model_id, refresh=False):
    """Read sizes, tags and descriptive metadata from Ollama's public model pages."""
    now = time.monotonic()
    with OLLAMA_LIBRARY_CACHE_LOCK:
        cached = OLLAMA_LIBRARY_CACHE.get(model_id)
        if not refresh and cached and cached[0] > now: return cached[1]
    tags_url = f"https://ollama.com/library/{model_id}/tags"
    page_url = f"https://ollama.com/library/{model_id}"
    with _marketplace_http() as client:
        tags_response = client.get(tags_url); tags_response.raise_for_status()
        page_response = client.get(page_url); page_response.raise_for_status()
    tags_page = MarketplaceLinkParser(); tags_page.feed(tags_response.text)
    tag_rows = OllamaTagPageParser(); tag_rows.feed(tags_response.text)
    model_page = MarketplaceLinkParser(); model_page.feed(page_response.text)
    variants = []
    for href, label in (tag_rows.rows or tags_page.links):
        match = re.fullmatch(rf"/library/{re.escape(model_id)}:([A-Za-z0-9_.-]+)", href or "")
        if not match: continue
        tag = f"{model_id}:{match.group(1)}"
        size_match = _OLLAMA_SIZE_RE.search(label)
        display = None
        low_bytes = high_bytes = None
        if size_match:
            low, high = size_match.group("value"), size_match.group("high")
            low_bytes, high_bytes = _ollama_size_bytes(low), _ollama_size_bytes(high) if high else None
            display = low if not high else f"{low}–{high}"
        variants.append({"name": tag, "size": display, "size_bytes": low_bytes if not high_bytes else None,
                         "size_min_bytes": low_bytes, "size_max_bytes": high_bytes or low_bytes,
                         "cloud": match.group(1).casefold().endswith("cloud"), "provider_label": label})
    variants = list({variant["name"]: variant for variant in variants}.values())
    full_text = " ".join(model_page.text + tags_page.text + model_page.headings).strip()
    tags_match = re.search(r"(?<![\w])(?:vision|tools|thinking|cloud|embedding|audio|video|completion|coding)(?:\s+(?:vision|tools|thinking|cloud|embedding|audio|video|completion|coding))*", full_text, re.I)
    capabilities = list(dict.fromkeys(re.findall(r"(?<![\w])(vision|tools|thinking|embedding|audio|video|completion|coding)(?![\w])", tags_match.group(0), re.I))) if tags_match else []
    parameters = None
    param_match = re.search(r"\b(\d+(?:\.\d+)?\s*[BM])\s*(?:total\s*)?(?:parameters?|params)\b", full_text, re.I)
    if not param_match:
        param_match = re.search(r"\b(\d+(?:\.\d+)?\s*[BM])\s+(?:(?:dense|mixture-of-experts|local|language|vision)\s+){0,2}model\b", " ".join(model_page.headings + model_page.text), re.I)
    if param_match:
        parameters = re.sub(r"\s+", "", param_match.group(1)).upper()
    else:
        variant_params = list(dict.fromkeys(re.sub(r"\s+", "", value).upper() for variant in variants
                                             for value in re.findall(r":(\d+(?:\.\d+)?\s*[BM])(?:[-_.]|$)", variant["name"], re.I)))
        if variant_params: parameters = " / ".join(variant_params)
    contexts = re.findall(r"\b([\d,.]+\s*[KMB])\s+(?:token\s+)?context(?:\s+window)?\b", full_text, re.I)
    context_length = contexts[0].replace(" ", "") if contexts else None
    architecture_match = re.search(r"\barchitecture\s*:?\s*([A-Za-z][A-Za-z0-9_.+-]*)", full_text, re.I)
    license_match = re.search(r"\b(?:license|licence)\s*:?\s*([A-Za-z0-9.+-]+)", full_text, re.I)
    cloud = bool(variants) and all(v["cloud"] for v in variants)
    if not variants and re.search(r"\bcloud\b", full_text, re.I): cloud = True
    descriptions = [x for x in model_page.headings if x.strip()]
    result = {"description": descriptions[0] if descriptions else "", "architecture": architecture_match.group(1) if architecture_match else None,
              "license": license_match.group(1) if license_match else None, "variants": variants,
              "versions": [v["name"] for v in variants], "is_cloud": cloud,
              "parameter_count": parameters, "context_length": context_length,
              "capabilities": capabilities, "size_range": None, "refreshed_at": get_time()}
    sizes = [v for v in variants if v["size_min_bytes"] is not None]
    if cloud:
        result["download_size"] = None
    elif sizes:
        mins = [v["size_min_bytes"] for v in sizes]; maxs = [v["size_max_bytes"] for v in sizes]
        result["size_min_bytes"], result["size_max_bytes"] = min(mins), max(maxs)
        result["size_range"] = sizes[0]["size"] if len(sizes) == 1 else None
        result["download_size"] = result["size_range"] or f"{_format_provider_bytes(min(mins))}–{_format_provider_bytes(max(maxs))}"
    else:
        result["download_size"] = None
    with OLLAMA_LIBRARY_CACHE_LOCK:
        OLLAMA_LIBRARY_CACHE[model_id] = (now + OLLAMA_LIBRARY_CACHE_SECONDS, result)
    return result


def _format_provider_bytes(value):
    if value is None: return None
    return f"{value / (1024 ** 3):.1f} GB" if value >= 1024 ** 3 else f"{value / (1024 ** 2):.0f} MB"


def _ollama_model_list(payload):
    if isinstance(payload, list): return payload
    if not isinstance(payload, dict): return []
    for key in ("models", "data", "value", "results"):
        candidate = payload.get(key)
        if isinstance(candidate, list): return candidate
        if isinstance(candidate, dict):
            for nested in ("models", "data", "value"):
                if isinstance(candidate.get(nested), list): return candidate[nested]
    return []


def _marketplace_http():
    return httpx.Client(timeout=httpx.Timeout(20.0, connect=7.0), follow_redirects=True,
                        headers={"User-Agent": "LOCAL-AI-STUDIO/0.9 model-marketplace"})


def _provider_http_error(provider, exc):
    """Map provider failures to stable, actionable API errors without leaking credentials."""
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(status_code=504, detail={"code": "provider_timeout", "message": f"{provider} catalog timed out. Try again."})
    if isinstance(exc, httpx.ConnectError):
        return HTTPException(status_code=503, detail={"code": "provider_unavailable", "message": f"Cannot reach {provider}. Check your internet connection."})
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 404:
            return HTTPException(status_code=502, detail={"code": "provider_endpoint_not_found", "message": f"The {provider} catalog endpoint returned 404."})
        if status == 429:
            return HTTPException(status_code=429, detail={"code": "provider_rate_limited", "message": f"{provider} is rate limiting catalog requests. Wait and retry."})
        if status >= 500:
            return HTTPException(status_code=502, detail={"code": "provider_server_error", "message": f"{provider} catalog returned HTTP {status}. Try again later."})
        return HTTPException(status_code=502, detail={"code": "provider_request_failed", "message": f"{provider} rejected the catalog request (HTTP {status})."})
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return HTTPException(status_code=502, detail={"code": "invalid_provider_response", "message": f"{provider} returned data in an unexpected format."})
    return HTTPException(status_code=502, detail={"code": "provider_error", "message": f"{provider} catalog is temporarily unavailable."})


@app.get("/marketplace/search")
def marketplace_search(provider: str = "huggingface", q: str = Query(default="", max_length=200), task: str = Query(default="", max_length=100), limit: int = 24, refresh: bool = False):
    limit = max(1, min(limit, 50))
    try:
        with _marketplace_http() as client:
            if provider == "huggingface":
                params = {"search": q, "limit": limit, "sort": "downloads", "direction": "-1"}
                if task:
                    params["pipeline_tag"] = task
                response = client.get("https://huggingface.co/api/models", params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
                    raise ValueError("Hugging Face returned an invalid model list.")
                results = []
                for item in payload:
                    card = item.get("cardData") or {}
                    tags = item.get("tags") or []
                    results.append({"id": item.get("id"), "provider": "Hugging Face",
                        "name": (item.get("id") or "").split("/")[-1], "author": (item.get("id") or "").split("/")[0],
                        "description": card.get("description") or card.get("pretty_name") or "", "architecture": card.get("model_type") or item.get("config", {}).get("model_type") or "",
                        "tasks": [item.get("pipeline_tag")] if item.get("pipeline_tag") else [], "license": card.get("license") or "Unspecified",
                        "downloads": item.get("downloads"), "updated": item.get("lastModified"), "url": "https://huggingface.co/" + item.get("id", ""),
                        "format": "GGUF" if "gguf" in tags else "Transformers", "installable": "gguf" in tags})
                return {"provider": provider, "results": results}
            if provider == "ollama":
                response = client.get("https://ollama.com/search", params={"q": q})
                response.raise_for_status()
                if "<" not in response.text:
                    raise ValueError("Ollama returned a non-HTML catalog response.")
                parser = MarketplaceLinkParser(); parser.feed(response.text)
                results, seen = [], set()
                for href, title in parser.links:
                    match = re.fullmatch(r"/library/([a-zA-Z0-9_.-]+)", href or "")
                    if not match or match.group(1) in seen:
                        continue
                    name = match.group(1); seen.add(name)
                    results.append({"id": name, "provider": "Ollama Library", "name": name, "author": "Ollama",
                        "description": title if title.lower() != name.lower() else "", "tasks": [], "license": "See provider page",
                        "url": "https://ollama.com/library/" + name, "format": "Ollama", "installable": True,
                        "versions": ["latest"]})
                    if len(results) >= limit: break
                def enrich(index, item):
                    try:
                        metadata = _ollama_library_metadata(item["id"], refresh=refresh)
                        item.update(metadata)
                        item["description"] = metadata.get("description") or item["description"]
                        item["tasks"] = metadata.get("capabilities") or []
                        item["installable"] = not metadata.get("is_cloud")
                    except Exception:
                        item.update({"metadata_available": False, "download_size": None, "is_cloud": False})
                    return index, item
                with ThreadPoolExecutor(max_workers=min(6, max(1, len(results)))) as pool:
                    futures = [pool.submit(enrich, index, item) for index, item in enumerate(results)]
                    enriched = [future.result() for future in as_completed(futures)]
                    results = [item for _, item in sorted(enriched, key=lambda pair: pair[0])]
                return {"provider": provider, "results": results}
            raise HTTPException(status_code=400, detail="Provider must be ollama or huggingface.")
    except HTTPException:
        raise
    except Exception as exc:
        raise _provider_http_error(provider, exc) from exc


@app.get("/marketplace/details")
def marketplace_details(provider: str, model_id: str, refresh: bool = False):
    if provider == "huggingface":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model_id):
            raise HTTPException(status_code=400, detail="Invalid Hugging Face repository identifier.")
        try:
            with _marketplace_http() as client:
                response = client.get(f"https://huggingface.co/api/models/{model_id}", params={"blobs": "true"})
                response.raise_for_status(); item = response.json()
                if not isinstance(item, dict): raise ValueError("Hugging Face returned invalid repository details.")
                card = item.get("cardData") or {}
                files = [{"name": f.get("rfilename"), "size": (f.get("lfs") or {}).get("size") or f.get("size"), "sha256": (f.get("lfs") or {}).get("oid")}
                         for f in item.get("siblings", []) if f.get("rfilename", "").lower().endswith(".gguf")]
                return {"id": model_id, "provider": "Hugging Face", "revision": item.get("sha") or "main", "license": card.get("license") or "Unspecified",
                    "license_url": card.get("license_link"), "description": card.get("description") or "", "architecture": card.get("model_type") or "Unknown",
                    "tasks": [item.get("pipeline_tag")] if item.get("pipeline_tag") else [], "context_length": card.get("context_length"), "files": files,
                    "url": f"https://huggingface.co/{model_id}", "installable": bool(files), "runtime": "llama.cpp (GGUF only)" if files else None}
        except Exception as exc:
            raise _provider_http_error("Hugging Face", exc) from exc
    if provider == "ollama":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", model_id):
            raise HTTPException(status_code=400, detail="Invalid Ollama model name.")
        try:
            metadata = _ollama_library_metadata(model_id, refresh=refresh)
        except Exception as exc:
            raise _provider_http_error("Ollama Library", exc) from exc
        return {"id": model_id, "provider": "Ollama Library", "url": f"https://ollama.com/library/{model_id}",
                "license": "See provider page", "runtime": "Ollama", "installable": not metadata.get("is_cloud"),
                **metadata, "note": "Variant sizes and tags are sourced from the Ollama Library."}
    raise HTTPException(status_code=400, detail="Unsupported provider.")


@app.get("/marketplace/versions")
def marketplace_versions(provider: str, model_id: str):
    detail = marketplace_details(provider=provider, model_id=model_id)
    return {"provider": provider, "model_id": model_id,
            "versions": detail.get("versions", []), "files": detail.get("files", []),
            "revision": detail.get("revision")}


@app.get("/marketplace/providers")
def marketplace_providers():
    result = []
    with _marketplace_http() as client:
        for key, url in (("ollama", "https://ollama.com/search?q=qwen"),
                         ("huggingface", "https://huggingface.co/api/models?limit=1")):
            try:
                response = client.get(url); response.raise_for_status()
                result.append({"id": key, "status": "online"})
            except Exception as exc:
                err = _provider_http_error("Ollama Library" if key == "ollama" else "Hugging Face", exc)
                result.append({"id": key, "status": "unavailable", "error": err.detail})
    return {"providers": result}


class MarketplaceInstall(BaseModel):
    provider: str
    model_id: str = Field(min_length=1, max_length=250)
    version: str = Field(min_length=1, max_length=500)
    runtime: str
    filename: str | None = None
    revision: str | None = None


MARKETPLACE_JOBS = {}
MARKETPLACE_JOB_CANCEL = {}
MARKETPLACE_JOB_KEYS = {}
MARKETPLACE_ACTIVE_INSTALLS = {}
MARKETPLACE_JOB_LOCK = threading.Lock()
MODEL_STORAGE_DIR = Path(os.environ.get("LOCAL_AI_MODEL_DIR", str(DATA_ROOT / "models"))).expanduser()


def _update_market_job(job_id, **updates):
    with MARKETPLACE_JOB_LOCK:
        MARKETPLACE_JOBS[job_id].update(updates)
        MARKETPLACE_JOBS[job_id]["updated_at"] = get_time()
        snapshot = dict(MARKETPLACE_JOBS[job_id])
        if snapshot.get("status") in {"completed", "failed", "cancelled"}:
            key = MARKETPLACE_JOB_KEYS.pop(job_id, None)
            if key: MARKETPLACE_ACTIVE_INSTALLS.pop(key, None)
    try:
        with get_connection() as db:
            db.execute("UPDATE model_download_jobs SET status=?,downloaded_bytes=?,total_bytes=?,destination=?,error=?,message=?,speed_bytes_per_second=?,updated_at=? WHERE id=?",
                       (snapshot.get("status", "queued"), snapshot.get("downloaded_bytes", 0), snapshot.get("total_bytes"),
                        snapshot.get("destination", ""), snapshot.get("error", ""), snapshot.get("message", ""),
                        snapshot.get("speed_bytes_per_second", 0), snapshot["updated_at"], job_id))
    except sqlite3.Error:
        pass


def _run_marketplace_install(job_id, request_data):
    provider, model_id, version, runtime = (request_data.provider, request_data.model_id,
                                            request_data.version, request_data.runtime)
    try:
        if provider == "ollama":
            if runtime != "ollama": raise ValueError("Ollama catalog models must be installed with Ollama.")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", model_id): raise ValueError("Invalid Ollama model name.")
            local = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
            if ":" in version and not version.startswith(model_id + ":"):
                raise ValueError("The selected tag must belong to the selected Ollama model.")
            exact_tag = version if ":" in version else f"{model_id}:{version}"
            with _marketplace_http() as client:
                tags = client.get(f"{local}/api/tags"); tags.raise_for_status()
                existing_tag = next((m for m in _ollama_model_list(tags.json()) if m.get("name", m.get("model")) == exact_tag), None)
                if existing_tag:
                    _update_market_job(job_id, status="verifying", downloaded_bytes=int(existing_tag.get("size") or 0), total_bytes=int(existing_tag.get("size") or 0), message="Already present in Ollama; verifying registry")
                else:
                    _update_market_job(job_id, status="downloading")
                    layer_progress = {}
                    with client.stream("POST", f"{local}/api/pull", json={"name": exact_tag, "stream": True}, timeout=None) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if MARKETPLACE_JOB_CANCEL[job_id].is_set(): raise InterruptedError("Cancelled by user")
                            if not line: continue
                            progress = json.loads(line)
                            layer = str(progress.get("digest") or progress.get("status") or "transfer")
                            layer_progress[layer] = (int(progress.get("completed") or 0), int(progress.get("total") or 0))
                            done = sum(value[0] for value in layer_progress.values())
                            total = sum(value[1] for value in layer_progress.values())
                            _update_market_job(job_id, downloaded_bytes=done, total_bytes=total or None,
                                               message=progress.get("status", "Pulling model"),
                                               status="verifying" if progress.get("status") == "success" else "downloading")
                verify = client.get(f"{local}/api/tags"); verify.raise_for_status()
                installed = next((m for m in _ollama_model_list(verify.json()) if m.get("name", m.get("model")) == exact_tag), None)
                if not installed: raise RuntimeError("Ollama pull finished but the model is not present in its model list.")
                now = get_time(); digest = installed.get("digest", ""); mid = "ollama-" + hashlib.sha256(("ollama:" + exact_tag.casefold()).encode()).hexdigest()[:24]
                meta = {"ollama_name": exact_tag, "digest": digest, "is_cloud": exact_tag.casefold().endswith(":cloud"), "capabilities": infer_model_capabilities(exact_tag, installed.get("details") or {}), "capabilities_inferred": True}
                with get_connection() as db:
                    d = installed.get("details") or {}
                    db.execute("""INSERT INTO model_registry(id,name,family,version,format,runtime,source_type,file_path,file_size_bytes,sha256,status,metadata_json,first_seen,last_seen,created_at,updated_at,installed_at,installed_at_source)
                    VALUES(?,?,?,?,?,'ollama','ollama',NULL,?,?, 'installed',?,?,?,?,?,?, 'app_recorded') ON CONFLICT(id) DO UPDATE SET name=excluded.name,file_size_bytes=excluded.file_size_bytes,sha256=excluded.sha256,status='installed',metadata_json=excluded.metadata_json,last_seen=excluded.last_seen,updated_at=excluded.updated_at,installed_at=COALESCE(model_registry.installed_at,excluded.installed_at),installed_at_source=COALESCE(model_registry.installed_at_source,excluded.installed_at_source)""",
                    (mid, exact_tag, d.get("family", "Unknown"), version, d.get("format", "ollama"), int(installed.get("size") or 0), digest, json.dumps(meta), now, now, now, now, now))
            verified_size = int(installed.get("size") or 0)
            _update_market_job(job_id, status="completed", downloaded_bytes=verified_size, total_bytes=verified_size or None,
                               is_cloud_registration=exact_tag.casefold().endswith(":cloud"),
                               message="Cloud registration verified" if exact_tag.casefold().endswith(":cloud") else "Installed and verified")
            return
        if provider != "huggingface" or runtime != "llama.cpp":
            raise ValueError("Hugging Face installation currently supports GGUF artifacts for llama.cpp only.")
        if not request_data.filename or Path(request_data.filename).name != request_data.filename or not request_data.filename.lower().endswith(".gguf"):
            raise ValueError("Select one GGUF file from the repository. Other formats are not installable through this adapter.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model_id): raise ValueError("Invalid repository identifier.")
        revision = request_data.revision or "main"
        if not re.fullmatch(r"[A-Za-z0-9._-]+", revision): raise ValueError("Invalid repository revision.")
        url = f"https://huggingface.co/{model_id}/resolve/{revision}/{request_data.filename}"
        sha = hashlib.sha256(); count = 0; started = time.monotonic()
        with _marketplace_http() as client:
            details_response = client.get(f"https://huggingface.co/api/models/{model_id}", params={"blobs": "true", "revision": revision})
            details_response.raise_for_status()
            repo_details = details_response.json()
            repo_revision = repo_details.get("sha")
            expected_digest = None
            expected_artifact_size = None
            for sibling in repo_details.get("siblings", []):
                if sibling.get("rfilename") == request_data.filename:
                    lfs = sibling.get("lfs") or {}
                    raw_size = lfs.get("size") or sibling.get("size")
                    if isinstance(raw_size, int) and raw_size > 0: expected_artifact_size = raw_size
                    oid = lfs.get("oid", "")
                    if re.fullmatch(r"[a-fA-F0-9]{64}", oid): expected_digest = oid.lower()
                    break
            if revision == "main" and repo_revision:
                revision = repo_revision
                url = f"https://huggingface.co/{model_id}/resolve/{revision}/{request_data.filename}"
            with get_connection() as db:
                existing = db.execute("SELECT id FROM model_registry WHERE json_extract(metadata_json,'$.repository')=? AND json_extract(metadata_json,'$.revision')=? AND json_extract(metadata_json,'$.filename')=? AND status IN ('downloaded','installed') LIMIT 1",
                                      (model_id, revision, request_data.filename)).fetchone()
            if existing:
                _update_market_job(job_id, status="completed", downloaded_bytes=0, total_bytes=0, message="This exact repository revision and artifact are already registered")
                return
            MODEL_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            revision_dir = re.sub(r"[^A-Za-z0-9._-]", "_", revision)
            destination_dir = (MODEL_STORAGE_DIR / "huggingface" / model_id.replace("/", "__") / revision_dir).resolve()
            root = (MODEL_STORAGE_DIR / "huggingface").resolve()
            if root not in destination_dir.parents: raise ValueError("Unsafe model destination.")
            destination_dir.mkdir(parents=True, exist_ok=True)
            final_path = (destination_dir / request_data.filename).resolve()
            if destination_dir not in final_path.parents: raise ValueError("Unsafe model filename.")
            if final_path.exists():
                raise FileExistsError("A file already exists at this exact repository revision destination. It was left untouched.")
            partial_path = final_path.with_suffix(final_path.suffix + ".partial")
            with client.stream("GET", url, timeout=None) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or expected_artifact_size or 0)
                free = shutil.disk_usage(MODEL_STORAGE_DIR).free
                if total and total > free:
                    raise ValueError(f"Not enough free disk space: need {total} bytes, have {free} bytes.")
                _update_market_job(job_id, status="downloading", destination=str(final_path), total_bytes=total)
                with open(partial_path, "wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if MARKETPLACE_JOB_CANCEL[job_id].is_set(): raise InterruptedError("Cancelled by user")
                        output.write(chunk); sha.update(chunk); count += len(chunk)
                        _update_market_job(job_id, downloaded_bytes=count, total_bytes=total,
                                           speed_bytes_per_second=int(count / max(time.monotonic() - started, .1)))
        _update_market_job(job_id, status="verifying")
        actual_digest = sha.hexdigest()
        if expected_digest and actual_digest != expected_digest:
            partial_path.unlink(missing_ok=True)
            raise ValueError("Downloaded file checksum does not match the provider's published SHA-256.")
        os.replace(partial_path, final_path)
        verified_size = final_path.stat().st_size
        if verified_size != count:
            raise RuntimeError("Downloaded file size changed during verification; registry was not updated.")
        revision_hash = revision if re.fullmatch(r"[a-f0-9]{40}", revision) else None
        now = get_time(); digest = sha.hexdigest(); mid = "hf-" + hashlib.sha256(f"{model_id}@{revision}/{request_data.filename}".encode()).hexdigest()[:24]
        meta = {"repository": model_id, "revision": revision, "filename": request_data.filename, "verification": "sha256-computed; provider checksum not independently confirmed"}
        with get_connection() as db:
            db.execute("""INSERT INTO model_registry(id,name,family,version,format,runtime,source_type,file_path,file_size_bytes,sha256,status,metadata_json,first_seen,last_seen,created_at,updated_at,installed_at,installed_at_source)
            VALUES(?,?,? ,?,'GGUF','llama.cpp','huggingface',?,?,?,'downloaded',?,?,?,?,?,?, 'app_recorded') ON CONFLICT(id) DO UPDATE SET file_path=excluded.file_path,file_size_bytes=excluded.file_size_bytes,sha256=excluded.sha256,status='downloaded',metadata_json=excluded.metadata_json,last_seen=excluded.last_seen,updated_at=excluded.updated_at,installed_at=COALESCE(model_registry.installed_at,excluded.installed_at),installed_at_source=COALESCE(model_registry.installed_at_source,excluded.installed_at_source)""",
            (mid, Path(model_id).name, model_id.split('/')[0], revision_hash or revision, str(final_path), verified_size, digest, json.dumps(meta), now, now, now, now, now))
        _update_market_job(job_id, status="completed", downloaded_bytes=verified_size, total_bytes=verified_size, checksum=digest, verification="provider SHA-256 verified" if expected_digest else "SHA-256 computed; provider checksum unavailable", revision=revision, message="Artifact downloaded, verified, and registered; runtime loading must be configured separately")
    except InterruptedError as exc:
        _update_market_job(job_id, status="cancelled", error=str(exc))
    except Exception as exc:
        _update_market_job(job_id, status="failed", error=str(exc)[:500])


@app.post("/marketplace/install", status_code=202)
def marketplace_install(data: MarketplaceInstall):
    job_id = str(uuid.uuid4()); now = get_time()
    if data.provider not in {"ollama", "huggingface"}:
        raise HTTPException(status_code=400, detail="Provider must be ollama or huggingface.")
    if data.provider == "ollama" and (data.version.casefold().endswith(":cloud") or data.version.casefold().endswith("-cloud")):
        raise HTTPException(status_code=409, detail="This Ollama variant is cloud-hosted and has no local model weights to download.")
    if data.provider == "ollama":
        try:
            metadata = _ollama_library_metadata(data.model_id)
            selected = next((variant for variant in metadata.get("variants", []) if variant["name"] == data.version), None)
            if metadata.get("is_cloud") or (selected and selected.get("cloud")):
                raise HTTPException(status_code=409, detail="This Ollama variant is cloud-hosted and has no local model weights to download.")
        except HTTPException: raise
        except Exception:
            # A missing public metadata page must not turn an unknown size into a
            # cloud classification. The exact requested local tag is still verified
            # against the Ollama runtime after pull.
            pass
    install_key = "|".join((data.provider, data.model_id.casefold(), data.version.casefold(), (data.filename or "").casefold()))
    record = {"id": job_id, "provider": data.provider, "model_name": data.model_id, "version": data.version, "status": "queued", "downloaded_bytes": 0, "total_bytes": None, "destination": "Ollama-managed" if data.provider == "ollama" else str(MODEL_STORAGE_DIR), "error": "", "message": "Queued", "created_at": now, "updated_at": now}
    with MARKETPLACE_JOB_LOCK:
        active_id = MARKETPLACE_ACTIVE_INSTALLS.get(install_key)
        if active_id:
            return {**MARKETPLACE_JOBS[active_id], "already_active": True}
        MARKETPLACE_JOBS[job_id] = record; MARKETPLACE_JOB_CANCEL[job_id] = threading.Event()
        MARKETPLACE_JOB_KEYS[job_id] = install_key
        MARKETPLACE_ACTIVE_INSTALLS[install_key] = job_id
    with get_connection() as db:
        db.execute("INSERT INTO model_download_jobs(id,provider,model_name,version,status,destination,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, data.provider, data.model_id, data.version, "queued", record["destination"], now, now))
    threading.Thread(target=_run_marketplace_install, args=(job_id, data), daemon=True).start()
    return record


@app.get("/marketplace/downloads")
def marketplace_downloads():
    with get_connection() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM model_download_jobs ORDER BY created_at DESC").fetchall()]
    with MARKETPLACE_JOB_LOCK:
        live = {item["id"]: item for item in MARKETPLACE_JOBS.values()}
    for row in rows:
        if row["id"] in live:
            row.update(live[row["id"]])
        else:
            row.update({"message": row.get("message") or row.get("error") or row["status"], "speed_bytes_per_second": row.get("speed_bytes_per_second") or 0})
    return rows


@app.delete("/marketplace/downloads/{job_id}")
def delete_download_history(job_id: str):
    with MARKETPLACE_JOB_LOCK:
        live = MARKETPLACE_JOBS.get(job_id)
        if live and live.get("status") in {"queued", "downloading", "verifying", "installing"}:
            raise HTTPException(status_code=409, detail="Cancel or finish this active download before deleting its history.")
    with get_connection() as db:
        row = db.execute("SELECT status FROM model_download_jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["status"] in {"queued", "downloading", "verifying", "installing"}:
            raise HTTPException(status_code=409, detail="Cancel or finish this active download before deleting its history.")
        cursor = db.execute("DELETE FROM model_download_jobs WHERE id=?", (job_id,))
    if cursor.rowcount == 0: raise HTTPException(status_code=404, detail="Download history entry not found.")
    with MARKETPLACE_JOB_LOCK:
        MARKETPLACE_JOBS.pop(job_id, None); MARKETPLACE_JOB_CANCEL.pop(job_id, None)
    return {"deleted": True, "id": job_id}


@app.delete("/marketplace/downloads")
def clear_download_history():
    with MARKETPLACE_JOB_LOCK:
        active = [job_id for job_id, job in MARKETPLACE_JOBS.items() if job.get("status") in {"queued", "downloading", "verifying", "installing"}]
        if active: raise HTTPException(status_code=409, detail="Cancel or finish active downloads before clearing history.")
    with get_connection() as db:
        active_rows = db.execute("SELECT COUNT(*) AS count FROM model_download_jobs WHERE status IN ('queued','downloading','verifying','installing')").fetchone()
        if active_rows["count"]: raise HTTPException(status_code=409, detail="Cancel or finish active downloads before clearing history.")
        db.execute("DELETE FROM model_download_jobs")
    with MARKETPLACE_JOB_LOCK:
        MARKETPLACE_JOBS.clear(); MARKETPLACE_JOB_CANCEL.clear(); MARKETPLACE_JOB_KEYS.clear()
    return {"deleted": True}


@app.post("/marketplace/downloads/{job_id}/cancel")
def marketplace_cancel(job_id: str):
    with MARKETPLACE_JOB_LOCK:
        if job_id not in MARKETPLACE_JOBS: raise HTTPException(status_code=404, detail="Download not found.")
        MARKETPLACE_JOB_CANCEL[job_id].set()
        return {"id": job_id, "status": "cancelling"}

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
# CODING AI MODEL AND ASSISTANT
# --------------------------------------------------

class CodingAssistantRequest(BaseModel):
    task: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=12000)
    relative_path: str = Field(default="", max_length=4096)
    code: str = Field(default="", max_length=50000)
    selection: str | None = Field(default=None, max_length=20000)


async def coding_model_health():
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{CODING_LLAMA_URL}/v1/models")
            if response.status_code == 200:
                payload = response.json()
                models = payload.get("data", [])
                model_id = models[0].get("id") if models else None
                return True, model_id
    except (httpx.RequestError, ValueError):
        pass

    return False, None


@app.get("/coding/model/status")
async def coding_model_status():
    global CODING_MODEL_PROCESS

    available, model_id = await coding_model_health()

    process_running = (
        CODING_MODEL_PROCESS is not None
        and CODING_MODEL_PROCESS.poll() is None
    )

    return {
        "available": available,
        "process_running": available or process_running,
        "model_id": model_id,
        "model_path": str(CODING_MODEL_PATH),
        "model_exists": CODING_MODEL_PATH.exists(),
        "runtime_url": CODING_LLAMA_URL,
        "message": (
            "Coding model is ready."
            if available
            else "Coding model is not responding."
        )
    }


@app.post("/coding/model/start")
async def start_coding_model():
    global CODING_MODEL_PROCESS

    available, _ = await coding_model_health()

    if available:
        return {
            "started": False,
            "message": "Coding model is already running."
        }

    with CODING_MODEL_LOCK:
        if CODING_MODEL_PROCESS is not None:
            if CODING_MODEL_PROCESS.poll() is None:
                return {
                    "started": False,
                    "message": "Coding model is already starting."
                }
            CODING_MODEL_PROCESS = None

        if not LLAMA_SERVER_EXE.exists():
            raise HTTPException(500, "llama-server.exe was not found.")

        if not CODING_MODEL_PATH.exists():
            raise HTTPException(404, "Coding GGUF model file was not found.")

        try:
            CODING_MODEL_PROCESS = subprocess.Popen(
                [
                    str(LLAMA_SERVER_EXE),
                    "--model", str(CODING_MODEL_PATH),
                    "--host", "127.0.0.1",
                    "--port", "8082",
                    "--ctx-size", "4096",
                    "--threads", "4",
                    "--n-gpu-layers", "0",
                    "--parallel", "1"
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        except OSError as exc:
            raise HTTPException(500, f"Unable to start coding model: {exc}")

    return {
        "started": True,
        "message": "Coding model server is starting."
    }


@app.post("/coding/model/stop")
async def stop_coding_model():
    global CODING_MODEL_PROCESS

    with CODING_MODEL_LOCK:
        process = CODING_MODEL_PROCESS

        if process is None:
            available, _ = await coding_model_health()

            if available:
                return {
                    "stopped": False,
                    "message": "The model was started externally. Stop it using Ctrl+C in its PowerShell window."
                }

            return {
                "stopped": False,
                "message": "Coding model is not running."
            }

        if process.poll() is None:
            process.terminate()

            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        CODING_MODEL_PROCESS = None

    return {
        "stopped": True,
        "message": "Coding model stopped."
    }


@app.post("/coding/assistant")
async def coding_assistant(data: CodingAssistantRequest):
    available, model_id = await coding_model_health()

    if not available:
        raise HTTPException(
            503,
            "Coding model is not running. Start Qwen2.5-Coder first."
        )

    system_prompt = """
You are the Coding AI assistant inside Local AI Studio.

Help the user understand, debug, explain, refactor and improve code.
Use the supplied source code and selection as context.
Give practical, accurate answers.
When suggesting code changes, provide complete replacement snippets
for the relevant section and explain where they belong.
Never claim that you modified files or executed commands.
All changes must be reviewed and approved by the user.
"""

    user_prompt = f"""
Task: {data.task}

User request:
{data.prompt}

File path:
{data.relative_path or "Not specified"}

Current file code:
{data.code or "No code supplied"}

Selected code:
{data.selection or "No selection"}
"""

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{CODING_LLAMA_URL}/v1/chat/completions",
                json={
                    "model": model_id or CODING_MODEL_NAME,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.2,
                    "max_tokens": 1200,
                    "stream": False
                }
            )

            response.raise_for_status()
            result = response.json()

        content = (
            result.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )

        if not content.strip():
            raise HTTPException(502, "Coding model returned an empty response.")

        return {
            "content": content.strip(),
            "task": data.task,
            "proposal": True,
            "model": CODING_MODEL_NAME
        }

    except httpx.TimeoutException:
        raise HTTPException(504, "Coding model response timed out.")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502,
            f"Coding model request failed: {exc.response.text[:500]}"
        )
    except httpx.RequestError:
        raise HTTPException(503, "Unable to connect to the coding model.")
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


# --------------------------------------------------
# PHASE 7.1 â€” DOCUMENT UPLOAD AND MANAGEMENT
# --------------------------------------------------

DOCUMENT_MAX_BYTES = 50 * 1024 * 1024
DOCUMENT_ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
DOCUMENTS_DIR = DATA_ROOT / "documents"


def _document_row(row):
    keys = set(row.keys())
    return {
        "id": row["id"], "filename": row["original_name"],
        "file_type": row["file_type"], "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"], "sha256": row["sha256"],
        "status": row["extraction_status"] if "extraction_status" in keys else row["status"],
        "uploaded_at": row["uploaded_at"],
        "page_count": row["page_count"] if "page_count" in keys else None,
        "extracted_chars": len(row["extracted_text"] or "") if "extracted_text" in keys else 0,
    }


def _extract_document(path: Path):
    extension = path.suffix.lower()
    if extension == ".txt":
        raw = path.read_bytes()
        try:
            return raw.decode("utf-8-sig"), None
        except UnicodeDecodeError:
            return raw.decode("cp1252", errors="replace"), None
    if extension == ".docx":
        from docx import Document
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table_index, table in enumerate(doc.tables, start=1):
            parts.append(f"[Table {table_index}]")
            parts.extend(" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows)
        return "\n".join(parts), None
    if extension == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        pages = [f"[Page {number}]\n{(page.extract_text() or '').strip()}" for number, page in enumerate(reader.pages, start=1)]
        return "\n\n".join(pages).strip(), len(reader.pages)
    raise ValueError("Unsupported document type.")


def _process_document(document_id: str, stored_name: str):
    try:
        extracted_text, page_count = _extract_document(DOCUMENTS_DIR / stored_name)
        searchable_text = re.sub(r"\[Page \d+\]", "", extracted_text).strip()
        status = "extracted" if searchable_text else "needs_ocr"
        with get_connection() as db:
            db.execute(
                """UPDATE documents SET extracted_text=?, extraction_status=?,
                   page_count=?, extracted_at=?, extraction_error=NULL WHERE id=?""",
                (extracted_text, status, page_count, get_time(), document_id)
            )
    except Exception as exc:
        with get_connection() as db:
            db.execute(
                """UPDATE documents SET extraction_status='failed',
                   extraction_error=?, extracted_at=? WHERE id=?""",
                (str(exc)[:1000], get_time(), document_id)
            )


def _resolve_tesseract():
    """Find a local Tesseract executable; no network access is used."""
    configured = os.environ.get("TESSERACT_CMD")
    candidates = [configured] if configured else []
    found = shutil.which("tesseract")
    if found:
        candidates.append(found)
    candidates.extend([
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"D:\Tesseract-OCR\tesseract.exe",
        r"D:\Program Files\Tesseract-OCR\tesseract.exe",
        r"D:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def _ocr_pdf(path: Path):
    """OCR each page separately to keep memory use bounded on 16 GB systems."""
    executable = _resolve_tesseract()
    if not executable:
        raise RuntimeError(
            "Tesseract OCR was not found. Install the Tesseract OCR Windows application, "
            "or set the TESSERACT_CMD environment variable to the full path of tesseract.exe, "
            "then restart LOCAL AI STUDIO."
        )
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "OCR Python dependencies are missing. Install pymupdf, pytesseract and Pillow "
            "in the LOCAL AI STUDIO backend virtual environment."
        ) from exc
    pytesseract.pytesseract.tesseract_cmd = executable
    parts = []
    with fitz.open(str(path)) as pdf:
        total_pages = len(pdf)
        if total_pages > 300:
            raise RuntimeError("OCR is limited to 300 pages per document in this prototype.")
        for page_number, page in enumerate(pdf, start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            text = pytesseract.image_to_string(image, lang="eng", config="--oem 1 --psm 3")
            parts.append(f"[Page {page_number}]\n{text.strip()}")
            del image, pix
    return "\n\n".join(parts).strip(), total_pages


def _store_document(source_path: Path, original_name: str, mime_type: str | None = None):
    original_name = Path(original_name).name
    extension = Path(original_name).suffix.lower()
    if not original_name or extension not in DOCUMENT_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only PDF, DOCX and TXT files are supported.")
    if not source_path.is_file() or source_path.is_symlink():
        raise HTTPException(status_code=400, detail="The selected path is not a regular file.")
    total = source_path.stat().st_size
    if total <= 0:
        raise HTTPException(status_code=400, detail="The selected file is empty.")
    if total > DOCUMENT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Document exceeds the 50 MB limit.")
    document_id = str(uuid.uuid4())
    stored_name = f"{document_id}{extension}"
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    destination = DOCUMENTS_DIR / stored_name
    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as src, destination.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                dst.write(chunk)
        uploaded_at = get_time()
        detected_mime = mime_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        with get_connection() as db:
            db.execute(
                """INSERT INTO documents
                (id, original_name, stored_name, file_type, mime_type, size_bytes, sha256, status, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'uploaded', ?)""",
                (document_id, original_name, stored_name, extension.lstrip(".").upper(),
                 detected_mime, total, digest.hexdigest(), uploaded_at)
            )
        _process_document(document_id, stored_name)
        with get_connection() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        return _document_row(row)
    except Exception:
        destination.unlink(missing_ok=True)
        with get_connection() as db:
            db.execute("DELETE FROM documents WHERE id=?", (document_id,))
        raise


@app.get("/documents")
def list_documents():
    with get_connection() as db:
        rows = db.execute("SELECT * FROM documents ORDER BY uploaded_at DESC").fetchall()
    return [_document_row(row) for row in rows]


@app.post("/documents/upload")
async def upload_document(file: UploadFile = File(...)):
    original_name = Path(file.filename or "").name
    extension = Path(original_name).suffix.lower()
    if extension not in DOCUMENT_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only PDF, DOCX and TXT files are supported.")
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    temporary = DOCUMENTS_DIR / f".upload-{uuid.uuid4().hex}{extension}"
    total = 0
    try:
        with temporary.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > DOCUMENT_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Document exceeds the 50 MB upload limit.")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="The selected file is empty.")
        return _store_document(temporary, original_name, file.content_type)
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()


@app.post("/documents/import-path")
def import_document_path(data: DocumentImportPath):
    source = Path(data.path).expanduser()
    try:
        source = source.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="Dropped file could not be found.")
    if source.suffix.lower() not in DOCUMENT_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"{source.name}: only PDF, DOCX and TXT are supported.")
    return _store_document(source, source.name)


@app.get("/documents/{document_id}/file")
def get_document_file(document_id: str):
    """Serve the original uploaded document for in-app viewing/download."""
    with get_connection() as db:
        row = db.execute(
            "SELECT original_name, stored_name, mime_type FROM documents WHERE id=?",
            (document_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    target = (DOCUMENTS_DIR / row["stored_name"]).resolve()
    if target.parent != DOCUMENTS_DIR.resolve() or not target.is_file():
        raise HTTPException(status_code=404, detail="Stored document file is unavailable.")
    media_type = row["mime_type"] or mimetypes.guess_type(row["original_name"])[0] or "application/octet-stream"
    return FileResponse(
        path=target,
        media_type=media_type,
        filename=row["original_name"],
        content_disposition_type="inline" if media_type == "application/pdf" else "attachment"
    )


@app.get("/documents/{document_id}/text")
def get_document_text(document_id: str):
    with get_connection() as db:
        row = db.execute("SELECT original_name, extracted_text, extraction_status, page_count, extraction_error FROM documents WHERE id=?", (document_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {
        "filename": row["original_name"],
        "text": "" if row["extraction_status"] == "needs_ocr" else (row["extracted_text"] or ""),
        "status": row["extraction_status"], "page_count": row["page_count"],
        "error": row["extraction_error"]
    }


@app.post("/documents/{document_id}/ocr")
def run_document_ocr(document_id: str):
    with get_connection() as db:
        row = db.execute(
            "SELECT stored_name, file_type FROM documents WHERE id=?", (document_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if str(row["file_type"]).upper() != "PDF":
        raise HTTPException(status_code=400, detail="OCR is currently available for PDF documents only.")
    target = (DOCUMENTS_DIR / row["stored_name"]).resolve()
    if target.parent != DOCUMENTS_DIR.resolve() or not target.is_file():
        raise HTTPException(status_code=404, detail="Stored PDF is unavailable.")
    try:
        extracted_text, page_count = _ocr_pdf(target)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"OCR failed: {str(exc)[:500]}")
    if not extracted_text:
        raise HTTPException(status_code=422, detail="OCR completed but no readable text was recognized in this PDF.")
    with get_connection() as db:
        db.execute(
            """UPDATE documents SET extracted_text=?, extraction_status='ocr_complete',
               page_count=?, extracted_at=?, extraction_error=NULL WHERE id=?""",
            (extracted_text, page_count, get_time(), document_id)
        )
    return {"status": "ocr_complete", "pages_processed": page_count, "extracted_chars": len(extracted_text)}


@app.delete("/documents/{document_id}")
def delete_document(document_id: str):
    with get_connection() as db:
        row = db.execute("SELECT stored_name FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        stored_name = row["stored_name"]
        db.execute("DELETE FROM documents WHERE id=?", (document_id,))
    target = (DOCUMENTS_DIR / stored_name).resolve()
    if target.parent == DOCUMENTS_DIR.resolve():
        target.unlink(missing_ok=True)
    return {"deleted": True, "id": document_id}


# --------------------------------------------------
# PHASE 7.3 â€” DOCUMENT STUDY, GROUNDED Q&A AND STUDY MATERIALS
# --------------------------------------------------

class DocumentAskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=4000)

class DocumentSynopsisRequest(BaseModel):
    style: str = Field(default="detailed", pattern="^(short|detailed|chapter|topic)$")

class DocumentQuestionRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=30)
    difficulty: str = Field(default="mixed", pattern="^(easy|medium|hard|mixed)$")
    question_type: str = Field(default="mixed", pattern="^(mcq|short|long|mixed)$")

class DocumentMaterialCreate(BaseModel):
    material_type: str = Field(pattern="^(note|answer|synopsis|flashcards|glossary|questions|quiz|concept_map)$")
    title: str = Field(default="", max_length=200)
    content: str = Field(min_length=1, max_length=200000)


def _get_study_document(document_id: str):
    with get_connection() as db:
        row = db.execute(
            "SELECT id, original_name, extracted_text, extraction_status, page_count FROM documents WHERE id=?",
            (document_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found.")
    text = (row["extracted_text"] or "").strip()
    if not text or row["extraction_status"] in ("needs_ocr", "failed", "pending"):
        raise HTTPException(status_code=409, detail="This document has no usable extracted text yet. Run OCR or extraction first.")
    return row, text


def _document_passages(text: str, max_chars: int = 1400):
    """Create bounded page-aware passages from the stored [Page N] text."""
    pages = re.split(r"(?=\[Page\s+\d+\])", text)
    passages = []
    for index, raw in enumerate(pages, start=1):
        if not raw.strip():
            continue
        match = re.match(r"\[Page\s+(\d+)\]\s*", raw)
        page = int(match.group(1)) if match else index
        body = raw[match.end():] if match else raw
        body = body.strip()
        if not body:
            continue
        # Split long pages at paragraph boundaries, then hard split only if needed.
        chunks = []
        current = ""
        for paragraph in re.split(r"\n\s*\n", body):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if len(current) + len(paragraph) + 2 > max_chars and current:
                chunks.append(current)
                current = ""
            while len(paragraph) > max_chars:
                if current:
                    chunks.append(current); current = ""
                chunks.append(paragraph[:max_chars]); paragraph = paragraph[max_chars:]
            current = (current + "\n\n" + paragraph).strip()
        if current:
            chunks.append(current)
        for chunk in chunks:
            passages.append({"page": page, "text": chunk})
    return passages


def _retrieve_document_passages(text: str, query: str, limit: int = 7, document_id: str | None = None):
    passages = _document_passages(text)
    terms = {term.lower() for term in re.findall(r"[A-Za-z0-9]{3,}", query)}
    if not terms:
        return passages[:limit]
    scored = []
    for passage in passages:
        words = re.findall(r"[A-Za-z0-9]{3,}", passage["text"].lower())
        counts = {}
        for word in words:
            counts[word] = counts.get(word, 0) + 1
        score = sum(min(counts.get(term, 0), 4) for term in terms)
        if score:
            scored.append((score, passage))
    scored.sort(key=lambda item: item[0], reverse=True)
    lexical_ranked = [item[1] for item in scored]

    # Rebuild the small per-document FTS index lazily, then combine BM25 and
    # token-overlap rankings with reciprocal-rank fusion. This avoids a second
    # embedding model competing for RAM on the user's 16 GB machine.
    fts_ranked = []
    if document_id:
        try:
            with get_connection() as db:
                db.execute("DELETE FROM document_passages_fts WHERE document_id=?", (document_id,))
                db.executemany(
                    "INSERT INTO document_passages_fts(document_id, page, content) VALUES(?,?,?)",
                    [(document_id, str(p["page"]), p["text"]) for p in passages]
                )
                match_query = " OR ".join('"' + term.replace('"', '') + '"' for term in sorted(terms))
                if match_query:
                    rows = db.execute(
                        "SELECT page, content, bm25(document_passages_fts) AS score FROM document_passages_fts WHERE document_passages_fts MATCH ? AND document_id=? ORDER BY score LIMIT ?",
                        (match_query, document_id, limit * 2)
                    ).fetchall()
                    fts_ranked = [{"page": int(r["page"]), "text": r["content"]} for r in rows]
        except sqlite3.Error:
            fts_ranked = []
    if not fts_ranked:
        return lexical_ranked[:limit]
    fused = {}
    for rank, passage in enumerate(fts_ranked, start=1):
        key = (passage["page"], passage["text"])
        fused[key] = fused.get(key, 0.0) + 1.0 / (60 + rank)
    for rank, passage in enumerate(lexical_ranked, start=1):
        key = (passage["page"], passage["text"])
        fused[key] = fused.get(key, 0.0) + 1.0 / (60 + rank)
    by_key = {(p["page"], p["text"]): p for p in passages}
    return [by_key[key] for key, _ in sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]]


async def _document_model_completion(system_prompt: str, user_prompt: str, max_tokens: int = 900, temperature: float = 0.2):
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }
    try:
        async with httpx.AsyncClient(timeout=240.0) as client:
            response = await client.post(f"{LLAMA_CPP_URL}/v1/chat/completions", json=payload)
            response.raise_for_status()
            result = response.json()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content.strip():
                raise RuntimeError("The local model returned an empty response.")
            return content.strip()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Local model is unavailable at {LLAMA_CPP_URL}. Start your configured llama.cpp model and retry. Details: {str(exc)[:300]}")
    except (KeyError, IndexError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Unexpected response from the local model: {str(exc)[:200]}")


def _extract_json_payload(raw: str):
    """Parse JSON even when a local model adds fences or short preambles."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    # First try the entire response, then locate the first JSON object/array.
    try:
        return json.loads(text)
    except Exception:
        pass
    for index, char in enumerate(text):
        if char in "[{":
            try:
                value, _ = decoder.raw_decode(text[index:])
                return value
            except Exception:
                continue
    raise ValueError("No valid JSON object or array was found in the model response")


async def _document_json_completion(system_prompt: str, user_prompt: str, max_tokens: int = 1800):
    """Request structured output, then make one local repair attempt if needed."""
    raw = await _document_model_completion(system_prompt, user_prompt, max_tokens=max_tokens, temperature=0.1)
    try:
        return _extract_json_payload(raw)
    except ValueError:
        repaired = await _document_model_completion(
            "Convert the supplied model output into valid JSON only. Preserve all factual content. "
            "Do not add facts. Return a valid JSON array or object with no markdown fences.",
            f"Required JSON schema/instructions:\n{system_prompt}\n\nOriginal output to repair:\n{raw}",
            max_tokens=max_tokens, temperature=0.0
        )
        try:
            return _extract_json_payload(repaired)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=(
                "The local model completed but returned malformed structured data, even after one repair attempt. "
                "Try fewer items or use a smaller document. " + str(exc)
            ))


@app.post("/documents/{document_id}/ask")
async def ask_document(document_id: str, data: DocumentAskRequest):
    row, text = _get_study_document(document_id)
    passages = _retrieve_document_passages(text, data.question, limit=8, document_id=document_id)
    if not passages:
        return {"answer": "I could not find relevant information for this question in the extracted document. Try different wording or check the extracted text.", "sources": [], "grounded": False}
    source_block = "\n\n".join(f"[Page {item['page']}]\n{item['text']}" for item in passages)
    answer = await _document_model_completion(
        "You are a document-grounded study assistant. Answer ONLY using the supplied excerpts. Cite every factual claim with [Page N]. If the excerpts do not contain the answer, say that the document does not provide enough information. Do not use outside knowledge. Be clear and concise.",
        f"Document: {row['original_name']}\nQuestion: {data.question}\n\nRetrieved document excerpts:\n{source_block}\n\nAnswer with page citations.",
        max_tokens=800
    )
    sources = sorted({item["page"] for item in passages})
    return {"answer": answer, "sources": sources, "grounded": True}


@app.post("/documents/{document_id}/synopsis")
async def create_document_synopsis(document_id: str, data: DocumentSynopsisRequest):
    row, text = _get_study_document(document_id)
    # Keep the prompt within a conservative context budget on 16 GB machines.
    passages = _document_passages(text, max_chars=1800)
    content = "\n\n".join(f"[Page {p['page']}] {p['text']}" for p in passages)
    if len(content) > 24000:
        content = content[:24000] + "\n[Document text truncated for this request]"
    styles = {
        "short": "Write a concise synopsis with the central topic, key points and conclusion.",
        "detailed": "Write a detailed study synopsis with overview, main concepts, important details and conclusion.",
        "chapter": "Organize the synopsis by chapter or section. If headings are absent, organize by the document's natural sections and say so.",
        "topic": "Organize the synopsis topic-wise with clear headings and concise explanations."
    }
    result = await _document_model_completion(
        "You summarize only the supplied document. Preserve its terminology and organization. Do not invent missing sections or facts. Include page references [Page N] for key points. If source text is incomplete, state that limitation.",
        f"Create a synopsis for: {row['original_name']}\nInstruction: {styles[data.style]}\n\nDocument text:\n{content}",
        max_tokens=1400
    )
    return {"synopsis": result, "style": data.style, "document_id": document_id}


@app.post("/documents/{document_id}/questions")
async def generate_document_questions(document_id: str, data: DocumentQuestionRequest):
    row, text = _get_study_document(document_id)
    passages = _document_passages(text, max_chars=1500)
    content = "\n\n".join(f"[Page {p['page']}] {p['text']}" for p in passages)
    if len(content) > 22000:
        content = content[:22000] + "\n[Document text truncated]"
    questions = await _document_json_completion(
        "You create study questions strictly from the supplied document. Return a JSON array of objects with keys: question, type, difficulty, answer, explanation, page, options. Every question MUST include a non-empty answer and explanation. For MCQ, options must be an array of four strings and answer must exactly match one option. For non-MCQ, options must be an empty array. Use page as an integer when identifiable, otherwise null. Do not invent facts. No markdown fences.",
        f"Create exactly {data.count} questions from {row['original_name']}. Difficulty: {data.difficulty}. Type: {data.question_type}.\n\nDocument:\n{content}",
        max_tokens=min(4200, 350 + data.count * 190)
    )
    if isinstance(questions, dict):
        questions = questions.get("questions", [])
    if not isinstance(questions, list):
        raise HTTPException(status_code=502, detail="The local model returned JSON, but it was not a question array. Try again with fewer questions.")
    normalized = []
    for item in questions[:data.count]:
        if not isinstance(item, dict) or not str(item.get("question", "")).strip():
            continue
        normalized.append({
            "question": str(item.get("question", "")).strip(),
            "type": str(item.get("type", data.question_type)),
            "difficulty": str(item.get("difficulty", data.difficulty)),
            "answer": str(item.get("answer", "Not specified in generated output")),
            "explanation": str(item.get("explanation", "")),
            "page": item.get("page") if isinstance(item.get("page"), int) else None,
            "options": item.get("options", []) if isinstance(item.get("options", []), list) else []
        })
    if not normalized:
        raise HTTPException(status_code=502, detail="No usable questions were returned by the local model.")
    return {"document_id": document_id, "filename": row["original_name"], "questions": normalized}


def _normalize_document_flashcards(raw_cards, requested_count: int):
    """
    Normalize common local-LLM flashcard response formats.
    Accepts front/back and question/answer field names.
    """
    if isinstance(raw_cards, dict):
        for key in ("flashcards", "cards", "items", "results"):
            if isinstance(raw_cards.get(key), list):
                raw_cards = raw_cards[key]
                break

    if not isinstance(raw_cards, list):
        return []

    normalized = []

    for card in raw_cards:
        if not isinstance(card, dict):
            continue

        front = (
            card.get("front")
            or card.get("question")
            or card.get("term")
            or card.get("prompt")
        )

        back = (
            card.get("back")
            or card.get("answer")
            or card.get("definition")
            or card.get("explanation")
        )

        front = str(front or "").strip()
        back = str(back or "").strip()

        if not front or not back:
            continue

        page = card.get("page")
        if isinstance(page, str):
            match = re.search(r"\d+", page)
            page = int(match.group()) if match else None
        elif not isinstance(page, int) or isinstance(page, bool):
            page = None

        normalized.append({
            "front": front,
            "back": back,
            "page": page
        })

        if len(normalized) >= requested_count:
            break

    return normalized


@app.post("/documents/{document_id}/flashcards")
async def generate_document_flashcards(
    document_id: str,
    data: DocumentQuestionRequest
):
    row, text = _get_study_document(document_id)

    passages = _document_passages(text, max_chars=1400)

    content = "\n\n".join(
        f"[Page {p['page']}]\n{p['text']}"
        for p in passages
    )[:18000]

    system_prompt = """
You are an expert study-material generator.

Create flashcards using ONLY the supplied document.
Do not use outside knowledge or invent facts.

Return ONLY a valid JSON array.
Every array item must have exactly these fields:
{
  "front": "A concise question or term",
  "back": "A concise, accurate answer",
  "page": 1
}

Rules:
1. Every card must have a non-empty front and back.
2. Ask one clear question per card.
3. Cover important definitions, concepts, facts, processes and comparisons.
4. Avoid duplicate or repetitive cards.
5. Keep questions and answers concise but meaningful.
6. Preserve technical terminology from the document.
7. Use the correct source page number when identifiable.
8. If the page is unknown, use null.
9. Do not include explanations outside the JSON array.
10. Do not use Markdown code fences.
"""

    user_prompt = f"""
Create exactly {data.count} useful study flashcards.

Document: {row['original_name']}
Requested difficulty: {data.difficulty}

Document content:
{content}
"""

    max_tokens = min(2800, max(900, 400 + data.count * 180))

    # First generation attempt
    raw_cards = await _document_json_completion(
        system_prompt,
        user_prompt,
        max_tokens=max_tokens
    )
    print("\n========== FLASHCARD DEBUG ==========")
    print("MODEL RAW/PARSED RESPONSE:")
    print(repr(cards))
    print("RESPONSE TYPE:", type(cards))
    print("=====================================\n")

    cards = _normalize_document_flashcards(
        raw_cards,
        data.count
    )

    # Retry once if the first response contains no usable cards.
    if not cards:
        retry_prompt = f"""
The previous response did not contain usable flashcards.

Generate exactly {data.count} flashcards from the document below.

Return ONLY a JSON array.
Use precisely these keys for every card:
front, back, page

Example format:
[
  {{
    "front": "What is the main concept?",
    "back": "The explanation given in the document.",
    "page": 1
  }}
]

Do not use question/answer keys.
Do not include Markdown or additional text.

Document:
{content}
"""

        raw_cards = await _document_json_completion(
            system_prompt,
            retry_prompt,
            max_tokens=max_tokens
        )

        cards = _normalize_document_flashcards(
            raw_cards,
            data.count
        )

    if not cards:
        raise HTTPException(
            status_code=502,
            detail=(
                "The local model could not generate usable flashcards "
                "after two attempts. Try a shorter document or fewer cards."
            )
        )

    return {
        "document_id": document_id,
        "filename": row["original_name"],
        "flashcards": cards,
        "generated_count": len(cards),
        "requested_count": data.count
    }


@app.post("/documents/{document_id}/concept-map")
async def generate_document_concept_map(document_id: str):
    row, text = _get_study_document(document_id)
    content = "\n\n".join(f"[Page {p['page']}] {p['text']}" for p in _document_passages(text, 1500))[:16000]
    result = await _document_model_completion(
        "Create a concept map strictly from the supplied document. Return Mermaid mindmap source only, beginning with 'mindmap'. Use short node labels. Do not add concepts absent from the source.",
        f"Create a concept map for {row['original_name']}.\n\n{content}", max_tokens=1200
    )
    result = re.sub(r"^```(?:mermaid)?\s*|\s*```$", "", result.strip(), flags=re.I)
    if not result.lstrip().startswith("mindmap"):
        raise HTTPException(status_code=502, detail="The local model did not return a valid Mermaid mindmap. Try again.")
    return {"document_id": document_id, "concept_map": result}


@app.post("/documents/{document_id}/glossary")
async def generate_document_glossary(document_id: str):
    row, text = _get_study_document(document_id)
    content = "\n\n".join(f"[Page {p['page']}] {p['text']}" for p in _document_passages(text, 1600))[:18000]
    terms = await _document_json_completion(
        "Extract a glossary of important terms used in the document. Return a JSON array with term, definition, page. Definitions must reflect the document only. No markdown fences.",
        f"Create a glossary for {row['original_name']}.\n\n{content}", max_tokens=1800
    )
    if isinstance(terms, dict): terms = terms.get("glossary", [])
    if not isinstance(terms, list):
        raise HTTPException(status_code=502, detail="The local model returned JSON, but it was not a glossary array.")
    terms = [t for t in terms if isinstance(t, dict) and str(t.get("term", "")).strip() and str(t.get("definition", "")).strip()]
    if not terms:
        raise HTTPException(status_code=502, detail="The model returned no usable glossary terms. Try again.")
    return {"document_id": document_id, "glossary": terms[:80]}


@app.get("/documents/{document_id}/materials")
def list_document_materials(document_id: str):
    _get_study_document(document_id)
    with get_connection() as db:
        rows = db.execute("SELECT id, material_type, title, content, created_at FROM document_materials WHERE document_id=? ORDER BY created_at DESC", (document_id,)).fetchall()
    return [dict(row) for row in rows]


@app.post("/documents/{document_id}/materials")
def save_document_material(document_id: str, data: DocumentMaterialCreate):
    _get_study_document(document_id)
    material_id = str(uuid.uuid4())
    with get_connection() as db:
        db.execute("INSERT INTO document_materials(id, document_id, material_type, title, content, created_at) VALUES(?,?,?,?,?,?)", (material_id, document_id, data.material_type, data.title, data.content, get_time()))
    return {"id": material_id, "document_id": document_id, "material_type": data.material_type, "title": data.title, "content": data.content}


@app.delete("/documents/{document_id}/materials/{material_id}")
def delete_document_material(document_id: str, material_id: str):
    with get_connection() as db:
        cursor = db.execute("DELETE FROM document_materials WHERE id=? AND document_id=?", (material_id, document_id))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Saved study material not found.")
    return {"deleted": True}
