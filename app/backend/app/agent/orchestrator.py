"""Persistent planning workflow; deliberately exposes no write or shell tools."""
from __future__ import annotations

import json
import asyncio
import os
import sqlite3
import sys
import time
import uuid
import threading
import ctypes
import logging
import re
from urllib.parse import urlsplit, urlunsplit
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, Query, Body
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import project_intelligence as intelligence
from .. import coding_changes

router = APIRouter(prefix="/agent", tags=["agent"])
logger = logging.getLogger(__name__)
project_router = APIRouter(prefix="/project-intelligence", tags=["project-intelligence"])
SCAN_JOBS: dict[str, dict] = {}
SCAN_LOCK = threading.Lock()
CHANGE_GENERATION_LOCK = threading.Lock()
CHANGE_GENERATION_ACTIVE: set[str] = set()
PLAN_GENERATION_LOCK = threading.Lock()
PLAN_GENERATION_ACTIVE: set[str] = set()
DB_PATH = Path(os.environ.get("LOCAL_AI_HOME", r"D:\LocalAIStudio\data")) / "database" / "local_ai.db"
STATUSES = {"pending", "in_progress", "waiting_for_approval", "completed", "failed", "blocked", "cancelled"}
SAFE_TOOLS = {"project.list_files", "project.read_file", "project.search", "project.structure", "project.summary", "project.symbols", "project.context"}
ROLE_CAPABILITIES = {"planner":"planning", "coding":"coding", "lightweight_reasoning":"reasoning", "embedding":"embedding", "project_summarization":"summarization"}
DEFAULT_INFERENCE_SETTINGS = {"read_timeout_seconds":90.0,"connect_timeout_seconds":5.0,"keep_alive":"0","max_generated_tokens":1800}
CITATION_PATTERN = re.compile(r"\[([^\]\r\n]+?):(\d+)-(\d+)\]")


def _invalid_citations(answer: str, items: list[dict]) -> list[str]:
    """Return citations that do not point inside source ranges actually retrieved."""
    ranges = {}
    for item in items:
        path = str(item.get("path", ""))
        start, end = item.get("line_start"), item.get("line_end")
        if path and isinstance(start, int) and isinstance(end, int):
            ranges.setdefault(path, []).append((start, end))
    invalid = []
    for match in CITATION_PATTERN.finditer(answer):
        path, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        if end < start or not any(start >= low and end <= high for low, high in ranges.get(path, [])):
            invalid.append(match.group(0))
    return invalid


def _inference_settings(profile="default"):
    """Return isolated inference settings; Ask can be tuned without affecting planning or benchmarks."""
    if profile == "planning":
        return {"read_timeout_seconds":float(os.environ.get("AGENT_PLAN_READ_TIMEOUT_SECONDS","300")),"connect_timeout_seconds":5.0,"keep_alive":"0","max_generated_tokens":int(os.environ.get("AGENT_PLAN_MAX_GENERATED_TOKENS","2600"))}
    if profile == "coding":
        return {"read_timeout_seconds":float(os.environ.get("AGENT_CODING_READ_TIMEOUT_SECONDS","900")),"connect_timeout_seconds":5.0,"keep_alive":"0","max_generated_tokens":int(os.environ.get("AGENT_CODING_MAX_GENERATED_TOKENS","6000"))}
    if profile!="ask": return dict(DEFAULT_INFERENCE_SETTINGS)
    return {
        "read_timeout_seconds":float(os.environ.get("AGENT_ASK_READ_TIMEOUT_SECONDS","300")),
        "connect_timeout_seconds":float(os.environ.get("AGENT_ASK_CONNECT_TIMEOUT_SECONDS","5")),
        "keep_alive":os.environ.get("AGENT_ASK_OLLAMA_KEEP_ALIVE","5m"),
        "max_generated_tokens":int(os.environ.get("AGENT_ASK_MAX_GENERATED_TOKENS","600")),
    }


def _change_storage(request: Request) -> Path:
    data_root = getattr(request.app.state, "data_root", Path(os.environ.get("LOCAL_AI_HOME", r"D:\LocalAIStudio\data")))
    return Path(data_root) / "coding_changesets"


