"""Safe, bounded project scanning, source analysis, indexing and retrieval."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import hashlib
import heapq
import json
import os
import re
import sqlite3
import sys
import time
import math
import httpx
from threading import Event
from collections import Counter
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("LOCAL_AI_HOME", r"D:\LocalAIStudio\data")) / "database" / "local_ai.db"
EXCLUDED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist", "target", ".next", ".cache", "coverage", "vendor"}
EXCLUDED_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519", "credentials", "secrets.json", "secrets.yaml", "secrets.yml"}
EXCLUDED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".gguf", ".safetensors", ".bin", ".exe", ".dll", ".so", ".pyc", ".pyd", ".zip", ".7z", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".mp4", ".mp3"}
SECRET_PATTERNS = [re.compile(r"(?i)(api[_-]?key|access[_-]?key|secret|password|token|private[_-]?key|client[_-]?secret)\s*[:=]\s*['\"]?[^\s'\"]{6,}"), re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{12,}"), re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), re.compile(r"\bAKIA[0-9A-Z]{16}\b"), re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")]
MAX_FILE = 1_000_000
MAX_FILES = 20_000
CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".c", ".h", ".cc", ".cpp", ".hpp", ".html", ".css", ".scss", ".sql", ".md", ".json", ".toml", ".yaml", ".yml", ".sh", ".bat", ".ps1"}


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
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
    if db_path:
        DB_PATH = Path(db_path)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS project_indexes(project_key TEXT PRIMARY KEY, root_path TEXT NOT NULL, status TEXT NOT NULL, summary_json TEXT, scan_json TEXT, indexed_at TEXT, updated_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', exclusions_json TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS project_index_files(project_key TEXT NOT NULL, relative_path TEXT NOT NULL, content_hash TEXT NOT NULL, mtime_ns INTEGER NOT NULL, size_bytes INTEGER NOT NULL, language TEXT, indexed_at TEXT NOT NULL, PRIMARY KEY(project_key,relative_path), FOREIGN KEY(project_key) REFERENCES project_indexes(project_key) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS project_symbols(id INTEGER PRIMARY KEY, project_key TEXT NOT NULL, relative_path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, line_start INTEGER, line_end INTEGER, signature TEXT NOT NULL DEFAULT '', details_json TEXT NOT NULL DEFAULT '{}');
        CREATE INDEX IF NOT EXISTS idx_project_symbols_name ON project_symbols(project_key,name);
        CREATE TABLE IF NOT EXISTS project_chunks(id INTEGER PRIMARY KEY, project_key TEXT NOT NULL, relative_path TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, content_hash TEXT NOT NULL, content TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_project_chunks_path ON project_chunks(project_key,relative_path);
        CREATE INDEX IF NOT EXISTS idx_project_chunks_line ON project_chunks(project_key,relative_path,line_start);
        CREATE TABLE IF NOT EXISTS project_chunk_embeddings(project_key TEXT NOT NULL, relative_path TEXT NOT NULL, line_start INTEGER NOT NULL, content_hash TEXT NOT NULL, model_id TEXT NOT NULL, vector_json TEXT NOT NULL, PRIMARY KEY(project_key,relative_path,line_start,content_hash));
        CREATE TABLE IF NOT EXISTS project_dependencies(project_key TEXT NOT NULL,source_path TEXT NOT NULL,target TEXT NOT NULL,kind TEXT NOT NULL,resolution TEXT NOT NULL,PRIMARY KEY(project_key,source_path,target,kind));
        """)
        columns={row["name"] for row in db.execute("PRAGMA table_info(project_indexes)").fetchall()}
        if "exclusions_json" not in columns: db.execute("ALTER TABLE project_indexes ADD COLUMN exclusions_json TEXT NOT NULL DEFAULT '[]'")
        db.execute("UPDATE project_indexes SET status=CASE WHEN EXISTS(SELECT 1 FROM project_index_files f WHERE f.project_key=project_indexes.project_key) THEN 'outdated' ELSE 'failed' END,error='Scan was interrupted by application shutdown',updated_at=? WHERE status='indexing'",(time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),))


