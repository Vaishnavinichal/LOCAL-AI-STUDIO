"""Local multi-file change proposals with explicit review and compensating rollback."""
from __future__ import annotations

import difflib
import base64
import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_CHANGESET_BYTES = 8_000_000
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock(changeset_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(changeset_id, threading.RLock())


def _relative(value: str) -> str:
    value = str(value or "").replace("\\", "/")
    if not value or value.startswith("/") or PureWindowsPath(value).is_absolute() or ":" in value:
        raise ValueError("Change paths must be relative to the selected project")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Change paths cannot contain empty, current, or parent-directory segments")
    lowered=[part.casefold() for part in parts]
    if any(part in {".git","node_modules",".venv","venv","__pycache__"} for part in lowered):
        raise ValueError("Generated changes cannot target version-control, dependency, or cache directories")
    filename=lowered[-1]
    if filename.startswith(".env") or any(token in filename for token in ("secret","credential")) or Path(filename).suffix in {".pem",".key",".p12",".pfx"}:
        raise ValueError("Generated changes cannot target secret or credential files")
    return PurePosixPath(*parts).as_posix()


def resolve(root: Path, relative_path: str, *, allow_missing: bool = True) -> Path:
    root = root.resolve(strict=True)
    normalized = _relative(relative_path)
    candidate = root / Path(*normalized.split("/"))
    current = root
    for part in normalized.split("/"):
        current = current / part
        if current.is_symlink():
            raise ValueError("Symbolic links cannot be changed by the coding agent")
    resolved = candidate.resolve(strict=not allow_missing)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Change path escapes the selected project") from exc
    return resolved


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > 1_000_000:
        raise ValueError("Files larger than 1 MB are not supported in a proposed changeset")
    if b"\x00" in raw:
        raise ValueError("Binary files cannot be included in a coding changeset")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Only UTF-8 text files can be included in a coding changeset") from exc


def _hash(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def _file_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _diff(path: str, old: str | None, new: str | None, new_path: str | None = None) -> str:
    old_name = f"a/{path}" if old is not None else "/dev/null"
    target = new_path or path
    new_name = f"b/{target}" if new is not None else "/dev/null"
    return "".join(difflib.unified_diff((old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True), fromfile=old_name, tofile=new_name))


def _manifest_path(storage: Path, changeset_id: str) -> Path:
    if not changeset_id or any(c not in "0123456789abcdef-" for c in changeset_id.lower()):
        raise ValueError("Invalid changeset identifier")
    return storage / f"{changeset_id}.json"


def _save(storage: Path, manifest: dict) -> None:
    storage.mkdir(parents=True, exist_ok=True)
    target = _manifest_path(storage, manifest["id"])
    fd, temporary = tempfile.mkstemp(prefix=".changeset-", suffix=".tmp", dir=storage)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(storage: Path, changeset_id: str) -> dict:
    with _manifest_path(storage, changeset_id).open(encoding="utf-8") as stream:
        return json.load(stream)


def _event(manifest: dict, kind: str, message: str, change_id: str | None = None) -> None:
    manifest.setdefault("events", []).append({"id": str(uuid.uuid4()), "type": kind, "message": message, "change_id": change_id, "created_at": _now()})


def _public(manifest: dict) -> dict:
    result = {k: v for k, v in manifest.items() if k not in {"root", "changes"}}
    result["changes"] = [{k: v for k, v in item.items() if k not in {"original_bytes_b64"}} for item in manifest["changes"]]
    return result


def prepare(root: Path, changes: list[dict], storage: Path, *, session_id: str | None = None, unsaved_paths: list[str] | None = None) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir() or not changes or len(changes) > 40:
        raise ValueError("A changeset must contain between 1 and 40 file operations")
    normalized_unsaved = {_relative(path).casefold() for path in (unsaved_paths or [])}
    seen: set[str] = set()
    entries = []
    total_bytes = 0
    for raw in changes:
        operation = str(raw.get("operation", ""))
        if operation not in {"create", "modify", "delete", "rename"}:
            raise ValueError(f"Unsupported file operation: {operation}")
        path = _relative(raw.get("path", ""))
        new_path = _relative(raw["new_path"]) if operation == "rename" else None
        touched = {path, new_path} - {None}
        touched_keys={item.casefold() for item in touched}
        if seen.intersection(touched_keys):
            raise ValueError("A changeset cannot include conflicting operations for the same path")
        seen.update(touched_keys)
        if touched_keys.intersection(normalized_unsaved):
            raise ValueError(f"Unsaved editor changes conflict with {path}; save or discard them before generating changes")
        source = resolve(root, path)
        destination = resolve(root, new_path) if new_path else None
        if operation in {"modify", "delete", "rename"}:
            if not source.exists() or not source.is_file():
                raise ValueError(f"Expected an existing text file for {operation}: {path}")
            old = _read_text(source)
            original_bytes_b64 = base64.b64encode(source.read_bytes()).decode("ascii")
        else:
            if source.exists():
                raise ValueError(f"Cannot create {path}; the path already exists")
            old = None
            original_bytes_b64 = None
        if destination and destination.exists():
            raise ValueError(f"Cannot rename to {new_path}; the path already exists")
        new = raw.get("content") if operation in {"create", "modify"} else None
        if operation in {"create", "modify"} and not isinstance(new, str):
            raise ValueError(f"Generated content is required for {operation}: {path}")
        if isinstance(new, str):
            total_bytes += len(new.encode("utf-8"))
        if old is not None:
            total_bytes += len(old.encode("utf-8"))
        if total_bytes > MAX_CHANGESET_BYTES:
            raise ValueError("The proposed changeset exceeds the 8 MB review limit")
        diff_new = old if operation == "rename" else new
        entries.append({
            "id": str(uuid.uuid4()), "operation": operation, "path": path, "new_path": new_path,
            "summary": str(raw.get("summary", ""))[:500], "task_ids": raw.get("task_ids", []),
            "original_content": old, "original_bytes_b64": original_bytes_b64, "content": new, "baseline_hash": _file_hash(source) if old is not None else None,
            "diff": _diff(path, old, diff_new, new_path), "status": "pending", "applied_hash": None, "created_dirs": [],
        })
    manifest = {"id": str(uuid.uuid4()), "root": str(root), "session_id": session_id, "status": "review", "created_at": _now(), "updated_at": _now(), "changes": entries, "events": [], "applied_change_ids": [], "snapshot_created_at": None}
    _event(manifest, "proposal_created", f"Prepared {len(entries)} file change(s)")
    _save(storage, manifest)
    return _public(manifest)


def review(storage: Path, changeset_id: str, change_id: str, decision: str) -> dict:
    if decision not in {"approved", "rejected"}:
        raise ValueError("Decision must be approved or rejected")
    with _lock(changeset_id):
        manifest = load(storage, changeset_id)
        if manifest["status"] not in {"review", "partially_applied"}:
            raise ValueError("This changeset is no longer open for review")
        change = next((c for c in manifest["changes"] if c["id"] == change_id), None)
        if not change:
            raise KeyError("Change not found")
        if change["status"] == "applied":
            raise ValueError("An applied change cannot be reviewed again")
        change["status"] = decision
        manifest["updated_at"] = _now()
        _event(manifest, f"change_{decision}", f"{decision.title()} {change['path']}", change_id)
        _save(storage, manifest)
        return _public(manifest)


def review_all(storage: Path, changeset_id: str, decision: str) -> dict:
    manifest = load(storage, changeset_id)
    for change in manifest["changes"]:
        if change["status"] == "pending" or (decision == "rejected" and change["status"] == "approved"):
            review(storage, changeset_id, change["id"], decision)
    return _public(load(storage, changeset_id))


def _verify_baseline(root: Path, change: dict) -> None:
    source = resolve(root, change["path"])
    if change["operation"] == "create":
        if source.exists():
            raise ValueError(f"Conflict: {change['path']} was created after the proposal")
    else:
        if not source.exists() or not source.is_file() or _file_hash(source) != change["baseline_hash"]:
            raise ValueError(f"Conflict: {change['path']} changed after the proposal")
    if change["new_path"] and resolve(root, change["new_path"]).exists():
        raise ValueError(f"Conflict: rename destination {change['new_path']} now exists")


def _write_text(path: Path, value: str) -> None:
    _write_bytes(path, value.encode("utf-8"))


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _create_parent_dirs(root: Path, path: Path) -> list[str]:
    missing=[]; parent=path.parent
    while parent != root and not parent.exists():
        missing.append(parent.relative_to(root).as_posix()); parent=parent.parent
    for relative in reversed(missing):
        resolve(root,relative).mkdir(exist_ok=True)
    return missing


def apply(storage: Path, changeset_id: str, *, before_write=None, unsaved_paths: list[str] | None = None) -> dict:
    with _lock(changeset_id):
        manifest = load(storage, changeset_id)
        if manifest["status"] not in {"review", "partially_applied"}:
            raise ValueError("This changeset cannot be applied in its current state")
        selected = [c for c in manifest["changes"] if c["status"] == "approved"]
        if not selected:
            raise ValueError("Approve at least one file before applying this changeset")
        unsaved = {_relative(path).casefold() for path in (unsaved_paths or [])}
        for change in selected:
            if {path.casefold() for path in ({change["path"], change["new_path"]} - {None})} & unsaved:
                raise ValueError(f"Unsaved editor changes conflict with {change['path']}; save or discard them before applying")
        root = Path(manifest["root"]).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("The selected project root is unavailable")
        for change in selected:
            _verify_baseline(root, change)
        manifest["status"] = "applying"
        manifest["snapshot_created_at"] = _now()
        manifest["updated_at"] = _now()
        _event(manifest, "snapshot_created", "Captured original file contents before applying approved changes")
        _save(storage, manifest)
        try:
            # Callback persists each preimage in the application's existing version history.
            if before_write:
                for change in selected:
                    if change["original_content"] is not None:
                        before_write(root, change["path"], change["original_content"])
            for change in selected:
                _verify_baseline(root,change)
                source = resolve(root, change["path"])
                if change["operation"] in {"create", "modify"}:
                    change["created_dirs"]=_create_parent_dirs(root,source)
                    _write_text(source, change["content"])
                    change["applied_hash"] = _hash(change["content"])
                elif change["operation"] == "delete":
                    source.unlink()
                else:
                    destination = resolve(root, change["new_path"])
                    change["created_dirs"]=_create_parent_dirs(root,destination)
                    source.rename(destination)
                    change["applied_hash"] = change["baseline_hash"]
                change["status"] = "applied"
                manifest["applied_change_ids"].append(change["id"])
                _event(manifest, "change_applied", f"Applied {change['operation']} {change['path']}", change["id"])
                _save(storage, manifest)
        except Exception as exc:
            _restore_selected(root, selected)
            for change in selected:
                if change["status"] == "applied":
                    change["status"] = "approved"
                    change["applied_hash"] = None
            manifest["applied_change_ids"] = [i for i in manifest["applied_change_ids"] if i not in {c["id"] for c in selected}]
            manifest["status"] = "apply_failed_rolled_back"
            manifest["updated_at"] = _now()
            _event(manifest, "apply_rolled_back", f"Apply failed and approved files were restored: {type(exc).__name__}")
            _save(storage, manifest)
            raise
        manifest["status"] = "partially_applied" if any(c["status"] == "pending" for c in manifest["changes"]) else "applied"
        manifest["updated_at"] = _now()
        _event(manifest, "apply_completed", f"Applied {len(selected)} approved file change(s)")
        _save(storage, manifest)
        return _public(manifest)


def _restore_selected(root: Path, selected: list[dict]) -> None:
    for change in reversed(selected):
        source = resolve(root, change["path"])
        destination = resolve(root, change["new_path"]) if change["new_path"] else None
        if destination and destination.exists():
            if destination.is_file(): destination.unlink()
        if change["operation"] == "create":
            if source.exists() and source.is_file(): source.unlink()
        elif change["original_content"] is not None:
            _write_bytes(source, base64.b64decode(change["original_bytes_b64"]))
    for change in reversed(selected):
        for relative in change.get("created_dirs", []):
            try: resolve(root,relative,allow_missing=False).rmdir()
            except (OSError,ValueError): pass


def rollback(storage: Path, changeset_id: str, *, before_restore=None, unsaved_paths: list[str] | None = None) -> dict:
    with _lock(changeset_id):
        manifest = load(storage, changeset_id)
        if manifest["status"] not in {"applied", "partially_applied"}:
            raise ValueError("Only applied changes can be rolled back")
        root = Path(manifest["root"]).resolve(strict=True)
        applied = [c for c in manifest["changes"] if c["status"] == "applied"]
        unsaved = {_relative(path).casefold() for path in (unsaved_paths or [])}
        for change in applied:
            if {path.casefold() for path in ({change["path"], change["new_path"]} - {None})} & unsaved:
                raise ValueError(f"Unsaved editor changes conflict with rollback of {change['path']}; save or discard them first")
        # Avoid overwriting edits made after apply.
        for change in applied:
            current_path = change["new_path"] if change["operation"] == "rename" else change["path"]
            target = resolve(root, current_path)
            if change["operation"] == "delete":
                if target.exists(): raise ValueError(f"Rollback conflict: {change['path']} now exists")
            elif not target.exists() or not target.is_file() or _file_hash(target) != change["applied_hash"]:
                raise ValueError(f"Rollback conflict: {current_path} changed after apply")
        if before_restore:
            for change in applied:
                current_path = change["new_path"] if change["operation"] == "rename" else change["path"]
                target = resolve(root, current_path)
                if target.exists() and target.is_file():
                    before_restore(root, current_path)
        for change in applied:
            current_path = change["new_path"] if change["operation"] == "rename" else change["path"]
            target = resolve(root, current_path)
            if change["operation"] == "delete":
                if target.exists(): raise ValueError(f"Rollback conflict: {change['path']} now exists")
            elif not target.exists() or not target.is_file() or _file_hash(target) != change["applied_hash"]:
                raise ValueError(f"Rollback conflict: {current_path} changed after apply")
        _restore_selected(root, applied)
        for change in applied:
            change["status"] = "rolled_back"
            _event(manifest, "change_rolled_back", f"Rolled back {change['path']}", change["id"])
        manifest["status"] = "rolled_back"
        manifest["updated_at"] = _now()
        _save(storage, manifest)
        return _public(manifest)


def recover_interrupted(storage: Path, root: Path) -> list[str]:
    """Compensate interrupted applies only when disk state matches the operation's pre/postimage."""
    root = root.resolve(strict=True)
    if not storage.exists():
        return []
    recovered = []
    for manifest_path in storage.glob("*.json"):
        changeset_id = manifest_path.stem
        with _lock(changeset_id):
            manifest = load(storage, changeset_id)
            if manifest.get("status") != "applying" or Path(manifest.get("root", "")).resolve() != root:
                continue
            selected = [c for c in manifest["changes"] if c["status"] in {"approved", "applied"}]
            safe = True
            for change in selected:
                source = resolve(root, change["path"])
                destination = resolve(root, change["new_path"]) if change["new_path"] else None
                source_hash = _file_hash(source) if source.exists() and source.is_file() else None
                dest_hash = _file_hash(destination) if destination and destination.exists() and destination.is_file() else None
                if change["operation"] == "create":
                    safe = source_hash in {None, _hash(change["content"])}
                elif change["operation"] == "modify":
                    safe = source_hash in {change["baseline_hash"], _hash(change["content"])}
                elif change["operation"] == "delete":
                    safe = source_hash in {None, change["baseline_hash"]}
                else:
                    safe = (source_hash == change["baseline_hash"] and dest_hash is None) or (source_hash is None and dest_hash == change["baseline_hash"])
                if not safe:
                    break
            if safe:
                _restore_selected(root, selected)
                manifest["status"] = "apply_failed_rolled_back"
                _event(manifest, "interrupted_apply_recovered", "Restored the changeset preimage after an interrupted apply")
            else:
                manifest["status"] = "recovery_conflict"
                _event(manifest, "interrupted_apply_conflict", "Recovery stopped because project files changed after the interrupted apply")
            manifest["updated_at"] = _now()
            _save(storage, manifest)
            recovered.append(changeset_id)
    return recovered