@contextmanager
def connect():
    db=sqlite3.connect(DB_PATH,timeout=10); db.row_factory=sqlite3.Row; db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize(db_path: Path | None = None):
    global DB_PATH
    if db_path: DB_PATH=Path(db_path)
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_sessions(id TEXT PRIMARY KEY, project_key TEXT NOT NULL, project_root TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('ask','planning')), status TEXT NOT NULL CHECK(status IN ('awaiting_input','ready','planning','waiting_for_approval','plan_approved','plan_rejected','paused','failed','cancelled')), requirement TEXT NOT NULL DEFAULT '', model_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', paused_from TEXT);
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_project ON agent_sessions(project_key,updated_at DESC);
        CREATE TABLE IF NOT EXISTS agent_plans(id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE, plan_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('waiting_for_approval','approved','rejected')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS agent_tasks(id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, parent_id TEXT, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status IN ('pending','in_progress','waiting_for_approval','completed','failed','blocked','cancelled')), acceptance_criteria_json TEXT NOT NULL DEFAULT '[]', position INTEGER NOT NULL, FOREIGN KEY(plan_id) REFERENCES agent_plans(id) ON DELETE CASCADE, FOREIGN KEY(parent_id) REFERENCES agent_tasks(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS agent_task_dependencies(task_id TEXT NOT NULL, depends_on_task_id TEXT NOT NULL, PRIMARY KEY(task_id,depends_on_task_id), CHECK(task_id<>depends_on_task_id), FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE, FOREIGN KEY(depends_on_task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS agent_events(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, event_type TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS idx_agent_events_session ON agent_events(session_id,created_at);
        CREATE TABLE IF NOT EXISTS agent_messages(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,model_id TEXT,created_at TEXT NOT NULL,FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id,created_at);
        CREATE TABLE IF NOT EXISTS agent_tool_executions(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool_name TEXT NOT NULL, permission TEXT NOT NULL, input_json TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS agent_benchmarks(id TEXT PRIMARY KEY, model_id TEXT NOT NULL, model_name TEXT NOT NULL, runtime TEXT NOT NULL, results_json TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        session_columns={row["name"] for row in db.execute("PRAGMA table_info(agent_sessions)").fetchall()}
        if "paused_from" not in session_columns: db.execute("ALTER TABLE agent_sessions ADD COLUMN paused_from TEXT")
        db.execute("UPDATE agent_sessions SET status='failed',error='Planning was interrupted by application shutdown',updated_at=? WHERE status='planning'",(now(),))


def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())

def available_memory_bytes():
    if sys.platform=="win32":
        class MemoryStatus(ctypes.Structure):
            _fields_=[("dwLength",ctypes.c_ulong),("dwMemoryLoad",ctypes.c_ulong),("ullTotalPhys",ctypes.c_ulonglong),("ullAvailPhys",ctypes.c_ulonglong),("ullTotalPageFile",ctypes.c_ulonglong),("ullAvailPageFile",ctypes.c_ulonglong),("ullTotalVirtual",ctypes.c_ulonglong),("ullAvailVirtual",ctypes.c_ulonglong),("ullAvailExtendedVirtual",ctypes.c_ulonglong)]
        status=MemoryStatus(); status.dwLength=ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)): return int(status.ullAvailPhys)
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except ImportError:
        return None

def recommend_models(models, role, available_bytes=None):
    available_bytes=available_bytes if available_bytes is not None else available_memory_bytes()
    output=[]
    for model in models:
        name=model["name"].casefold(); metadata=model.get("metadata") or {}
        if metadata.get("is_cloud") or name.endswith(":cloud"): continue
        embedding=role=="embedding"
        if embedding != ("embed" in name or bool(metadata.get("embedding_length"))): continue
        size=int(model.get("file_size_bytes") or 0)
        estimated=int(size*(1.25 if embedding else 1.45)+(300_000_000 if embedding else 1_500_000_000)) if size else None
        fits=estimated is None or available_bytes is None or estimated<=int(available_bytes*.8)
        if not fits: continue
        role_terms={"planner":("qwen","instruct","reason"),"coding":("coder","code"),"lightweight_reasoning":("3b","small","mini","135m"),"project_summarization":("qwen","instruct","3b"),"embedding":("embed",)}.get(role,())
        score=sum(2 for term in role_terms if term in name)
        score+=1 if model.get("runtime")=="ollama" else 0
        output.append({"id":model["id"],"name":model["name"],"runtime":model.get("runtime"),"estimated_working_set_bytes":estimated,"fits_available_memory":fits,"score":score,"selection":"recommendation only; user chooses"})
    return sorted(output,key=lambda item:(-item["score"],item["estimated_working_set_bytes"] or 0,item["name"].casefold()))


def event(db, session_id, event_type, message, data=None):
    db.execute("INSERT INTO agent_events VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),session_id,event_type,message,json.dumps(data or {}),now()))


def root_for(request: Request):
    root=getattr(request.app.state,"active_workspace",None)
    if not root: raise HTTPException(400,"Open a project in Coding Workspace first")
    root=Path(root).resolve()
    if not root.is_dir(): raise HTTPException(400,"The selected project folder is no longer available")
    return root


class SessionCreate(BaseModel):
    mode: Literal["ask","planning"] = "planning"
    requirement: str = Field(default="",max_length=12000)
    model_id: str | None = Field(default=None,max_length=160)

class Requirement(BaseModel):
    requirement: str = Field(min_length=5,max_length=12000)
    model_id: str | None = Field(default=None,max_length=160)

class PlanApproval(BaseModel):
    approved: bool

class ChangeGenerationRequest(BaseModel):
    model_id: str | None = Field(default=None,max_length=160)
    unsaved_paths: list[str] = Field(default_factory=list,max_length=100)

class FileChangeDecision(BaseModel):
    decision: Literal["approved","rejected"]

class ReviewAllChanges(BaseModel):
    decision: Literal["approved","rejected"]

class FileOperationGuard(BaseModel):
    unsaved_paths: list[str] = Field(default_factory=list,max_length=100)

class TaskStatusUpdate(BaseModel):
    status: Literal["pending","in_progress","waiting_for_approval","completed","failed","blocked","cancelled"]

class RoleAssignment(BaseModel):
    role: Literal["planner","coding","lightweight_reasoning","embedding","project_summarization"]
    model_id: str | None = None

class BenchmarkRequest(BaseModel):
    model_id: str = Field(min_length=1,max_length=160)

class ProjectScanRequest(BaseModel):
    exclude_paths: list[str] = Field(default_factory=list,max_length=200)

    @staticmethod
    def validate_paths(paths):
        normalized=[]
        for value in paths:
            raw=value.replace("\\","/").strip()
            if raw.startswith("/") or ":" in raw or ".." in raw.split("/"):
                raise ValueError("Exclusion entries must be relative project paths or file/folder names")
            item=raw.strip("/")
            if not item or len(item)>240:
                raise ValueError("Exclusion entries must be relative project paths or file/folder names")
            normalized.append(item)
        return list(dict.fromkeys(normalized))

class SessionResponse(BaseModel):
    id: str
    project_key: str
    project_root: str
    mode: Literal["ask","planning"]
    status: Literal["awaiting_input","ready","planning","waiting_for_approval","plan_approved","plan_rejected","paused","failed","cancelled"]
    requirement: str
    model_id: str | None = None
    created_at: str
    updated_at: str
    error: str = ""
    paused_from: str | None = None
    plan_status: str | None = None
    title: str = "New session"
    title_manually_edited: bool = False

class SessionTitleUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=100)

class TaskResponse(BaseModel):
    id: str
    plan_id: str
    parent_id: str | None = None
    title: str
    description: str
    status: Literal["pending","in_progress","waiting_for_approval","completed","failed","blocked","cancelled"]
    acceptance_criteria: list[str]
    dependencies: list[str]
    position: int

class PlanResponse(BaseModel):
    session: SessionResponse
    plan: dict | None = None
    tasks: list[TaskResponse]
    clarification_questions: list[str] | None = None

class PlanDetailResponse(BaseModel):
    id: str
    session_id: str
    plan_json: str
    status: Literal["waiting_for_approval","approved","rejected"]
    created_at: str
    updated_at: str
    plan: dict
    tasks: list[TaskResponse]

class AnswerResponse(BaseModel):
    session: SessionResponse
    answer: str
    context: dict

class EventResponse(BaseModel):
    id: str
    session_id: str
    event_type: str
    message: str
    data_json: str
    created_at: str
    data: dict


def _session(db, session_id):
    row=db.execute("SELECT * FROM agent_sessions WHERE id=?",(session_id,)).fetchone()
    if not row: raise HTTPException(404,"Agent session not found")
    return dict(row)


def _with_session_title(db, session):
    custom = db.execute("SELECT message FROM agent_events WHERE session_id=? AND event_type='session_title_edited' ORDER BY created_at DESC,id DESC LIMIT 1", (session["id"],)).fetchone()
    first_user = db.execute("SELECT content FROM agent_messages WHERE session_id=? AND role='user' ORDER BY created_at,rowid LIMIT 1", (session["id"],)).fetchone()
    title = custom["message"] if custom else (first_user["content"] if first_user else "New session")
    title = " ".join(str(title).split())
    if not custom and len(title) > 72: title = title[:69].rstrip() + "..."
    session["title"] = title
    session["title_manually_edited"] = bool(custom)
    return session


@router.post("/sessions")
async def create_session(data: SessionCreate, request: Request):
    root=root_for(request); project_key=__import__("hashlib").sha256(str(root).casefold().encode()).hexdigest(); session_id=str(uuid.uuid4()); timestamp=now()
    with connect() as db:
        db.execute("INSERT INTO agent_sessions(id,project_key,project_root,mode,status,requirement,model_id,created_at,updated_at,error) VALUES(?,?,?,?,?,?,?,?,?,?)",(session_id,project_key,str(root),data.mode,"awaiting_input" if not data.requirement.strip() else "ready",data.requirement.strip(),data.model_id,timestamp,timestamp,""))
        event(db,session_id,"session_created","Agent session created",{"mode":data.mode,"project_root":root.name})
    if data.requirement.strip(): return generate_plan_internal(session_id,data.requirement,data.model_id,request) if data.mode=="planning" else {"session":get_session(session_id),"answer":None,"needs_requirement":False}
    return get_session(session_id)


@router.get("/sessions",response_model=list[SessionResponse])
def list_sessions(request: Request):
    active_root=getattr(request.app.state,"active_workspace",None)
    if active_root:
        root=root_for(request); key=__import__("hashlib").sha256(str(root).casefold().encode()).hexdigest()
        query="SELECT * FROM agent_sessions WHERE project_key=? ORDER BY updated_at DESC LIMIT 100"; params=(key,)
    else:
        # Session metadata is local and read-only. Allow the Agent to recover
        # history after restart even if Coding Workspace has no selected folder.
        query="SELECT * FROM agent_sessions ORDER BY updated_at DESC LIMIT 100"; params=()
    with connect() as db: rows=db.execute(query,params).fetchall()
    with connect() as db: return [_with_session_title(db, dict(r)) for r in rows]


def get_session(session_id):
    with connect() as db:
        s=_with_session_title(db, _session(db,session_id)); plan=db.execute("SELECT * FROM agent_plans WHERE session_id=?",(session_id,)).fetchone(); s["plan_status"]=plan["status"] if plan else None
        return s


@router.patch("/sessions/{session_id}/title", response_model=SessionResponse)
def update_session_title(session_id: str, data: SessionTitleUpdate, request: Request):
    title = " ".join(data.title.split())
    if not title: raise HTTPException(400, "Session title cannot be blank")
    with connect() as db:
        session = _session(db, session_id)
        if Path(session["project_root"]).resolve() != root_for(request): raise HTTPException(404, "Agent session not found for this workspace")
        event(db, session_id, "session_title_edited", title, {"manually_edited": True})
        db.execute("UPDATE agent_sessions SET updated_at=? WHERE id=?", (now(), session_id))
    return get_session(session_id)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request):
    session = get_session(session_id)
    active_root = getattr(request.app.state, "active_workspace", None)
    if active_root and Path(session["project_root"]).resolve() != Path(active_root).resolve():
        raise HTTPException(404, "Agent session not found for this workspace")
    if session["status"] == "planning":
        raise HTTPException(409, "A planning session cannot be deleted while it is running")
    with connect() as db:
        db.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))
    return {"deleted": True}


def session_get(session_id: str, request: Request):
    session=get_session(session_id)
    if Path(session["project_root"]).resolve()!=root_for(request): raise HTTPException(404,"Agent session not found for this workspace")
    return session


@router.get("/sessions/{session_id}",response_model=SessionResponse)
def read_session(session_id: str, request: Request):
    # Session metadata is read-only; project association checks belong to
    # operations that inspect or change project files.
    return get_session(session_id)


def _selected_model(db, model_id):
    if not model_id:
        role=db.execute("SELECT model_id,endpoint_id FROM model_capability_assignments WHERE capability='planning'").fetchone()
        model_id=(role["model_id"] or role["endpoint_id"]) if role else None
    if not model_id: raise HTTPException(400,"Select an installed local model for the planner in Model Manager or Agent settings")
    model=db.execute("SELECT id,name,runtime,source_type,status,metadata_json FROM model_registry WHERE id=?",(model_id,)).fetchone()
    if model and model["status"]=="installed":
        metadata=json.loads(model["metadata_json"] or "{}")
        if metadata.get("is_cloud") or model["name"].casefold().endswith(":cloud"):
            raise HTTPException(400,"Cloud models are not enabled for the local autonomous agent")
        return dict(model),None
    endpoint=db.execute("SELECT id,name,runtime,endpoint_url,external_model_id FROM model_endpoints WHERE id=? AND is_enabled=1",(model_id,)).fetchone()
    if endpoint: return None,dict(endpoint)
    raise HTTPException(400,"Selected model is not currently available in the local model registry")


class InferenceFailure(Exception):
    def __init__(self, category, detail):
        self.category=category
        self.detail=detail or "No additional error details were provided."
        super().__init__(self.detail)


def _safe_endpoint(value):
    try:
        parsed=urlsplit(str(value))
        host=parsed.hostname or "unknown-host"
        if parsed.port: host+=f":{parsed.port}"
        return urlunsplit((parsed.scheme,host,parsed.path,"",""))
    except (ValueError, TypeError):
        return "<invalid endpoint>"


def _safe_error_detail(value):
    detail=re.sub(r"(?i)(bearer\s+)[^\s,;]+",r"\1[redacted]",str(value or ""))
    detail=re.sub(r"(?i)(password|token|api[_-]?key|secret)(\s*[=:]\s*)[^\s,;]+",r"\1\2[redacted]",detail)
    return detail[:500] or "No response details were provided."


async def _complete_metrics(model, endpoint, prompt, profile="default"):
    runtime="unknown"; name="unknown"; target="<unresolved>"; status=None; started=time.monotonic()
    try:
        settings=_inference_settings(profile)
        if endpoint:
            url=endpoint["endpoint_url"].rstrip("/"); runtime=endpoint["runtime"]; name=endpoint["external_model_id"] or ""
        elif model:
            runtime=model["runtime"]; name=model["name"]
            if runtime=="ollama": url=os.environ.get("OLLAMA_URL","http://127.0.0.1:11434").rstrip("/")
            else: url=os.environ.get("CODING_LLAMA_CPP_URL" if "coder" in name.casefold() else "LLAMA_CPP_URL","http://127.0.0.1:8081").rstrip("/")
        else:
            raise ValueError("No local model or endpoint was selected")
        if runtime=="ollama": target=url+"/api/generate"
        else: target=url+"/v1/chat/completions"
        safe_target=_safe_endpoint(target)
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings["read_timeout_seconds"],connect=settings["connect_timeout_seconds"])) as client:
            if runtime=="ollama":
                response=await client.post(target,json={"model":name,"prompt":prompt,"stream":False,"keep_alive":settings["keep_alive"],"options":{"temperature":0.1,"num_predict":settings["max_generated_tokens"]}})
            else:
                response=await client.post(target,json={"model":name or "local-model","messages":[{"role":"system","content":"Return one valid JSON object only. Do not use markdown fences. Do not claim that code was changed or executed."},{"role":"user","content":prompt}],"temperature":0.1,"max_tokens":settings["max_generated_tokens"]})
            status=response.status_code
            response.raise_for_status()
            try: payload=response.json()
            except (ValueError, json.JSONDecodeError) as exc: raise InferenceFailure("invalid_json", "The local runtime returned invalid JSON.") from exc
        if not isinstance(payload,dict): raise InferenceFailure("unexpected_response", "The local runtime response must be a JSON object.")
        if runtime=="ollama":
            text=payload.get("response")
            if not isinstance(text,str) or not text.strip(): raise InferenceFailure("unexpected_response", "Ollama response is missing non-empty 'response' text.")
            result={"text":text,"generated_tokens":payload.get("eval_count"),"generation_duration_ns":payload.get("eval_duration")}
        else:
            choices=payload.get("choices")
            if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict): raise InferenceFailure("unexpected_response", "The local runtime response has no valid completion choice.")
            message=choices[0].get("message") or {}
            text=message.get("content") if isinstance(message,dict) else None
            if not isinstance(text,str) or not text.strip(): raise InferenceFailure("unexpected_response", "The local runtime completion is empty or malformed.")
            usage=payload.get("usage") or {}
            result={"text":text,"generated_tokens":usage.get("completion_tokens") if isinstance(usage,dict) else None,"generation_duration_ns":None}
        logger.info("Agent inference completed",extra={"profile":profile,"model_name":name,"runtime":runtime,"endpoint":safe_target,"request_duration_ms":round((time.monotonic()-started)*1000),"http_status":status,"generated_tokens":result.get("generated_tokens")})
        return result
    except InferenceFailure as exc:
        logger.exception("Agent inference failed",extra={"error_class":type(exc).__name__,"failure_category":exc.category,"model_name":name,"runtime":runtime,"endpoint":_safe_endpoint(target),"request_duration_ms":round((time.monotonic()-started)*1000),"http_status":status,"safe_details":_safe_error_detail(exc.detail)})
        raise
    except httpx.TimeoutException as exc:
        detail="The local model request timed out."
        logger.exception("Agent inference timed out",extra={"error_class":type(exc).__name__,"failure_category":"timeout","model_name":name,"runtime":runtime,"endpoint":_safe_endpoint(target),"request_duration_ms":round((time.monotonic()-started)*1000),"http_status":status,"safe_details":detail})
        raise InferenceFailure("timeout",detail) from exc
    except httpx.ConnectError as exc:
        detail="Could not connect to the local model runtime. Confirm it is running and its endpoint is reachable."
        logger.exception("Agent inference connection failed",extra={"error_class":type(exc).__name__,"failure_category":"connection","model_name":name,"runtime":runtime,"endpoint":_safe_endpoint(target),"request_duration_ms":round((time.monotonic()-started)*1000),"http_status":status,"safe_details":_safe_error_detail(exc)})
        raise InferenceFailure("connection",detail) from exc
    except httpx.HTTPStatusError as exc:
        status=exc.response.status_code
        detail=_safe_error_detail(exc.response.text)
        logger.exception("Agent inference returned HTTP error",extra={"error_class":type(exc).__name__,"failure_category":"http_status","model_name":name,"runtime":runtime,"endpoint":_safe_endpoint(target),"request_duration_ms":round((time.monotonic()-started)*1000),"http_status":status,"safe_details":detail})
        raise InferenceFailure("http_status",f"Local runtime returned HTTP {status}: {detail}") from exc
    except httpx.HTTPError as exc:
        detail=_safe_error_detail(exc)
        logger.exception("Agent HTTP inference failed",extra={"error_class":type(exc).__name__,"failure_category":"http_error","model_name":name,"runtime":runtime,"endpoint":_safe_endpoint(target),"request_duration_ms":round((time.monotonic()-started)*1000),"http_status":status,"safe_details":detail})
        raise InferenceFailure("http_error",detail) from exc
    except Exception as exc:
        detail=_safe_error_detail(exc) if str(exc).strip() else f"{type(exc).__name__} occurred without an error message."
        logger.exception("Unexpected agent inference failure",extra={"error_class":type(exc).__name__,"failure_category":"unexpected","model_name":name,"runtime":runtime,"endpoint":_safe_endpoint(target),"request_duration_ms":round((time.monotonic()-started)*1000),"http_status":status,"safe_details":detail})
        raise InferenceFailure("unexpected",detail) from exc