def safe_path(root: Path, rel: str = "") -> Path:
    root = root.resolve(strict=True)
    candidate = (root / rel).resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise ValueError("Path escapes the selected project root")
    cursor = root
    for part in Path(rel).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Parent-directory traversal is not allowed")
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Symbolic links are excluded from project scanning")
    return candidate


def _secret_path(path: Path) -> bool:
    name = path.name.lower()
    if name in EXCLUDED_NAMES or "secret" in name or "credential" in name or name.startswith(".env") or name.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    return False


def _language(path: Path) -> str | None:
    return {".py":"Python", ".js":"JavaScript", ".jsx":"JavaScript", ".mjs":"JavaScript", ".cjs":"JavaScript", ".ts":"TypeScript", ".tsx":"TypeScript", ".java":"Java", ".c":"C", ".h":"C", ".cc":"C++", ".cpp":"C++", ".hpp":"C++", ".html":"HTML", ".css":"CSS", ".scss":"SCSS", ".sql":"SQL", ".md":"Markdown", ".json":"JSON", ".toml":"TOML", ".yaml":"YAML", ".yml":"YAML", ".sh":"Shell", ".ps1":"PowerShell"}.get(path.suffix.lower())


def enumerate_files(root: Path, max_files: int = MAX_FILES, exclusions: list[str] | None = None):
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Selected project root is not a directory")
    found = []
    extra={str(item).replace("\\","/").strip("/") for item in (exclusions or []) if str(item).strip()}
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and d not in extra and str((Path(base)/d).relative_to(root)).replace("\\","/") not in extra and not d.startswith(".") and not (Path(base) / d).is_symlink())
        for name in sorted(names):
            path = Path(base) / name
            rel=path.relative_to(root).as_posix()
            if name in extra or rel in extra or path.is_symlink() or _secret_path(path) or path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            try:
                stat = path.stat()
                if stat.st_size > MAX_FILE:
                    continue
            except OSError:
                continue
            found.append(path)
            if len(found) >= max_files:
                return found
    return found


def _read(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
        if b"\x00" in raw:
            return None
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeError):
        return None
    # Exclude whole files containing credentials; never pass partial secrets onward.
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        return None
    return text