async def _complete(model, endpoint, prompt, profile="default"):
    result=await _complete_metrics(model,endpoint,prompt,profile=profile)
    text=result.get("text") if isinstance(result,dict) else None
    if not isinstance(text,str) or not text.strip(): raise InferenceFailure("unexpected_response","The local model returned an empty completion.")
    return text


def _parse_json(text):
    text=text.strip()
    if text.startswith("```"): text=text.split("\n",1)[-1].rsplit("```",1)[0].strip()
    try: return json.loads(text)
    except json.JSONDecodeError:
        start=text.find("{"); end=text.rfind("}")
        if start>=0 and end>start: return json.loads(text[start:end+1])
        raise


def _validate_plan(plan):
    required={"objective","requirements","assumptions","technology_stack","architecture_overview","modules","tasks","risks","estimated_complexity"}
    if not isinstance(plan,dict) or not required.issubset(plan): raise ValueError("Plan is missing required fields")
    for key in ("requirements","assumptions","technology_stack","modules","tasks","risks"):
        if not isinstance(plan[key],list): raise ValueError(f"{key} must be an array")
    if not plan["tasks"] or len(plan["tasks"])>80: raise ValueError("Plan must contain 1 to 80 tasks")
    flat=[]; ids=set()
    def collect(items,parent_id=None):
        if not isinstance(items,list): raise ValueError("Tasks and subtasks must be arrays")
        for task in items:
            if not isinstance(task,dict) or not str(task.get("title","")).strip(): raise ValueError("Each task requires a title")
            task_id=str(task.get("id") or f"task-{len(flat)+1}")
            if task_id in ids: raise ValueError("Task identifiers must be unique")
            task["id"]=task_id; task["depends_on"]=task.get("depends_on",[]); task["acceptance_criteria"]=task.get("acceptance_criteria",[]); task["subtasks"]=task.get("subtasks",[]); task["parent_id"]=parent_id
            if not isinstance(task["depends_on"],list) or not isinstance(task["acceptance_criteria"],list): raise ValueError("Task dependencies and acceptance criteria must be arrays")
            ids.add(task_id); flat.append(task)
            collect(task["subtasks"],task_id)
            if len(flat)>80: raise ValueError("Plan must contain no more than 80 tasks and subtasks")
    collect(plan["tasks"])
    for task in flat:
        if task["id"] in task["depends_on"] or any(dep not in ids for dep in task["depends_on"]): raise ValueError("Plan includes an invalid task dependency")
    graph={task["id"]:task["depends_on"] for task in flat}
    visiting=set(); visited=set()
    def visit(task_id):
        if task_id in visiting: raise ValueError("Plan task dependencies contain a cycle")
        if task_id in visited: return
        visiting.add(task_id)
        for dependency in graph[task_id]: visit(dependency)
        visiting.remove(task_id); visited.add(task_id)
    for task_id in graph: visit(task_id)
    verification_command = plan.get("verification_command", "")
    if not isinstance(verification_command, str): raise ValueError("verification_command must be a string")
    plan["verification_command"] = verification_command.strip()[:4000]
    structure=plan.get("project_structure",[])
    if not isinstance(structure,list) or len(structure)>160:
        raise ValueError("project_structure must be an array with no more than 160 entries")
    normalized_structure=[]
    for item in structure:
        if isinstance(item,str): item={"path":item,"purpose":""}
        if not isinstance(item,dict) or not isinstance(item.get("path"),str) or not item["path"].strip():
            raise ValueError("Each project_structure entry requires a relative path")
        path=item["path"].replace("\\","/").strip().strip("/")
        if any(part==".." for part in path.split("/")) or ":" in path:
            raise ValueError("project_structure paths must stay inside the project")
        purpose=item.get("purpose","")
        if not isinstance(purpose,str): purpose=""
        normalized_structure.append({"path":path,"purpose":purpose[:300]})
    plan["project_structure"]=normalized_structure
    commands=plan.get("commands",[])
    if not isinstance(commands,list) or len(commands)>12:
        raise ValueError("commands must be an array with no more than 12 entries")
    normalized_commands=[]
    for item in commands:
        if not isinstance(item,dict) or not isinstance(item.get("command"),str) or not item["command"].strip():
            raise ValueError("Each suggested command requires a command string")
        reason=item.get("reason","")
        normalized_commands.append({"command":item["command"].strip()[:4000],"reason":reason[:500] if isinstance(reason,str) else ""})
    plan["commands"]=normalized_commands
    return plan


def _simple_file_plan(requirement):
    """Build a small plan for a clear single-file request without relying on model JSON."""
    request=str(requirement or "").strip()
    filename_match=re.search(r"(?i)([a-z0-9_.-]+\.[a-z0-9_-]{1,10})\b",request)
    create_request=bool(re.search(r"(?i)\b(create|make|write|add|generate)\b",request))
    edit_request=bool(re.search(r"(?i)\b(edit|modify|change|update|remove|delete|fix|refactor|clean)\b",request))
    if not filename_match or not (create_request or edit_request):
        return None
    filename=filename_match.group(1)
    description=request.rstrip(" .")
    operation="Create" if create_request and not edit_request else "Update"
    acceptance=(
        f"A Python file named {filename} exists in the project. The file implements the requested behavior and can be run with Python."
        if operation=="Create" and filename.casefold().endswith(".py")
        else f"The existing file {filename} reflects the requested changes and preserves unrelated behavior."
    )
    task={
        "id":"update-requested-file" if operation=="Update" else "create-requested-file",
        "title":f"{operation} {filename}",
        "description":description,
        "depends_on":[],
        "acceptance_criteria":[acceptance],
        "subtasks":[],
    }
    return _validate_plan({
        "objective":description,
        "requirements":[description],
        "assumptions":["The file should be created at the project root unless a directory is specified."],
        "technology_stack":["Python"],
        "architecture_overview":f"The requested change is limited to {filename}.",
        "modules":[filename],
        "tasks":[task],
        "risks":[],
        "estimated_complexity":"low",
        "project_structure":[{"path":filename,"purpose":"File targeted by the requested change."}],
        "verification_command":f"python {filename}" if operation=="Create" and filename.casefold().endswith(".py") else "",
        "commands":[],
    })