def _detect(files: list[Path], root: Path):
    names = {p.name.lower() for p in files}
    languages = Counter(_language(p) for p in files if _language(p))
    frameworks = set(); dependencies=set(); scripts=[]; database_signals=set(); config_files=[]
    manifests = []
    if "package.json" in names:
        manifests.append("package.json")
        for p in files:
            if p.name.lower() == "package.json":
                try:
                    package = json.loads(_read(p) or "{}")
                    deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
                    dependencies.update(str(k) for k in deps)
                    package_scripts=package.get("scripts",{})
                    scripts.extend(f"npm run {key}" for key in package_scripts if key in {"dev","start","build","test","lint"})
                    for key, label in {"react":"React", "next":"Next.js", "vite":"Vite", "vue":"Vue", "@angular/core":"Angular", "express":"Express", "fastify":"Fastify"}.items():
                        if key in deps: frameworks.add(label)
                except (json.JSONDecodeError, TypeError): pass
    for name, label in [("pyproject.toml", "pyproject.toml"), ("requirements.txt", "requirements.txt"), ("pipfile", "Pipfile"), ("pom.xml", "Maven"), ("cargo.toml", "Cargo"), ("makefile", "Makefile")]:
        if name in names: manifests.append(label)
    for p in files:
        text = (_read(p) or "").lower()
        if p.name.lower() in {"requirements.txt", "pyproject.toml"} or p.suffix.lower()==".py":
            if "fastapi" in text: frameworks.add("FastAPI")
            if "flask" in text: frameworks.add("Flask")
        if p.name.lower() in {"requirements.txt", "pyproject.toml"}:
            if p.name.lower()=="requirements.txt":
                dependencies.update(re.split(r"[<>=!~;\s]",line.split(";")[0].split("[")[0],maxsplit=1)[0].strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#","-")))
        if "config" in p.name.lower() or p.name.lower() in {"vite.config.ts","vite.config.js","webpack.config.js","tsconfig.json","dockerfile","makefile"}:
            config_files.append(p.relative_to(root).as_posix())
        if p.suffix.lower() in {".py",".ts",".tsx",".js",".jsx",".json",".toml"}:
            text=(_read(p) or "").casefold()
            for token,label in (("sqlite","SQLite"),("sqlalchemy","SQLAlchemy"),("postgresql","PostgreSQL"),("mysql","MySQL"),("mongodb","MongoDB")):
                if token in text: database_signals.add(label)
    tests = [p.relative_to(root).as_posix() for p in files if "test" in p.parts or p.name.lower().startswith(("test_", "tests.")) or ".test." in p.name.lower() or ".spec." in p.name.lower()]
    docs = [p.relative_to(root).as_posix() for p in files if p.suffix.lower() == ".md" and (p.name.lower().startswith(("readme", "contributing", "docs")) or "docs" in p.parts)][:100]
    entries = [p.relative_to(root).as_posix() for p in files if p.name.lower() in {"main.py", "app.py", "server.py", "index.ts", "index.tsx", "main.ts", "main.tsx", "index.js", "server.js", "manage.py", "program.cs"}]
    types = set()
    if "package.json" in names: types.add("Node.js")
    if "requirements.txt" in names or "pyproject.toml" in names: types.add("Python")
    if frameworks: types.update(frameworks)
    if any(p.suffix.lower() == ".java" for p in files): types.add("Java")
    if any(p.suffix.lower() in {".c", ".cpp", ".cc"} for p in files): types.add("C/C++")
    if not types and any(p.suffix.lower() in {".html", ".css"} for p in files): types.add("HTML/CSS")
    if "pytest" in names or any(p.name.startswith("test_") for p in files): scripts.append("pytest")
    if "pom.xml" in names: scripts.extend(["mvn test","mvn package"])
    if "makefile" in names: scripts.append("make")
    if "gradlew" in names: scripts.append("./gradlew test")
    if "cargo.toml" in names: scripts.extend(["cargo test","cargo build"])
    return {"project_name": root.name, "project_type": sorted(types), "languages": dict(languages), "frameworks": sorted(frameworks), "dependency_manifests": sorted(set(manifests)), "dependencies": sorted(x for x in dependencies if x)[:500], "database_signals": sorted(database_signals), "configuration_files": config_files[:150], "run_instructions": list(dict.fromkeys(scripts)), "entry_points": entries[:100], "tests": tests[:500], "documentation": docs, "file_count": len(files)}


def _embedding_model():
    try:
        with connect() as db:
            row=db.execute("SELECT m.id,m.name,m.runtime,m.status,m.metadata_json FROM model_capability_assignments a JOIN model_registry m ON m.id=a.model_id WHERE a.capability='embedding' LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or row["status"]!="installed" or row["runtime"]!="ollama" or "embed" not in row["name"].casefold() or row["name"].casefold().endswith(":cloud") or json.loads(row["metadata_json"] or "{}").get("is_cloud"):
        return None
    return dict(row)


def _embed(model_name: str, texts: list[str], keep_alive="0"):
    url=os.environ.get("OLLAMA_URL","http://127.0.0.1:11434").rstrip("/")
    response=httpx.post(url+"/api/embed",json={"model":model_name,"input":texts,"keep_alive":keep_alive},timeout=httpx.Timeout(120.0,connect=5.0))
    if response.is_error: raise ValueError(f"Ollama embedding request failed ({response.status_code}): {response.text[:240]}")
    payload=response.json(); vectors=payload.get("embeddings") or []
    if len(vectors)!=len(texts) or any(not isinstance(v,list) or not v for v in vectors): raise ValueError("Embedding runtime returned an invalid vector batch")
    return vectors


def _index_embeddings(project_key: str):
    model=_embedding_model()
    if not model: return {"status":"unavailable","model_id":None,"embedded_chunks":0,"reason":"Assign an installed local Ollama embedding model to the Embedding role to enable semantic retrieval."}
    with connect() as db:
        current=db.execute("SELECT DISTINCT model_id FROM project_chunk_embeddings WHERE project_key=?",(project_key,)).fetchall()
        if any(row[0]!=model["id"] for row in current): db.execute("DELETE FROM project_chunk_embeddings WHERE project_key=?",(project_key,))
        rows=db.execute("SELECT c.relative_path,c.line_start,c.content_hash,c.content FROM project_chunks c LEFT JOIN project_chunk_embeddings e ON e.project_key=c.project_key AND e.relative_path=c.relative_path AND e.line_start=c.line_start AND e.content_hash=c.content_hash AND e.model_id=? WHERE c.project_key=? AND e.model_id IS NULL ORDER BY c.relative_path,c.line_start",(model["id"],project_key)).fetchall()
    embedded=0; attempted=bool(rows)
    try:
        for offset in range(0,len(rows),16):
            batch=rows[offset:offset+16]; vectors=_embed(model["name"],[r["content"] for r in batch],keep_alive="10m")
            with connect() as db:
                db.executemany("INSERT OR REPLACE INTO project_chunk_embeddings VALUES(?,?,?,?,?,?)",[(project_key,r["relative_path"],r["line_start"],r["content_hash"],model["id"],json.dumps(v)) for r,v in zip(batch,vectors)])
            embedded+=len(batch)
        with connect() as db: total=db.execute("SELECT COUNT(*) FROM project_chunk_embeddings WHERE project_key=? AND model_id=?",(project_key,model["id"])).fetchone()[0]
        return {"status":"indexed","model_id":model["id"],"embedded_chunks":embedded,"total_embeddings":total}
    except (httpx.HTTPError,ValueError,KeyError) as exc:
        return {"status":"failed","model_id":model["id"],"embedded_chunks":embedded,"reason":str(exc)[:300]}
    finally:
        if attempted:
            try: httpx.post(os.environ.get("OLLAMA_URL","http://127.0.0.1:11434").rstrip("/")+"/api/generate",json={"model":model["name"],"prompt":"","keep_alive":0,"stream":False},timeout=2)
            except httpx.HTTPError: pass


def _parse_symbols(rel: str, text: str):
    symbols = []
    if rel.lower().endswith(".py"):
        try:
            tree = ast.parse(text)
            parents={child:node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    is_method=isinstance(parents.get(node),(ast.ClassDef,))
                    kind = "class" if isinstance(node, ast.ClassDef) else ("async_method" if is_method and isinstance(node,ast.AsyncFunctionDef) else "method" if is_method else "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
                    symbols.append({"name": node.name, "kind": kind, "line_start": node.lineno, "line_end": getattr(node, "end_lineno", node.lineno), "signature": node.name})
                    if isinstance(node,ast.ClassDef) and any(any(marker in ast.dump(base) for marker in ("Base", "Model", "SQLModel", "DeclarativeBase")) for base in node.bases):
                        symbols.append({"name":node.name,"kind":"db_model","line_start":node.lineno,"line_end":getattr(node,"end_lineno",node.lineno),"signature":node.name})
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    for decorator in node.decorator_list:
                        if isinstance(decorator,ast.Call) and isinstance(decorator.func,ast.Attribute) and decorator.func.attr.lower() in {"get","post","put","patch","delete","options","head"}:
                            path=decorator.args[0].value if decorator.args and isinstance(decorator.args[0],ast.Constant) and isinstance(decorator.args[0].value,str) else None
                            if path: symbols.append({"name":f"{decorator.func.attr.upper()} {path}","kind":"route","line_start":decorator.lineno,"line_end":decorator.lineno,"signature":f"{decorator.func.attr.upper()} {path}"})
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    symbols.extend({"name": n, "kind":"import", "line_start":node.lineno, "line_end":node.lineno, "signature":n} for n in names if n)
            return symbols, None
        except SyntaxError as exc:
            return symbols, f"SyntaxError at line {exc.lineno}: {exc.msg}"
    # Lightweight structural extraction without adding parser dependencies.
    patterns = [(r"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([\w$]+)", "function"), (r"\bclass\s+([\w$]+)", "class"), (r"\b(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>", "function"), (r"\bimport\s+(?:[^;]*?\s+from\s+)?['\"]([^'\"]+)['\"]", "import"), (r"\bexport\s+(?:const|let|var|function|class)\s+([\w$]+)", "export"), (r"@(?:app|router)\.(?:get|post|put|patch|delete)\(['\"]([^'\"]+)", "route")]
    for pattern, kind in patterns:
        for match in re.finditer(pattern, text):
            line = text.count("\n", 0, match.start()) + 1
            symbols.append({"name":match.group(1), "kind":kind, "line_start":line, "line_end":line, "signature":match.group(0)[:240]})
    return symbols, None


def _dependency_references(path: Path, text: str):
    refs=[]
    if path.suffix.lower()==".py":
        try:
            tree=ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node,ast.Import): refs.extend((alias.name,"python_import") for alias in node.names)
                elif isinstance(node,ast.ImportFrom) and node.module: refs.append(("."*node.level+node.module,"python_import"))
        except SyntaxError: pass
    else:
        for match in re.finditer(r"\b(?:from|import)\s*['\"]([^'\"]+)['\"]|\bfrom\s+['\"]([^'\"]+)['\"]|\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)",text):
            value=next((group for group in match.groups() if group),None)
            if value: refs.append((value,"javascript_import"))
    return list(dict.fromkeys(refs))


class ScanCancelled(Exception): pass

def scan_and_index(root: Path, progress=None, cancel_event: Event | None = None, exclusions: list[str] | None = None):
    root = root.resolve(strict=True)
    project_key = hashlib.sha256(str(root).casefold().encode()).hexdigest()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with connect() as db:
        db.execute("INSERT INTO project_indexes(project_key,root_path,status,updated_at,error,exclusions_json) VALUES(?,?,'indexing',?,'',?) ON CONFLICT(project_key) DO UPDATE SET status='indexing',updated_at=excluded.updated_at,error='',exclusions_json=excluded.exclusions_json", (project_key,str(root),now,json.dumps(exclusions or [])))
        previous = {r["relative_path"]: r["content_hash"] for r in db.execute("SELECT relative_path,content_hash FROM project_index_files WHERE project_key=?",(project_key,))}
    files = enumerate_files(root,exclusions=exclusions)
    file_entries, symbols_all, chunks_all, dependencies_all, errors = [], [], [], [], []
    module_paths={}
    for p in files:
        rel=p.relative_to(root).as_posix()
        stem=Path(rel).with_suffix("").as_posix()
        module_paths[stem.casefold()]=rel
        if Path(rel).stem.casefold()=="index": module_paths[Path(rel).parent.as_posix().casefold()]=rel
        if Path(rel).stem.casefold()=="__init__": module_paths[Path(rel).parent.as_posix().casefold()]=rel
    for index, path in enumerate(files):
        if cancel_event and cancel_event.is_set(): raise ScanCancelled("Project scan was cancelled")
        rel = path.relative_to(root).as_posix()
        if progress: progress(index, len(files), rel)
        text = _read(path)
        if text is None:
            try:
                raw=path.read_bytes(); stat=path.stat()
                # Track only metadata and a hash so status checks remain stable; never index secret-bearing text.
                file_entries.append((rel,hashlib.sha256(raw).hexdigest(),stat.st_mtime_ns,stat.st_size,_language(path),now))
            except OSError: pass
            continue
        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        stat = path.stat()
        file_entries.append((rel,digest,stat.st_mtime_ns,stat.st_size,_language(path),now))
        if previous.get(rel) == digest:
            continue
        for reference,kind in _dependency_references(path,text):
            if kind=="javascript_import" and reference.startswith("."):
                normalized=(Path(rel).parent/reference).as_posix().replace("\\","/")
            elif reference.startswith("."):
                base=Path(rel).parent; leading=len(reference)-len(reference.lstrip(".")); module=reference.lstrip(".")
                for _ in range(max(0,leading-1)): base=base.parent
                normalized=(base/module.replace(".","/")).as_posix() if module else base.as_posix()
            else: normalized=reference.replace(".","/") if kind=="python_import" else reference
            candidates=[normalized,normalized+".py",normalized+".js",normalized+".jsx",normalized+".ts",normalized+".tsx",normalized+"/index.js",normalized+"/index.ts"]
            target=next((module_paths[c.casefold()] for c in candidates if c.casefold() in module_paths),None)
            if target: resolution="internal"; target_value=target
            else:
                resolution="unresolved"; target_value=reference
            dependencies_all.append((rel,target_value,kind,resolution))
        syms, err = _parse_symbols(rel,text)
        if err: errors.append({"path":rel,"error":err})
        symbols_all.extend((rel,s["name"],s["kind"],s["line_start"],s["line_end"],s["signature"],json.dumps({})) for s in syms)
        lines = text.splitlines()
        start = 0
        while start < len(lines):
            end = min(start + 100, len(lines))
            chunk = "\n".join(lines[start:end])
            chunks_all.append((rel,start+1,max(start+1,end),hashlib.sha256(chunk.encode()).hexdigest(),chunk))
            start = end
    summary = _detect(files,root)
    summary_deps={x.casefold().replace("-","_") for x in summary.get("dependencies",[])}
    dependencies_all=[(source,target,kind,"external" if resolution=="unresolved" and (target.split(".")[0].split("/")[0].casefold().replace("-","_") in summary_deps or (kind=="python_import" and target.split(".")[0].split("/")[0] in getattr(sys,"stdlib_module_names",set())) or (kind=="javascript_import" and target.startswith("node:"))) else resolution) for source,target,kind,resolution in dependencies_all]
    dirs = sorted({p.relative_to(root).parts[0] for p in files if len(p.relative_to(root).parts)>1})[:250]
    summary["directory_structure"] = dirs
    summary["potential_issues"] = [f"{len(errors)} source file(s) had parse errors; those files remain searchable." ] if errors else []
    with connect() as db:
        present={f[0] for f in file_entries}
        removed=set(previous)-present
        changed={f[0] for f in file_entries if previous.get(f[0])!=f[1]}
        for rel in removed|changed:
            db.execute("DELETE FROM project_symbols WHERE project_key=? AND relative_path=?",(project_key,rel))
            db.execute("DELETE FROM project_chunks WHERE project_key=? AND relative_path=?",(project_key,rel))
            db.execute("DELETE FROM project_index_files WHERE project_key=? AND relative_path=?",(project_key,rel))
            db.execute("DELETE FROM project_chunk_embeddings WHERE project_key=? AND relative_path=?",(project_key,rel))
            db.execute("DELETE FROM project_dependencies WHERE project_key=? AND source_path=?",(project_key,rel))
        db.executemany("INSERT INTO project_index_files(project_key,relative_path,content_hash,mtime_ns,size_bytes,language,indexed_at) VALUES(?,?,?,?,?,?,?)", [(project_key,*f) for f in file_entries if f[0] in changed])
        db.executemany("INSERT INTO project_symbols(project_key,relative_path,name,kind,line_start,line_end,signature,details_json) VALUES(?,?,?,?,?,?,?,?)", [(project_key,*s) for s in symbols_all])
        db.executemany("INSERT INTO project_chunks(project_key,relative_path,line_start,line_end,content_hash,content) VALUES(?,?,?,?,?,?)", [(project_key,*c) for c in chunks_all])
        db.executemany("INSERT OR REPLACE INTO project_dependencies VALUES(?,?,?,?,?)",[(project_key,*edge) for edge in dependencies_all])
    embedding_status=_index_embeddings(project_key)
    with connect() as db:
        symbols_count=db.execute("SELECT COUNT(*) FROM project_symbols WHERE project_key=?",(project_key,)).fetchone()[0]
        chunks_count=db.execute("SELECT COUNT(*) FROM project_chunks WHERE project_key=?",(project_key,)).fetchone()[0]
        dependencies_count=db.execute("SELECT COUNT(*) FROM project_dependencies WHERE project_key=?",(project_key,)).fetchone()[0]
        summary["important_modules"]=[r["relative_path"] for r in db.execute("SELECT relative_path,COUNT(*) symbol_count FROM project_symbols WHERE project_key=? AND kind IN ('class','function','async_function','route','db_model') GROUP BY relative_path ORDER BY symbol_count DESC,relative_path LIMIT 20",(project_key,))]
        summary["dependency_relationships"]={"internal":db.execute("SELECT COUNT(*) FROM project_dependencies WHERE project_key=? AND resolution='internal'",(project_key,)).fetchone()[0],"external":db.execute("SELECT COUNT(*) FROM project_dependencies WHERE project_key=? AND resolution='external'",(project_key,)).fetchone()[0],"unresolved":db.execute("SELECT COUNT(*) FROM project_dependencies WHERE project_key=? AND resolution='unresolved'",(project_key,)).fetchone()[0]}
        db.execute("UPDATE project_indexes SET status='indexed',summary_json=?,scan_json=?,indexed_at=?,updated_at=?,error='' WHERE project_key=?", (json.dumps(summary),json.dumps({"parse_errors":errors,"files_indexed":len(file_entries),"embedding":embedding_status}),now,now,project_key))
    return {"project_key":project_key,"status":"indexed","files_indexed":len(file_entries),"symbols_indexed":symbols_count,"chunks_indexed":chunks_count,"dependencies_indexed":dependencies_count,"embedding":embedding_status,"parse_errors":errors,"summary":summary,"indexed_at":now}


def get_status(root: Path):
    key = hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()
    with connect() as db:
        row = db.execute("SELECT * FROM project_indexes WHERE project_key=?",(key,)).fetchone()
        count = db.execute("SELECT COUNT(*) FROM project_index_files WHERE project_key=?",(key,)).fetchone()[0]
        indexed_files={r["relative_path"]:(r["mtime_ns"],r["size_bytes"]) for r in db.execute("SELECT relative_path,mtime_ns,size_bytes FROM project_index_files WHERE project_key=?",(key,))}
    if not row: return {"status":"not_indexed","files_indexed":0,"indexed_at":None,"error":""}
    result = dict(row); result["summary"] = json.loads(result.pop("summary_json") or "null"); result["scan"] = json.loads(result.pop("scan_json") or "{}"); result["files_indexed"] = count
    if result["status"]=="indexed":
        try:
            current={p.relative_to(root).as_posix():(p.stat().st_mtime_ns,p.stat().st_size) for p in enumerate_files(root,exclusions=json.loads(result.get("exclusions_json") or "[]"))}
            if current!=indexed_files:
                result["status"]="outdated"
                with connect() as db: db.execute("UPDATE project_indexes SET status='outdated',updated_at=? WHERE project_key=?",(time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),key))
        except (OSError,ValueError): result["status"]="outdated"
    return result


def search(root: Path, query: str, kind: str = "keyword", limit: int = 20):
    key = hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()
    terms = [t.casefold() for t in re.findall(r"[\w$.-]+", query) if len(t)>1][:12]
    limit=max(1,min(limit,100)); results=[]
    if kind in {"semantic","hybrid"}:
        model=_embedding_model()
        if model:
            try:
                vector=_embed(model["name"],[query])[0]
                query_norm=math.sqrt(sum(float(x)*float(x) for x in vector)) or 1.0
                top=[]
                with connect() as db:
                    for item in db.execute("SELECT relative_path,line_start,content_hash,vector_json FROM project_chunk_embeddings WHERE project_key=? AND model_id=?",(key,model["id"])):
                        candidate=json.loads(item["vector_json"]); norm=math.sqrt(sum(float(x)*float(x) for x in candidate)) or 1.0
                        score=sum(float(a)*float(b) for a,b in zip(vector,candidate))/(query_norm*norm)
                        entry=(score,item["relative_path"],item["line_start"],item["content_hash"])
                        if len(top)<limit: heapq.heappush(top,entry)
                        elif entry>top[0]: heapq.heapreplace(top,entry)
                    semantic=[]
                    keyword_keys=set()
                    for score,path,line,digest in sorted(top,reverse=True):
                        chunk=db.execute("SELECT line_end,content FROM project_chunks WHERE project_key=? AND relative_path=? AND line_start=? AND content_hash=? LIMIT 1",(key,path,line,digest)).fetchone()
                        if chunk: semantic.append({"type":"chunk","path":path,"line_start":line,"line_end":chunk["line_end"],"excerpt":chunk["content"][:1200],"score":round(score,5),"rank_mode":"semantic"})
                if semantic:
                    if kind=="hybrid":
                        keyword={ (r["path"],r.get("line_start")):r for r in _keyword_search(key,terms,limit) }
                        for row in semantic:
                            row["score"]+=0.2 if (row["path"],row["line_start"]) in keyword else 0
                    return sorted(semantic,key=lambda x:(-x["score"],x["path"],x["line_start"]))[:limit]
            except (httpx.HTTPError,ValueError,KeyError,TypeError):
                pass
    return _keyword_search(key,terms,limit)

def _keyword_search(key,terms,limit):
    if not terms: return []
    like_terms=[f"%{term}%" for term in terms]
    symbol_clause=" OR ".join("name LIKE ? OR signature LIKE ?" for _ in terms)
    chunk_clause=" OR ".join("content LIKE ?" for _ in terms)
    results=[]
    with connect() as db:
        symbols=db.execute(f"SELECT relative_path,name,kind,line_start,line_end,signature FROM project_symbols WHERE project_key=? AND ({symbol_clause}) LIMIT 1000",(key,*[x for term in like_terms for x in (term,term)])).fetchall()
        chunks=db.execute(f"SELECT relative_path,line_start,line_end,content FROM project_chunks WHERE project_key=? AND ({chunk_clause}) ORDER BY relative_path,line_start LIMIT 500",(key,*like_terms)).fetchall()
    for s in symbols:
        score=sum((3 if t == s["name"].casefold() else 1) for t in terms if t in s["name"].casefold() or t in s["signature"].casefold())
        if score: results.append({"type":"symbol","path":s["relative_path"],"name":s["name"],"kind":s["kind"],"line_start":s["line_start"],"line_end":s["line_end"],"excerpt":s["signature"],"score":score,"rank_mode":"keyword"})
    for c in chunks:
        lowered=c["content"].casefold(); score=sum(lowered.count(t) for t in terms)
        if score: results.append({"type":"chunk","path":c["relative_path"],"line_start":c["line_start"],"line_end":c["line_end"],"excerpt":c["content"][:1200],"score":score,"rank_mode":"keyword"})
    return sorted(results,key=lambda x:(-x["score"],x["path"],x.get("line_start",0)))[:limit]


def context(root: Path, query: str, max_chars: int = 12000):
    selected = search(root,query,kind="hybrid",limit=50)
    output=[]; used=0; evidence_items=[]
    for item in selected:
        # Re-read the indexed range from disk so a shortened excerpt never carries
        # a misleading range that describes more lines than were actually supplied.
        try:
            source = safe_path(root, item["path"])
            if _secret_path(source):
                continue
            lines = (_read(source) or "").splitlines()
        except (OSError, ValueError, KeyError):
            continue
        start = max(1, int(item.get("line_start") or 1))
        end = min(len(lines), int(item.get("line_end") or start))
        if end < start:
            continue
        snippet = "\n".join(lines[start - 1:end])
        # Keep citation ranges exact even when a source chunk is unusually large.
        snippet_lines = snippet.splitlines()
        if len(snippet_lines) > 80:
            snippet_lines = snippet_lines[:80]
            end = start + len(snippet_lines) - 1
            snippet = "\n".join(snippet_lines)
        text=f"[{item['path']}:{start}-{end}] {item.get('name','')}\n{snippet}"
        if used + len(text) > max_chars:
            if not output and max_chars > 0:
                # Trim whole source lines and update the displayed citation range.
                header, _, body = text.partition("\n")
                budget = max(0, max_chars - len(header) - 1)
                kept=[]; length=0
                for line in body.splitlines():
                    if length + len(line) + (1 if kept else 0) > budget: break
                    kept.append(line); length += len(line) + (1 if len(kept)>1 else 0)
                if not kept: break
                end = start + len(kept) - 1
                text=f"[{item['path']}:{start}-{end}] {item.get('name','')}\n" + "\n".join(kept)
            else: break
        output.append(text); used+=len(text)
        evidence_items.append({**item,"line_start":start,"line_end":end,"excerpt":snippet})
    mode=selected[0].get("rank_mode","keyword") if selected else "keyword"
    return {"query":query,"items":evidence_items,"context":"\n\n".join(output),"characters":used,"max_characters":max_chars,"retrieval_mode":mode}