def _project_evidence(root):
    status=intelligence.get_status(root)
    if status.get("status")!="indexed":
        scan=intelligence.scan_and_index(root); return scan["summary"]
    return status.get("summary") or {}


async def _generate_plan_internal(session_id, requirement, model_id, request, stored_root=None):
    session=get_session(session_id); root=Path(session["project_root"]).resolve()
    if session["mode"]!="planning": raise HTTPException(409,"This session is in Ask mode; create a Planning session to generate a plan")
    if stored_root is None:
        if root!=root_for(request): raise HTTPException(404,"Agent session not found for this workspace")
    elif root!=Path(stored_root).resolve() or not root.is_dir():
        raise HTTPException(409,"The project's stored folder is unavailable or no longer valid")
    if session["status"] in {"paused","cancelled"}: raise HTTPException(409,f"Session is {session['status']}")
    if len(requirement.strip())<5: raise HTTPException(422,"Please provide a clearer software requirement")
    words=requirement.casefold().split()
    if len(words)<4 or " ".join(words) in {"build an app","create an app","make an app"}:
        questions=["What should the software or feature do for its users?","Which part of this project should change, if you know?"]
        with connect() as db:
            db.execute("UPDATE agent_sessions SET status='awaiting_input',requirement=?,model_id=COALESCE(?,model_id),updated_at=? WHERE id=?",(requirement.strip(),model_id,now(),session_id))
            db.execute("INSERT INTO agent_messages VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),session_id,"user",requirement.strip(),model_id,now()))
            event(db,session_id,"clarification_requested","More detail is needed before planning",{"questions":questions})
        return {"session":get_session(session_id),"clarification_questions":questions,"tasks":[]}
    evidence=await run_in_threadpool(_project_evidence,root)
    tree=await run_in_threadpool(lambda:[path.relative_to(root).as_posix() for path in intelligence.enumerate_files(root)])
    source_context=await run_in_threadpool(intelligence.context,root,requirement.strip(),5500)
    with connect() as db:
        model,endpoint=_selected_model(db,model_id or session.get("model_id")); actual_id=(model or endpoint)["id"]
        db.execute("UPDATE agent_sessions SET status='planning',requirement=?,model_id=?,updated_at=?,error='' WHERE id=?",(requirement.strip(),actual_id,now(),session_id)); event(db,session_id,"planning_started","Project context collected; requesting a structured plan",{"indexed_files":evidence.get("file_count")})
    prompt="Create a practical development plan grounded in the supplied files and requirement. Include JSON fields: objective, requirements (string array), assumptions (string array), technology_stack (string array), architecture_overview, modules (array), project_structure (array of {path,purpose} describing proposed folders and key files), tasks (array of {id,title,description,depends_on:string[],acceptance_criteria:string[]}), risks (string array), estimated_complexity, verification_command (supported command or empty), and commands (array of {command,reason} for necessary setup/start/test actions, empty if none). Base current architecture and command choices on the supplied tree and excerpts. Mark uncertainties as assumptions. Never claim that implementation or a command execution already occurred.\n\nREQUIREMENT:\n"+requirement.strip()+"\n\nCURRENT PROJECT SUMMARY:\n"+json.dumps(evidence,ensure_ascii=False)[:3500]+"\n\nCURRENT FILE TREE:\n"+json.dumps(tree,ensure_ascii=False)[:3000]+"\n\nRELEVANT SOURCE EXCERPTS:\n"+source_context.get("context","")[:4000]
    plan=_simple_file_plan(requirement)
    if plan is None:
        try:
            raw=await _complete(model,endpoint,prompt,profile="planning")
            try: plan=_validate_plan(_parse_json(raw))
            except (ValueError,json.JSONDecodeError):
                try:
                    repair=await _complete(model,endpoint,"Repair the following into the required valid JSON plan schema. Preserve only supplied facts. Include project_structure as an array of {path,purpose} and commands as an array of {command,reason}. JSON only.\n"+raw[:12000],profile="planning")
                    plan=_validate_plan(_parse_json(repair))
                except (ValueError,json.JSONDecodeError):
                    plan=_simple_file_plan(requirement)
                    if plan is None: raise
        except HTTPException: raise
        except InferenceFailure as exc:
            detail=f"{exc.category}: {exc.detail}"
            with connect() as db:
                db.execute("UPDATE agent_sessions SET status='failed',error=?,updated_at=? WHERE id=?",(detail[:500],now(),session_id))
                event(db,session_id,"plan_failed","Local model failed to generate a plan",{"category":exc.category})
            raise HTTPException(502,f"Local planning failed [{exc.category}]: {exc.detail}") from exc
        except (httpx.HTTPError,ValueError,json.JSONDecodeError) as exc:
            with connect() as db: db.execute("UPDATE agent_sessions SET status='failed',error=?,updated_at=? WHERE id=?",(str(exc)[:500],now(),session_id)); event(db,session_id,"plan_failed","Local model failed to return a valid plan",{})
            raise HTTPException(502,"Local planning failed. Check the selected model/runtime and retry.")
    latest=get_session(session_id)
    if latest["status"] in {"paused","cancelled"}:
        with connect() as db: event(db,session_id,"planning_interrupted",f"Plan generation stopped because the session was {latest['status']}",{})
        raise HTTPException(409,f"Session is {latest['status']}; generated plan was not stored")
    plan_id=str(uuid.uuid4())
    with connect() as db:
        db.execute("DELETE FROM agent_plans WHERE session_id=?",(session_id,))
        db.execute("INSERT INTO agent_plans VALUES(?,?,?,'waiting_for_approval',?,?)",(plan_id,session_id,json.dumps(plan),now(),now()))
        flat=[]
        def flatten(items):
            for task in items:
                flat.append(task); flatten(task["subtasks"])
        flatten(plan["tasks"])
        task_ids={task["id"]:str(uuid.uuid4()) for task in flat}
        for pos,task in enumerate(flat):
            tid=task_ids[task["id"]]
            parent_id=task_ids.get(task.get("parent_id"))
            db.execute("INSERT INTO agent_tasks VALUES(?,?,?,?,?,'pending',?,?)",(tid,plan_id,parent_id,task["title"],task.get("description", ""),json.dumps(task.get("acceptance_criteria",[])),pos))
            for dep in task["depends_on"]: db.execute("INSERT INTO agent_task_dependencies VALUES(?,?)",(tid,task_ids[dep]))
        db.execute("UPDATE agent_sessions SET status='waiting_for_approval',updated_at=?,error='' WHERE id=?",(now(),session_id)); event(db,session_id,"plan_ready","Structured plan is ready for review",{"task_count":len(flat),"model_id":actual_id})
    return {"session":get_session(session_id),"plan":plan,"tasks":get_tasks(session_id)}


async def generate_plan_internal(session_id, requirement, model_id, request, stored_root=None):
    with PLAN_GENERATION_LOCK:
        if session_id in PLAN_GENERATION_ACTIVE: raise HTTPException(409,"Plan generation is already running for this session")
        PLAN_GENERATION_ACTIVE.add(session_id)
    try:
        return await _generate_plan_internal(session_id,requirement,model_id,request,stored_root=stored_root)
    finally:
        with PLAN_GENERATION_LOCK: PLAN_GENERATION_ACTIVE.discard(session_id)


@router.post("/sessions/{session_id}/plan",response_model=PlanResponse)
async def create_plan(session_id: str, data: Requirement, request: Request):
    session=get_session(session_id)
    if session["status"]=="planning": raise HTTPException(409,"Plan generation is already running for this session")
    return await generate_plan_internal(session_id,data.requirement,data.model_id,request)


@router.post("/sessions/{session_id}/plan/retry",response_model=PlanResponse)
async def retry_plan(session_id: str, request: Request):
    session=get_session(session_id)
    if session["mode"]!="planning": raise HTTPException(409,"Only Planning sessions can retry plan generation")
    if session["status"]!="failed": raise HTTPException(409,f"Retry requires a failed session; current status is {session['status']}")
    if not session.get("requirement","").strip(): raise HTTPException(409,"This session has no saved requirement to retry")
    root=Path(session["project_root"]).expanduser().resolve()
    if not root.is_dir(): raise HTTPException(409,"The project's stored folder is unavailable; select the project again before retrying")
    with PLAN_GENERATION_LOCK:
        if session_id in PLAN_GENERATION_ACTIVE: raise HTTPException(409,"Plan generation is already running for this session")
        PLAN_GENERATION_ACTIVE.add(session_id)
    try:
        with connect() as db:
            if db.execute("SELECT 1 FROM agent_plans WHERE session_id=?",(session_id,)).fetchone():
                raise HTTPException(409,"This session already has a saved plan; it will not be replaced")
            updated=db.execute("UPDATE agent_sessions SET status='planning',error='',updated_at=? WHERE id=? AND status='failed'",(now(),session_id))
            if updated.rowcount!=1: raise HTTPException(409,"Session state changed; refresh it before retrying")
            event(db,session_id,"planning_retry_requested","User explicitly retried plan generation",{"project_root":str(root)})
        return await _generate_plan_internal(session_id,session["requirement"],session.get("model_id"),request,stored_root=root)
    except Exception as exc:
        with connect() as db:
            current=db.execute("SELECT status FROM agent_sessions WHERE id=?",(session_id,)).fetchone()
            if current and current["status"]=="planning":
                detail=(exc.detail if isinstance(exc,HTTPException) else str(exc))
                db.execute("UPDATE agent_sessions SET status='failed',error=?,updated_at=? WHERE id=?",(str(detail)[:500],now(),session_id))
                event(db,session_id,"planning_retry_failed","Plan retry failed",{"error":str(detail)[:500]})
        raise
    finally:
        with PLAN_GENERATION_LOCK: PLAN_GENERATION_ACTIVE.discard(session_id)

@router.post("/sessions/{session_id}/ask",response_model=AnswerResponse)
async def ask_project(session_id: str, data: Requirement, request: Request):
    stage="session_lookup"; started=time.monotonic(); model=None; endpoint=None; selected=None
    session=get_session(session_id)
    if session["mode"]!="ask": raise HTTPException(409,"This session is in Planning mode; create an Ask session to ask project questions")
    root=root_for(request)
    if Path(session["project_root"]).resolve()!=root: raise HTTPException(404,"Agent session not found for this workspace")
    if session["status"] in {"paused","cancelled"}: raise HTTPException(409,f"Session is {session['status']}")
    try:
        stage="project_evidence"
        evidence=await run_in_threadpool(_project_evidence,root)
        if not isinstance(evidence,dict): raise ValueError("Project evidence must be an object")
        stage="context_retrieval"
        ctx=await run_in_threadpool(intelligence.context,root,data.requirement,7000)
        if not isinstance(ctx,dict) or not isinstance(ctx.get("context"),str) or not isinstance(ctx.get("items"),list): raise ValueError("Project context response is malformed")
        stage="model_selection"
        with connect() as db:
            model,endpoint=_selected_model(db,data.model_id or session.get("model_id")); selected=(model or endpoint)["id"]
            db.execute("UPDATE agent_sessions SET requirement=?,model_id=?,updated_at=? WHERE id=?",(data.requirement,selected,now(),session_id))
            db.execute("INSERT INTO agent_messages VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),session_id,"user",data.requirement,selected,now()))
            event(db,session_id,"question","Project question submitted",{"question":data.requirement})
        logger.info("Agent Ask context ready",extra={"stage":"prompt_ready","session_id":session_id,"model_id":selected,"model_name":(model or endpoint).get("name"),"runtime":(model or endpoint).get("runtime"),"evidence_files":evidence.get("file_count"),"context_items":len(ctx["items"]),"context_characters":len(ctx["context"])})
        stage="prompt_build"
        prompt="Answer the user's project question using only the supplied project evidence. Treat the indexed project summary as a discovery hint that may contain false positives; base claims about architecture, purpose, frameworks, and storage on retrieved source excerpts or explicit documentation. Cite each factual point with an exact source path and line range in the form [path:line_start-line_end]. If the supplied excerpts do not establish a fact, say that it is unclear instead of guessing. Do not claim files were modified or commands were run.\nPROJECT SUMMARY (discovery hints):\n"+json.dumps(evidence)[:7000]+"\nRETRIEVED SOURCE EXCERPTS (preferred evidence):\n"+ctx["context"]+"\nQUESTION:\n"+data.requirement
        stage="inference"
        answer=await _complete(model,endpoint,prompt,profile="ask")
        invalid = _invalid_citations(answer, ctx["items"])
        if invalid:
            stage="citation_repair"
            allowed = ", ".join(f"[{i['path']}:{i['line_start']}-{i['line_end']}]" for i in ctx["items"])
            answer = await _complete(model,endpoint,
                "Revise the answer using only the supplied source excerpts. Every citation must exactly match one of these retrieved ranges: "
                + (allowed or "none")
                + ". Remove claims that cannot be supported. If evidence is insufficient, state that clearly.\n\nEXCERPTS:\n"
                + ctx["context"] + "\n\nANSWER TO REVISE:\n" + answer,
                profile="ask")
            invalid = _invalid_citations(answer, ctx["items"])
            if invalid:
                answer = ("I could not produce an answer with citations that match the retrieved source lines. "
                          "Please inspect the retrieved evidence before relying on a project-specific claim.")
    except HTTPException:
        raise
    except InferenceFailure as exc:
        with connect() as db:
            db.execute("UPDATE agent_sessions SET status='failed',error=?,updated_at=? WHERE id=?",(f"{exc.category}: {exc.detail}"[:500],now(),session_id))
            event(db,session_id,"ask_failed","Local model request failed",{"category":exc.category})
        raise HTTPException(502,f"Local model request failed [{exc.category}]: {exc.detail}") from exc
    except Exception as exc:
        detail=_safe_error_detail(exc) if str(exc).strip() else f"{type(exc).__name__} occurred without an error message."
        logger.exception("Agent Ask request failed before or during inference",extra={"error_class":type(exc).__name__,"failure_category":"ask_lifecycle","stage":stage,"session_id":session_id,"model_name":(model or endpoint or {}).get("name"),"runtime":(model or endpoint or {}).get("runtime"),"request_duration_ms":round((time.monotonic()-started)*1000),"safe_details":detail})
        raise HTTPException(502,f"Agent Ask failed during {stage}: {detail}") from exc
    with connect() as db:
        db.execute("UPDATE agent_sessions SET status='ready',updated_at=?,error='' WHERE id=?",(now(),session_id)); event(db,session_id,"answer","Local model answered using retrieved project context",{"context_items":len(ctx["items"])})
        db.execute("INSERT INTO agent_messages VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),session_id,"assistant",answer,selected,now()))
    return {"session":get_session(session_id),"answer":answer,"context":ctx}


@router.get("/sessions/{session_id}/plan",response_model=PlanDetailResponse)
def read_plan(session_id: str, request: Request):
    get_session(session_id)
    with connect() as db:
        row=db.execute("SELECT * FROM agent_plans WHERE session_id=?",(session_id,)).fetchone()
        if not row: raise HTTPException(404,"No plan exists for this session")
    return {**dict(row),"plan":json.loads(row["plan_json"]),"tasks":get_tasks(session_id)}


def get_tasks(session_id):
    with connect() as db:
        rows=db.execute("SELECT t.*, (SELECT json_group_array(d.depends_on_task_id) FROM agent_task_dependencies d WHERE d.task_id=t.id) dependencies_json FROM agent_tasks t JOIN agent_plans p ON p.id=t.plan_id WHERE p.session_id=? ORDER BY t.position",(session_id,)).fetchall()
    return [{**dict(r),"acceptance_criteria":json.loads(r["acceptance_criteria_json"]),"dependencies":json.loads(r["dependencies_json"])} for r in rows]


@router.post("/sessions/{session_id}/approve-plan")
def approve(session_id: str, data: PlanApproval, request: Request):
    session=get_session(session_id)
    if Path(session["project_root"]).resolve()!=root_for(request): raise HTTPException(404,"Agent session not found for this workspace")
    with connect() as db:
        plan=db.execute("SELECT id,status FROM agent_plans WHERE session_id=?",(session_id,)).fetchone()
        if not plan: raise HTTPException(404,"No plan exists for this session")
        if plan["status"]!="waiting_for_approval": raise HTTPException(409,"Plan is no longer awaiting approval")
        status="approved" if data.approved else "rejected"; db.execute("UPDATE agent_plans SET status=?,updated_at=? WHERE id=?",(status,now(),plan["id"]))
        session_status="plan_approved" if data.approved else "plan_rejected"; db.execute("UPDATE agent_sessions SET status=?,updated_at=? WHERE id=?",(session_status,now(),session_id)); event(db,session_id,session_status,"Plan approved" if data.approved else "Plan rejected",{})
    return {"session":get_session(session_id),"plan_status":status}


def _changeset_for_session(request: Request, session_id: str, changeset_id: str):
    session=session_get(session_id,request)
    try: manifest=coding_changes.load(_change_storage(request),changeset_id)
    except FileNotFoundError: raise HTTPException(404,"Change proposal not found")
    except ValueError as exc: raise HTTPException(400,str(exc))
    if manifest.get("session_id")!=session_id or Path(manifest.get("root","")).resolve()!=root_for(request):
        raise HTTPException(404,"Change proposal not found for this project")
    return session,manifest


def _ordered_approved_plan_tasks(plan: dict, persisted_tasks: list[dict]) -> list[dict]:
    flattened = []
    def flatten(items):
        for task in items:
            flattened.append(task)
            flatten(task.get("subtasks", []))
    flatten(plan.get("tasks", []))
    if not flattened or len(flattened) != len(persisted_tasks):
        raise HTTPException(409, "The approved plan tasks are missing or no longer match the session task records")
    plan_to_db = {}
    db_by_plan_id = {}
    for source, stored in zip(flattened, persisted_tasks):
        if source["title"].strip() != stored["title"].strip():
            raise HTTPException(409, "The approved plan no longer matches its persisted task records")
        plan_to_db[source["id"]] = stored["id"]
        db_by_plan_id[source["id"]] = stored
    for source in flattened:
        expected = {plan_to_db[dependency] for dependency in source["depends_on"]}
        if set(db_by_plan_id[source["id"]]["dependencies"]) != expected:
            raise HTTPException(409, "Persisted task dependencies do not match the approved plan")
    completed = set()
    ordered = []
    while len(ordered) < len(flattened):
        ready = next((task for task in flattened if task["id"] not in completed and set(task["depends_on"]).issubset(completed)), None)
        if ready is None:
            raise HTTPException(409, "The approved plan tasks contain an unresolved dependency cycle")
        ordered.append(ready)
        completed.add(ready["id"])
    return ordered


@router.post("/sessions/{session_id}/changes/generate")
async def generate_changes(session_id: str, data: ChangeGenerationRequest, request: Request):
    session=session_get(session_id,request)
    if session["mode"]!="planning": raise HTTPException(409,"File changes require an approved planning session")
    if session["status"]!="plan_approved" or session.get("plan_status")!="approved": raise HTTPException(409,"Approve the plan before generating file changes")
    with CHANGE_GENERATION_LOCK:
        if session_id in CHANGE_GENERATION_ACTIVE: raise HTTPException(409,"File changes are already being generated for this session")
        CHANGE_GENERATION_ACTIVE.add(session_id)
    try:
        root=root_for(request)
        with connect() as db:
            plan_row=db.execute("SELECT plan_json,status FROM agent_plans WHERE session_id=?",(session_id,)).fetchone()
            coding_role=db.execute("SELECT model_id FROM model_capability_assignments WHERE capability='coding'").fetchone()
            model_id=data.model_id or (coding_role[0] if coding_role else None) or session.get("model_id")
            model,endpoint=_selected_model(db,model_id)
        if not plan_row or plan_row["status"]!="approved": raise HTTPException(409,"An approved plan is required")
        plan=json.loads(plan_row["plan_json"])
        persisted_tasks=await run_in_threadpool(get_tasks,session_id)
        ordered_tasks=_ordered_approved_plan_tasks(plan,persisted_tasks)
        task_lines=[{"id":task["id"],"title":task["title"],"description":task.get("description",""),"acceptance_criteria":task.get("acceptance_criteria",[]),"depends_on":task["depends_on"]} for task in ordered_tasks]
        task_ids={task["id"] for task in task_lines}
        await run_in_threadpool(_project_evidence,root)
        query="\n".join([plan.get("objective","")]+[t["title"]+" "+t["description"] for t in task_lines])
        context=await run_in_threadpool(intelligence.context,root,query[:500],12000)
        prompt=("Implement the approved plan as a coordinated, reviewable set of complete text-file operations. Return one JSON object with a 'changes' array. Each item has operation (create|modify|delete|rename), path (project-relative), optional new_path for rename, content for create/modify, summary, and task_ids. Implement tasks in the supplied dependency order. Include only files necessary to implement the tasks, preserve unrelated code, use only APIs and dependencies supported by source evidence, and do not include markdown fences or commands. For modification, return the complete new file content. Use operation=modify for every path that already exists in the source evidence. Preserve all existing behavior while implementing the original requirement and its acceptance criteria. Do not make changes outside the selected project.\nORIGINAL USER REQUIREMENT:\n"
                + session["requirement"]+"\nAPPROVED PLAN:\n"
                + json.dumps({"objective":plan.get("objective"),"requirements":plan.get("requirements"),"tasks":task_lines},ensure_ascii=False)
                + "\nRETRIEVED SOURCE EVIDENCE:\n"+context.get("context", ""))
        raw=await _complete(model,endpoint,prompt,profile="coding")
        payload=_parse_json(raw)
        changes=payload.get("changes") if isinstance(payload,dict) else None
        if not isinstance(changes,list) or not changes: raise ValueError("The coding model did not return a non-empty changes array")
        for item in changes:
            if not isinstance(item,dict): raise ValueError("Every proposed file operation must be an object")
            if item.get("operation")=="create" and isinstance(item.get("path"),str):
                # The filesystem is authoritative when a local model confuses a new
                # file with an existing target; turn it into a reviewable modification.
                existing=coding_changes.resolve(root,item["path"])
                if existing.exists():
                    item["operation"]="modify"
                    item["summary"]=(str(item.get("summary", ""))+" (existing file detected; reviewed as a modification)")[:500]
            item["task_ids"]=[task_id for task_id in item.get("task_ids",[]) if task_id in task_ids] if isinstance(item.get("task_ids",[]),list) else []
        proposal=await run_in_threadpool(coding_changes.prepare,root,changes,_change_storage(request),session_id=session_id,unsaved_paths=data.unsaved_paths)
    except HTTPException: raise
    except (ValueError,json.JSONDecodeError) as exc: raise HTTPException(422,f"Generated changes were rejected: {exc}") from exc
    except InferenceFailure as exc: raise HTTPException(502,f"Local coding generation failed [{exc.category}]: {exc.detail}") from exc
    except Exception as exc:
        logger.exception("Coding changeset generation failed",extra={"session_id":session_id})
        raise HTTPException(502,"Local coding generation failed. Check the selected local model and retry.") from exc
    else:
        with connect() as db: event(db,session_id,"changeset_created","Coordinated file changes are ready for review",{"changeset_id":proposal["id"],"file_count":len(proposal["changes"]),"model_id":model_id,"task_ids":sorted({task_id for change in proposal["changes"] for task_id in change["task_ids"]})})
        return proposal
    finally:
        with CHANGE_GENERATION_LOCK: CHANGE_GENERATION_ACTIVE.discard(session_id)


@router.get("/sessions/{session_id}/changes")
def list_session_changes(session_id: str, request: Request):
    session=session_get(session_id,request); root=root_for(request); storage=_change_storage(request)
    try: recovered=coding_changes.recover_interrupted(storage,root)
    except (OSError,ValueError,UnicodeError) as exc:
        raise HTTPException(409,f"Interrupted changeset recovery needs attention: {exc}") from exc
    if recovered:
        with connect() as db:
            for changeset_id in recovered: event(db,session_id,"changeset_recovered","Recovered an interrupted file apply",{"changeset_id":changeset_id})
    if not storage.exists(): return []
    results=[]
    for path in storage.glob("*.json"):
        try: manifest=coding_changes.load(storage,path.stem)
        except (OSError,json.JSONDecodeError,ValueError): continue
        if manifest.get("session_id")==session_id and Path(manifest.get("root","")).resolve()==root: results.append(coding_changes._public(manifest))
    return sorted(results,key=lambda item:item["created_at"],reverse=True)


@router.post("/sessions/{session_id}/changes/{changeset_id}/review-all")
def review_all_changes(session_id: str, changeset_id: str, data: ReviewAllChanges, request: Request):
    _changeset_for_session(request,session_id,changeset_id)
    try: result=coding_changes.review_all(_change_storage(request),changeset_id,data.decision)
    except ValueError as exc: raise HTTPException(409,str(exc)) from exc
    with connect() as db: event(db,session_id,"changeset_reviewed_all",f"{data.decision.title()} all pending file changes",{"changeset_id":changeset_id})
    return result


@router.post("/sessions/{session_id}/changes/{changeset_id}/{change_id}/review")
def review_file_change(session_id: str, changeset_id: str, change_id: str, data: FileChangeDecision, request: Request):
    _changeset_for_session(request,session_id,changeset_id)
    try: result=coding_changes.review(_change_storage(request),changeset_id,change_id,data.decision)
    except KeyError as exc: raise HTTPException(404,"File change not found") from exc
    except ValueError as exc: raise HTTPException(409,str(exc)) from exc
    with connect() as db: event(db,session_id,"file_change_reviewed",f"File change {data.decision}",{"changeset_id":changeset_id,"change_id":change_id})
    return result


def _snapshot_agent_preimage(root: Path, relative_path: str, changeset_id: str):
    target=coding_changes.resolve(root,relative_path,allow_missing=False)
    raw=target.read_bytes(); version_id=str(uuid.uuid4())
    with connect() as db:
        db.execute("INSERT INTO coding_file_versions(id,workspace_path,relative_path,content,size,created_at,reason) VALUES(?,?,?,?,?,?,?)",(version_id,str(root),relative_path,raw,len(raw),now(),f"agent:{changeset_id}"))


@router.post("/sessions/{session_id}/changes/{changeset_id}/apply")
def apply_file_changes(session_id: str, changeset_id: str, data: FileOperationGuard, request: Request):
    session,_=_changeset_for_session(request,session_id,changeset_id)
    if session["status"]!="plan_approved" or session.get("plan_status")!="approved":
        raise HTTPException(409,"The approved plan is paused or cancelled; resume it before applying code changes")
    try:
        result=coding_changes.apply(_change_storage(request),changeset_id,before_write=lambda root,path,_content:_snapshot_agent_preimage(root,path,changeset_id),unsaved_paths=data.unsaved_paths)
    except ValueError as exc: raise HTTPException(409,str(exc)) from exc
    except Exception as exc:
        logger.exception("Approved changeset apply failed and compensation was attempted",extra={"session_id":session_id,"changeset_id":changeset_id})
        raise HTTPException(500,"Applying the changes failed; the engine attempted to restore the captured snapshot") from exc
    with connect() as db: event(db,session_id,"changeset_applied","Approved file changes applied",{"changeset_id":changeset_id,"applied_change_ids":result["applied_change_ids"]})
    return result


@router.post("/sessions/{session_id}/changes/{changeset_id}/rollback")
def rollback_file_changes(session_id: str, changeset_id: str, data: FileOperationGuard, request: Request):
    _changeset_for_session(request,session_id,changeset_id)
    try:
        result=coding_changes.rollback(_change_storage(request),changeset_id,before_restore=lambda root,path:_snapshot_agent_preimage(root,path,changeset_id),unsaved_paths=data.unsaved_paths)
    except ValueError as exc: raise HTTPException(409,str(exc)) from exc
    except Exception as exc:
        logger.exception("Changeset rollback failed",extra={"session_id":session_id,"changeset_id":changeset_id})
        raise HTTPException(500,"Unable to complete the rollback") from exc
    with connect() as db: event(db,session_id,"changeset_rolled_back","File changes rolled back",{"changeset_id":changeset_id})
    return result


def _control(session_id, request, action):
    session=get_session(session_id)
    if Path(session["project_root"]).resolve()!=root_for(request): raise HTTPException(404,"Agent session not found for this workspace")
    allowed={"pause":{"planning","waiting_for_approval","plan_approved","ready","awaiting_input"},"resume":{"paused","failed"},"cancel":{"planning","waiting_for_approval","plan_approved","paused","ready","awaiting_input","failed"}}
    if session["status"] not in allowed[action]: raise HTTPException(409,f"Cannot {action} a session in state {session['status']}")
    state=session.get("paused_from") or "ready" if action=="resume" else "paused" if action=="pause" else "cancelled"
    with connect() as db:
        paused_from=session["status"] if action=="pause" else None
        db.execute("UPDATE agent_sessions SET status=?,paused_from=?,updated_at=? WHERE id=?",(state,paused_from,now(),session_id)); event(db,session_id,state,f"Session {state}",{})
        if action=="cancel": db.execute("UPDATE agent_tasks SET status='cancelled' WHERE plan_id IN (SELECT id FROM agent_plans WHERE session_id=?) AND status IN ('pending','in_progress','waiting_for_approval')",(session_id,))
    return get_session(session_id)


@router.post("/sessions/{session_id}/pause")
def pause(session_id: str,request: Request): return _control(session_id,request,"pause")
@router.post("/sessions/{session_id}/resume")
def resume(session_id: str,request: Request): return _control(session_id,request,"resume")
@router.post("/sessions/{session_id}/cancel")
def cancel(session_id: str,request: Request): return _control(session_id,request,"cancel")

@router.get("/sessions/{session_id}/tasks",response_model=list[TaskResponse])
def tasks(session_id: str,request: Request):
    session_get(session_id,request); return get_tasks(session_id)

@router.patch("/sessions/{session_id}/tasks/{task_id}",response_model=TaskResponse)
def update_task(session_id: str,task_id: str,data: TaskStatusUpdate,request: Request):
    session=session_get(session_id,request)
    with connect() as db:
        row=db.execute("SELECT t.status,t.plan_id,p.status plan_status FROM agent_tasks t JOIN agent_plans p ON p.id=t.plan_id WHERE t.id=? AND p.session_id=?",(task_id,session_id)).fetchone()
        if not row: raise HTTPException(404,"Task not found")
        if data.status=="in_progress":
            if row["plan_status"]!="approved" or session["status"]!="plan_approved": raise HTTPException(409,"Approve the plan before starting a task")
            pending=db.execute("SELECT COUNT(*) FROM agent_task_dependencies d JOIN agent_tasks dependency ON dependency.id=d.depends_on_task_id WHERE d.task_id=? AND dependency.status<>'completed'",(task_id,)).fetchone()[0]
            if pending: raise HTTPException(409,"Complete task dependencies before starting this task")
        if data.status=="completed" and row["status"]!="in_progress": raise HTTPException(409,"Only an in-progress task can be completed")
        db.execute("UPDATE agent_tasks SET status=? WHERE id=?",(data.status,task_id)); event(db,session_id,"task_status_changed",f"Task status changed to {data.status}",{"task_id":task_id})
    return next(task for task in get_tasks(session_id) if task["id"]==task_id)

@router.get("/sessions/{session_id}/events",response_model=list[EventResponse])
def events(session_id: str,request: Request):
    get_session(session_id)
    with connect() as db: rows=db.execute("SELECT * FROM agent_events WHERE session_id=? ORDER BY created_at",(session_id,)).fetchall()
    return [{**dict(r),"data":json.loads(r["data_json"])} for r in rows]

@router.get("/sessions/{session_id}/history")
def history(session_id: str,request: Request):
    get_session(session_id)
    with connect() as db: rows=db.execute("SELECT id,role,content,model_id,created_at FROM agent_messages WHERE session_id=? ORDER BY created_at,id",(session_id,)).fetchall()
    return [dict(row) for row in rows]

@router.get("/models/roles")
def model_roles():
    with connect() as db:
        models=[dict(r) for r in db.execute("SELECT id,name,runtime,status,source_type,metadata_json FROM model_registry WHERE status='installed' ORDER BY name COLLATE NOCASE") if not (json.loads(r["metadata_json"] or "{}").get("is_cloud") or r["name"].casefold().endswith(":cloud"))]
        endpoints=[dict(r) for r in db.execute("SELECT e.id,e.name,e.runtime,e.endpoint_url,e.external_model_id,e.ownership,e.last_status,e.is_enabled,m.name AS linked_model_name FROM model_endpoints e LEFT JOIN model_registry m ON m.id=e.model_id WHERE e.is_enabled=1 ORDER BY e.name COLLATE NOCASE")]
        assigned={role:(r["model_id"] or r["endpoint_id"]) for role,capability in ROLE_CAPABILITIES.items() if (r:=db.execute("SELECT model_id,endpoint_id FROM model_capability_assignments WHERE capability=?",(capability,)).fetchone())}
        benchmarks=[dict(r) for r in db.execute("SELECT * FROM agent_benchmarks ORDER BY created_at DESC LIMIT 20")]
    output=[{**m,"metadata":json.loads(m.pop("metadata_json") or "{}")} for m in models]
    for endpoint in endpoints:
        endpoint_name=endpoint["name"]
        if endpoint["runtime"]=="llama.cpp" and "chat" in endpoint_name.casefold(): label="qwen2.5:3b · llama.cpp chat"
        elif endpoint["runtime"]=="llama.cpp" and "coding" in endpoint_name.casefold(): label="qwen2.3:coder7b · llama.cpp coding"
        else: label=f"{endpoint['linked_model_name'] or endpoint['external_model_id'] or endpoint_name} · {endpoint['runtime']}"
        output.append({"id":endpoint["id"],"name":label,"runtime":endpoint["runtime"],"status":endpoint["last_status"],"source_type":"endpoint","endpoint_url":endpoint["endpoint_url"],"external_model_id":endpoint["external_model_id"],"ownership":endpoint["ownership"]})
    return {"models":output,"roles":assigned,"benchmarks":[{**b,"results":json.loads(b["results_json"])} for b in benchmarks]}

@router.put("/models/roles")
def set_role(data: RoleAssignment):
    with connect() as db:
        if data.model_id:
            found=db.execute("SELECT id,status,name,metadata_json FROM model_registry WHERE id=?",(data.model_id,)).fetchone()
            endpoint=None
            if found and found["status"]=="installed":
                metadata=json.loads(found["metadata_json"] or "{}")
                if metadata.get("is_cloud") or found["name"].casefold().endswith(":cloud"): raise HTTPException(400,"Cloud models are not enabled for the local autonomous agent")
            else:
                endpoint=db.execute("SELECT id FROM model_endpoints WHERE id=? AND is_enabled=1",(data.model_id,)).fetchone()
                if not endpoint: raise HTTPException(400,"Choose an installed local model or an enabled local runtime endpoint")
            model_id=found["id"] if found and found["status"]=="installed" else None
            endpoint_id=data.model_id if endpoint else None
        else:
            model_id=None;endpoint_id=None
        db.execute("INSERT INTO model_capability_assignments(capability,model_id,endpoint_id,updated_at) VALUES(?,?,?,?) ON CONFLICT(capability) DO UPDATE SET model_id=excluded.model_id,endpoint_id=excluded.endpoint_id,updated_at=excluded.updated_at",(ROLE_CAPABILITIES[data.role],model_id,endpoint_id,now()))
    return {"role":data.role,"model_id":data.model_id}
@router.get("/models/recommendations")
def model_recommendations(role: Literal["planner","coding","lightweight_reasoning","embedding","project_summarization"]="planner"):
    with connect() as db:
        rows=db.execute("SELECT id,name,runtime,file_size_bytes,metadata_json FROM model_registry WHERE status='installed'").fetchall()
    models=[{"id":r["id"],"name":r["name"],"runtime":r["runtime"],"file_size_bytes":r["file_size_bytes"],"metadata":json.loads(r["metadata_json"] or "{}")} for r in rows]
    return {"role":role,"available_memory_bytes":available_memory_bytes(),"candidates":recommend_models(models,role),"note":"Estimates use registered model file size plus runtime overhead; they are guidance, not measured memory usage."}

@router.post("/models/benchmark")
async def benchmark(data: BenchmarkRequest):
    with connect() as db: model,endpoint=_selected_model(db,data.model_id)
    model_id=(model or endpoint)["id"]; model_name=(model or endpoint)["name"]; runtime=(model or endpoint)["runtime"]
    is_embedding="embed" in model_name.casefold()
    prompts={
      "code_generation":("Write a short Python function named clamp(value, low, high) that returns the value bounded by low and high. Code only.",False),
      "code_explanation":("In one or two sentences, explain what this Python expression does: [x*x for x in values if x > 0].",False),
      "requirement_analysis":("Return JSON with objective and requirements fields for this request: add a health endpoint that returns JSON status.",True),
      "planning":("Return JSON with a tasks array containing two ordered implementation tasks for adding the health endpoint.",True),
      "structured_output":("Return exactly JSON: {\"valid\": true, \"count\": 3}.",True),
      "tool_instruction":("Return JSON with action=read_file and path=README.md. Do not modify files or run commands.",True),
      "project_summarization":("Return JSON with a summary field based only on this evidence: Python API; app/main.py; tests/test_api.py; SQLite.",True),
      "long_context":("Read the following bounded context and return JSON {\"marker\":\"LOCAL-CONTEXT-731\"} only if you see the marker. Context: "+("The source file defines a parser, route handlers and tests. "*140)+"\nMarker: LOCAL-CONTEXT-731",True),
    }
    results={}
    if is_embedding:
        started=time.perf_counter()
        try:
            if runtime!="ollama": raise ValueError("Embedding benchmark is currently supported for the Ollama local adapter only")
            url=os.environ.get("OLLAMA_URL","http://127.0.0.1:11434").rstrip("/")
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0,connect=5.0)) as client:
                response=await client.post(url+"/api/embed",json={"model":model_name,"input":["Find the function that returns a bounded numeric value."],"keep_alive":"0"})
                response.raise_for_status(); payload=response.json()
            vectors=payload.get("embeddings") or []
            valid=bool(vectors and isinstance(vectors[0],list) and vectors[0] and all(isinstance(v,(float,int)) for v in vectors[0]))
            results["embedding"]={"status":"success" if valid else "invalid","response_ms":round((time.perf_counter()-started)*1000,1),"dimensions":len(vectors[0]) if valid else 0,"vector_valid":valid}
        except Exception as exc:
            results["embedding"]={"status":"failed","response_ms":round((time.perf_counter()-started)*1000,1),"vector_valid":False,"error":str(exc)[:200]}
        for task in prompts: results[task]={"status":"not_applicable","reason":"Embedding model; completion tasks were skipped."}
    else:
        results["embedding"]={"status":"not_applicable","reason":"Completion model; semantic embedding test is run for embedding-assigned models."}
        for task,(prompt,expects_json) in prompts.items():
            started=time.perf_counter()
            try:
                measured=await _complete_metrics(model,endpoint,prompt); output=measured["text"]; parsed=None; valid_json=None
                if expects_json:
                    try: parsed=_parse_json(output); valid_json=True
                    except (ValueError,json.JSONDecodeError): valid_json=False
                compliant=True
                if task=="structured_output": compliant=parsed=={"valid":True,"count":3}
                elif task=="tool_instruction": compliant=isinstance(parsed,dict) and parsed.get("action")=="read_file" and parsed.get("path")=="README.md"
                elif task=="long_context": compliant=isinstance(parsed,dict) and parsed.get("marker")=="LOCAL-CONTEXT-731"
                elif task=="code_generation": compliant=any(token in output for token in ("def ","return","lambda"))
                elif task=="code_explanation": compliant=len(output.strip())>=20
                elapsed=(time.perf_counter()-started)*1000; generated=measured.get("generated_tokens")
                if generated is None: generated=max(1,round(len(output)/4))
                duration_ns=measured.get("generation_duration_ns")
                tps=round(generated/(duration_ns/1e9),2) if duration_ns else round(generated/(elapsed/1000),2) if elapsed else 0
                results[task]={"status":"success" if compliant and valid_json is not False else "invalid","response_ms":round(elapsed,1),"json_valid":valid_json,"instruction_compliant":compliant,"generated_tokens":generated,"tokens_per_second":tps,"token_rate_measured":bool(duration_ns),"preview":output[:400]}
            except Exception as exc:
                results[task]={"status":"failed","response_ms":round((time.perf_counter()-started)*1000,1),"json_valid":False if expects_json else None,"instruction_compliant":False,"error":str(exc)[:200]}
    record_id=str(uuid.uuid4())
    with connect() as db: db.execute("INSERT INTO agent_benchmarks VALUES(?,?,?,?,?,?)",(record_id,model_id,model_name,runtime,json.dumps(results),now()))
    return {"id":record_id,"model_id":model_id,"model_name":model_name,"runtime":runtime,"results":results,"note":"Controlled local prompts were run sequentially with keep_alive=0. Token rate uses runtime counts when available and otherwise a response-length estimate. Memory consumption was not measured."}


@project_router.get("/status")
def project_status(request: Request):
    return intelligence.get_status(root_for(request))

@project_router.post("/scan")
async def project_scan(request: Request,data: ProjectScanRequest = Body(default=ProjectScanRequest())):
    root=root_for(request)
    try: exclusions=ProjectScanRequest.validate_paths(data.exclude_paths)
    except ValueError as exc: raise HTTPException(422,str(exc))
    with SCAN_LOCK:
        for job in SCAN_JOBS.values():
            if job["root"]==str(root) and job["status"] in {"indexing","cancelling"}:
                raise HTTPException(409,"A scan for this workspace is already running")
        job_id=str(uuid.uuid4()); SCAN_JOBS[job_id]={"root":str(root),"status":"indexing","completed":0,"total":None,"current_file":"","error":"","result":None,"cancel":threading.Event()}
    asyncio.create_task(_run_scan(job_id,root,exclusions))
    return {"job_id":job_id,"status":"indexing","completed":0,"total":None,"current_file":""}

async def _run_scan(job_id,root,exclusions):
    def report(done,total,path):
        with SCAN_LOCK:
            if job_id in SCAN_JOBS: SCAN_JOBS[job_id].update(completed=done,total=total,current_file=path)
    try:
        result=await run_in_threadpool(intelligence.scan_and_index,root,report,SCAN_JOBS[job_id]["cancel"],exclusions)
        with SCAN_LOCK: SCAN_JOBS[job_id].update(status="indexed",completed=result["files_indexed"],total=result["files_indexed"],current_file="",result=result)
    except intelligence.ScanCancelled as exc:
        with intelligence.connect() as db: db.execute("UPDATE project_indexes SET status='failed',error=?,updated_at=? WHERE root_path=?",(str(exc),now(),str(root)))
        with SCAN_LOCK: SCAN_JOBS[job_id].update(status="cancelled",error=str(exc),current_file="")
    except Exception as exc:
        with intelligence.connect() as db: db.execute("UPDATE project_indexes SET status='failed',error=?,updated_at=? WHERE root_path=?",(str(exc)[:500],now(),str(root)))
        with SCAN_LOCK: SCAN_JOBS[job_id].update(status="failed",error=str(exc)[:500],current_file="")

@project_router.get("/scan/{job_id}")
def project_scan_status(job_id: str,request: Request):
    root=str(root_for(request))
    with SCAN_LOCK: job=SCAN_JOBS.get(job_id)
    if not job or job["root"]!=root: raise HTTPException(404,"Project scan job not found")
    return {key:value for key,value in job.items() if key not in {"root","cancel"}}

@project_router.delete("/scan/{job_id}")
def project_scan_cancel(job_id: str,request: Request):
    root=str(root_for(request))
    with SCAN_LOCK:
        job=SCAN_JOBS.get(job_id)
        if not job or job["root"]!=root: raise HTTPException(404,"Project scan job not found")
        if job["status"]!="indexing": raise HTTPException(409,"Project scan is not running")
        job["cancel"].set(); job["status"]="cancelling"
    return {"job_id":job_id,"status":"cancelling"}

@project_router.get("/summary")
def project_summary(request: Request):
    root=root_for(request); status=intelligence.get_status(root)
    if status.get("status")!="indexed": raise HTTPException(409,"Scan and index the selected project first")
    return status["summary"]

@project_router.get("/search")
def project_search(request: Request, q: str = Query(min_length=1,max_length=500), limit: int = Query(default=20,ge=1,le=100), mode: Literal["keyword","semantic","hybrid"]="keyword"):
    root=root_for(request); status=intelligence.get_status(root)
    if status.get("status")!="indexed": raise HTTPException(409,"Scan and index the selected project first")
    results=intelligence.search(root,q,kind=mode,limit=limit)
    actual=results[0].get("rank_mode",mode) if results else ("keyword_fallback" if mode!="keyword" else "keyword")
    return {"query":q,"results":results,"mode":actual}

@project_router.get("/context")
def project_context(request: Request, q: str = Query(min_length=1,max_length=500), max_chars: int = Query(default=12000,ge=256,le=40000)):
    root=root_for(request)
    if max_chars<256 or max_chars>40000: raise HTTPException(422,"max_chars must be between 256 and 40000")
    status=intelligence.get_status(root)
    if status.get("status")!="indexed": raise HTTPException(409,"Scan and index the selected project first")
    return intelligence.context(root,q,max_chars)

@project_router.get("/files")
def project_files(request: Request, q: str = "", limit: int = 500):
    root=root_for(request)
    config=intelligence.get_status(root); exclusions=json.loads(config.get("exclusions_json") or "[]")
    try: paths=intelligence.enumerate_files(root,max_files=max(1,min(limit,2000)),exclusions=exclusions)
    except (OSError,ValueError) as exc: raise HTTPException(400,str(exc))
    if q: paths=[p for p in paths if q.casefold() in p.relative_to(root).as_posix().casefold()]
    return [{"path":p.relative_to(root).as_posix(),"size":p.stat().st_size,"language":intelligence._language(p)} for p in paths[:max(1,min(limit,2000))]]

@project_router.get("/read")
def project_read(request: Request, path: str = Query(min_length=1,max_length=4096)):
    root=root_for(request)
    try:
        target=intelligence.safe_path(root,path)
        if intelligence._secret_path(target) or target.suffix.lower() in intelligence.EXCLUDED_SUFFIXES: raise ValueError("This file is excluded from project context")
        text=intelligence._read(target)
        if text is None: raise ValueError("File is binary, unreadable, or contains secret material")
        return {"path":target.relative_to(root).as_posix(),"content":text[:200000],"truncated":len(text)>200000}
    except (OSError,ValueError) as exc: raise HTTPException(403,str(exc))

@project_router.get("/symbols")
def project_symbols(request: Request, q: str = "", limit: int = 100):
    root=root_for(request); status=intelligence.get_status(root)
    if status.get("status")!="indexed": raise HTTPException(409,"Scan and index the selected project first")
    terms=[x.casefold() for x in q.split()]
    key=__import__("hashlib").sha256(str(root).casefold().encode()).hexdigest()
    with intelligence.connect() as db:
        rows=db.execute("SELECT relative_path,name,kind,line_start,line_end,signature FROM project_symbols WHERE project_key=? ORDER BY relative_path,line_start LIMIT ?",(key,max(1,min(limit,500)))).fetchall()
    result=[dict(r) for r in rows if not terms or all(t in (r["name"]+" "+r["relative_path"]+" "+r["signature"]).casefold() for t in terms)]
    return result[:max(1,min(limit,500))]

@project_router.get("/structure")
def project_structure(request: Request):
    root=root_for(request)
    config=intelligence.get_status(root); exclusions=json.loads(config.get("exclusions_json") or "[]")
    try: paths=intelligence.enumerate_files(root,max_files=20000,exclusions=exclusions)
    except (OSError,ValueError) as exc: raise HTTPException(400,str(exc))
    tree={}
    for path in paths:
        rel=path.relative_to(root).parts; branch=tree
        for part in rel[:-1]: branch=branch.setdefault(part,{})
        branch[rel[-1]]=None
    return tree

@project_router.get("/dependencies")
def project_dependencies(request: Request, resolution: Literal["all","internal","external","unresolved"]="all"):
    root=root_for(request); status=intelligence.get_status(root)
    if status.get("status")!="indexed": raise HTTPException(409,"Scan and index the selected project first")
    key=__import__("hashlib").sha256(str(root).casefold().encode()).hexdigest()
    with intelligence.connect() as db:
        if resolution=="all": rows=db.execute("SELECT source_path,target,kind,resolution FROM project_dependencies WHERE project_key=? ORDER BY source_path,target",(key,)).fetchall()
        else: rows=db.execute("SELECT source_path,target,kind,resolution FROM project_dependencies WHERE project_key=? AND resolution=? ORDER BY source_path,target",(key,resolution)).fetchall()
    return {"dependencies":[dict(row) for row in rows],"counts":{kind:sum(1 for row in rows if row["resolution"]==kind) for kind in ("internal","external","unresolved")}}

@project_router.get("/tools")
def tool_registry():
    return [{"name":name,"description":description,"input_schema":schema,"output_schema":{"type":"object"},"permission":"read_only","enabled":True} for name,description,schema in [
        ("project.list_files","List safe source files in the selected workspace",{"type":"object","properties":{"q":{"type":"string"}}}),
        ("project.read_file","Read a non-secret text file under the workspace root",{"type":"object","required":["path"],"properties":{"path":{"type":"string"}}}),
        ("project.search","Keyword search indexed symbols and source chunks",{"type":"object","required":["q"],"properties":{"q":{"type":"string"}}}),
        ("project.structure","Retrieve project directory structure",{"type":"object"}),
        ("project.summary","Retrieve evidence-based project summary",{"type":"object"}),
        ("project.symbols","Retrieve indexed code symbols",{"type":"object","properties":{"q":{"type":"string"}}}),
        ("project.context","Retrieve bounded relevant source context",{"type":"object","required":["q"],"properties":{"q":{"type":"string"},"max_chars":{"type":"integer"}}}),
    ]]
