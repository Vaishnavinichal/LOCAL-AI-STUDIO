"""Project-aware test discovery, result parsing and local run history."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from app.agent import project_intelligence

MAX_RUN_OUTPUT = 200_000
_history_lock = threading.RLock()


def redact(text: str) -> str:
    text = re.sub(r"(?i)(password|passwd|token|api[_-]?key|secret)(\s*[:=]\s*)[^\s;]+", r"\1\2[REDACTED]", text)
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", text)
    return text[-MAX_RUN_OUTPUT:]


def _test_file(path: Path) -> bool:
    name = path.name.casefold()
    return (name.startswith(("test_", "tests.")) or ".test." in name or ".spec." in name
            or "test" in {part.casefold() for part in path.parts})


def _inside(root: Path, path: Path) -> str | None:
    try:
        resolved = path.resolve()
        return resolved.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def discover(root: Path) -> dict:
    root = root.resolve(strict=True)
    files = project_intelligence.enumerate_files(root, max_files=20_000)
    relative = [path.relative_to(root).as_posix() for path in files]
    test_files = sorted(path for path in relative if _test_file(Path(path)))
    suites = []

    py_files = [p for p in files if p.suffix.casefold() == ".py"]
    has_python_manifest = any((root / name).is_file() for name in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"))
    if py_files or has_python_manifest:
        test_deps = "\n".join((root / name).read_text(encoding="utf-8", errors="replace")
                               for name in ("pyproject.toml", "requirements.txt", "setup.cfg") if (root / name).is_file())
        local_python = next((root / candidate for candidate in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python", "venv/bin/python") if (root / candidate).is_file()), None)
        python_command = f'& "{local_python}"' if local_python else "python"
        pytest_declared = bool(re.search(r"(?i)pytest", test_deps)) or any("pytest" in path.casefold() for path in test_files)
        pytest_available = bool(local_python and ((local_python.parent / "pytest.exe").exists() or (local_python.parent / "pytest").exists())) or bool(shutil.which("pytest")) or importlib.util.find_spec("pytest") is not None
        python_test_files = [p for p in test_files if p.casefold().endswith(".py")]
        if pytest_declared or python_test_files:
            suites.append({"id": "python-pytest", "label": "Python · pytest", "framework": "pytest", "kind": "tests",
                           "command": f"{python_command} -m pytest -v", "available": pytest_available,
                           "missing_reason": "pytest is not available in the detected Python environment. No dependency was installed.",
                           "test_files": python_test_files})
        if python_test_files or "unittest" in test_deps.casefold():
            suites.append({"id": "python-unittest", "label": "Python · unittest", "framework": "unittest", "kind": "tests",
                           "command": f"{python_command} -m unittest discover -v", "available": True, "missing_reason": "",
                           "test_files": python_test_files})
        if py_files:
            suites.append({"id": "python-syntax", "label": "Python · syntax compilation", "framework": "compileall", "kind": "syntax",
                           "command": f"{python_command} -m compileall -q .", "available": bool(local_python or shutil.which("python")),
                           "missing_reason": "Python was not found on PATH.", "test_files": [p.relative_to(root).as_posix() for p in py_files[:500]]})

    package_files = [p for p in files if p.name.casefold() == "package.json" and (root / p.relative_to(root)).parent == root]
    if not package_files and (root / "package.json").is_file(): package_files = [root / "package.json"]
    for manifest in package_files:
        try: package = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            suites.append({"id": "node-config-error", "label": "Node.js · package.json", "framework": "npm", "kind": "configuration", "command": "", "available": False, "missing_reason": "package.json is not valid JSON.", "test_files": []})
            continue
        scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
        dependencies = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
        pkg_dir = manifest.parent
        rel_dir = pkg_dir.relative_to(root).as_posix() if pkg_dir != root else "."
        local_modules = (pkg_dir / "node_modules").is_dir()
        files_here = [p for p in test_files if rel_dir == "." or p.startswith(rel_dir + "/")]
        project_files = [p for p in relative if rel_dir == "." or p.startswith(rel_dir + "/")]
        test_script = scripts.get("test")
        if test_script or files_here:
            framework = "vitest" if "vitest" in dependencies or "vitest" in str(test_script).casefold() else "jest" if "jest" in dependencies or "jest" in str(test_script).casefold() else "npm test"
            # On Windows, PowerShell execution policy may block npm.ps1.
            command = "npm.cmd test -- --run" if framework == "vitest" else "npm.cmd test -- --runInBand" if framework == "jest" else "npm.cmd test"
            dependencies_available = local_modules or not dependencies
            suites.append({"id": f"npm-test:{rel_dir}", "label": f"Node.js · {framework} tests" + (f" ({rel_dir})" if rel_dir != "." else ""), "framework": framework, "kind": "tests", "command": command,
                           "working_directory": rel_dir, "available": bool(test_script and dependencies_available and shutil.which("node")),
                           "missing_reason": "A test script or a declared dependency is missing from node_modules. Dependencies are never installed automatically." if not test_script or (dependencies and not local_modules) else "Node.js is not available on PATH." if not shutil.which("node") else "",
                           "test_files": files_here})
        if scripts.get("build"):
            suites.append({"id": f"npm-build:{rel_dir}", "label": f"Node.js · production build" + (f" ({rel_dir})" if rel_dir != "." else ""), "framework": "npm", "kind": "build", "command": "npm.cmd run build", "working_directory": rel_dir,
                           "available": local_modules, "missing_reason": "node_modules is missing. Dependencies are never installed automatically." if not local_modules else "", "test_files": []})
        if scripts.get("typecheck") or scripts.get("type-check"):
            script = "typecheck" if scripts.get("typecheck") else "type-check"
            suites.append({"id": f"npm-typecheck:{rel_dir}", "label": f"TypeScript · {script}", "framework": "typescript", "kind": "typecheck", "command": f"npm.cmd run {script}", "working_directory": rel_dir,
                           "available": local_modules, "missing_reason": "node_modules is missing. Dependencies are never installed automatically." if not local_modules else "", "test_files": []})
        elif any(Path(p).suffix.casefold() in {".ts", ".tsx"} for p in project_files) and (pkg_dir / "tsconfig.json").is_file():
            tsc_path = pkg_dir / "node_modules/.bin/tsc.cmd"
            suites.append({"id": f"npm-typecheck:{rel_dir}", "label": f"TypeScript · tsc ({rel_dir})", "framework": "typescript", "kind": "typecheck", "command": "./node_modules/.bin/tsc.cmd --noEmit", "working_directory": rel_dir,
                           "available": tsc_path.exists(), "missing_reason": "Local TypeScript compiler is missing. Dependencies are never installed automatically." if not tsc_path.exists() else "", "test_files": []})

    if not suites:
        suites.append({"id": "no-tests-found", "label": "No supported test command discovered", "framework": "unknown", "kind": "none", "command": "", "available": False,
                       "missing_reason": "No recognized Python or Node.js test configuration or test files were found.", "test_files": []})
    return {"project_root": str(root), "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "test_files": test_files, "suites": suites}


def failure_signature(failure: dict) -> str:
    normalized = " ".join(str(failure.get("message", "")).casefold().split())[:240]
    return "|".join((str(failure.get("category", "unknown")), str(failure.get("failed_test", "")), normalized))


def repair_stop_reason(failure: dict, attempts: list[dict], limit: int = 2) -> str | None:
    if len(attempts) >= max(1, limit): return "attempt_limit"
    if attempts and attempts[-1].get("retest_run_id") and attempts[-1].get("failure_signature") == failure_signature(failure): return "repeated_failure"
    if float(failure.get("confidence", 1.0)) < 0.55: return "low_confidence"
    return None


def classify(output: str, *, command: str, suite_kind: str, exit_code: int | None, timed_out=False, cancelled=False) -> dict:
    raw = redact(output)
    lower = raw.casefold()
    categories = [
        ("cancelled", cancelled, "Execution cancelled by the user"),
        ("timeout", timed_out, "Test command exceeded its approved timeout"),
        ("missing_dependency", bool(re.search(r"no module named|module not found|cannot find module|could not resolve|not recognized as the name|command not found|npm err! missing script", lower)), "A required dependency or command is unavailable"),
        ("syntax_error", bool(re.search(r"syntaxerror|syntax error|unexpected token|was never closed|eol while scanning", lower)), "The source or test code could not be parsed"),
        ("import_error", bool(re.search(r"importerror|modulenotfounderror|cannot import name|failed to import", lower)), "A project module could not be imported"),
        ("type_error", bool(re.search(r"typeerror|type error|ts\d{3,5}:|type .* is not assignable", lower)), "A type check or runtime type operation failed"),
        ("assertion_failure", bool(re.search(r"assertionerror|assertion failed|assert .* failed|expected .* (?:got|received)|tests? failed", lower)), "A test assertion failed"),
        ("failed_api_request", bool(re.search(r"http\s*[45]\d\d|status code [45]\d\d|request failed with status", lower)), "A request returned an error status"),
        ("database_connection", bool(re.search(r"database.*(?:connect|connection)|connection refused|unable to open database|operationalerror.*locked", lower)), "A database connection or query failed"),
        ("configuration_error", bool(re.search(r"invalid config|configuration error|error in .*config|tsconfig.*error|failed to load config", lower)), "Project test or build configuration is invalid"),
        ("test_environment", bool(re.search(r"permission denied|access is denied|no such file or directory|path not found|environmenterror|no tests ran|no tests found", lower)) or (suite_kind == "tests" and exit_code == 5), "The test environment could not access a required resource or discover runnable tests"),
        ("runtime_exception", bool(re.search(r"traceback \(most recent call last\)|uncaught exception|fatal error|exception:", lower)), "The program raised an unhandled exception"),
    ]
    category, matched, default_message = next(((key, ok, message) for key, ok, message in categories if ok), ("build_failure" if suite_kind in {"build", "typecheck", "syntax"} else "unknown", bool(exit_code), "Command failed"))
    failed_tests = []
    for pattern in (r"(?m)^FAILED\s+([^\s]+)", r"(?m)^(\S+\.py::\S+)\s+(?:FAILED|ERROR)", r"(?m)^(test\S+)\s+\.\.\.\s+FAIL"):
        failed_tests.extend(re.findall(pattern, raw))
    failed_tests.extend(re.findall(r"(?m)^\s*FAIL\s+([^\s]+)", raw))
    failed_tests.extend(re.findall(r"(?m)^\s*●\s+(.+)$", raw))
    failed_tests = list(dict.fromkeys(failed_tests))[:100]
    counts = {"passed": None, "failed": None, "skipped": None, "total": None}
    match = re.search(r"(?i)(\d+)\s+failed(?:,\s*(\d+)\s+passed)?(?:,\s*(\d+)\s+skipped)?", raw)
    if match:
        counts.update(failed=int(match.group(1)), passed=int(match.group(2) or 0), skipped=int(match.group(3) or 0))
    else:
        match = re.search(r"(?i)(\d+)\s+passed(?:,\s*(\d+)\s+failed)?(?:,\s*(\d+)\s+skipped)?", raw)
        if match: counts.update(passed=int(match.group(1)), failed=int(match.group(2) or 0), skipped=int(match.group(3) or 0))
        match = re.search(r"(?i)Ran\s+(\d+)\s+tests?", raw)
        if match and counts["passed"] is None:
            counts["total"] = int(match.group(1))
            if "failed" in lower or "errors=" in lower: counts["failed"] = 1
            elif "ok" in lower: counts["passed"] = counts["total"]
    unittest_skipped = re.search(r"(?i)skipped=(\d+)", raw)
    if unittest_skipped and counts["total"] is not None:
        counts["skipped"] = int(unittest_skipped.group(1))
        if counts["passed"] is not None: counts["passed"] = max(0, counts["total"] - counts["skipped"] - (counts["failed"] or 0))
    tap_tests = re.search(r"(?m)^# tests (\d+)\s*$", raw)
    tap_pass = re.search(r"(?m)^# pass (\d+)\s*$", raw)
    tap_fail = re.search(r"(?m)^# fail (\d+)\s*$", raw)
    tap_skip = re.search(r"(?m)^# (?:skipped|todo) (\d+)\s*$", raw)
    if tap_tests:
        counts.update(total=int(tap_tests.group(1)), passed=int(tap_pass.group(1)) if tap_pass else 0,
                      failed=int(tap_fail.group(1)) if tap_fail else 0, skipped=int(tap_skip.group(1)) if tap_skip else 0)
    # Node's built-in test runner emits TAP with an informational Unicode marker.
    node_tap = {key: re.search(rf"(?m)^\s*(?:\u2139\s*)?{label} (\d+)\s*$", raw)
                for key, label in (("total", "tests"), ("passed", "pass"), ("failed", "fail"), ("skipped", "skipped"))}
    if node_tap["total"]:
        counts.update({key: int(match.group(1)) if match else 0 for key, match in node_tap.items()})
    jest = re.search(r"(?i)Tests:\s*(?:(\d+) failed,?\s*)?(?:(\d+) passed,?\s*)?(?:(\d+) skipped,?\s*)?(\d+) total", raw)
    if jest:
        counts.update(failed=int(jest.group(1) or 0), passed=int(jest.group(2) or 0), skipped=int(jest.group(3) or 0), total=int(jest.group(4)))
    vitest = re.search(r"(?i)Tests\s+(?:(\d+) failed\s*\|\s*)?(?:(\d+) passed)?(?:\s*\|\s*(\d+) skipped)?\s*\((\d+)\)", raw)
    if vitest:
        counts.update(failed=int(vitest.group(1) or 0), passed=int(vitest.group(2) or 0), skipped=int(vitest.group(3) or 0), total=int(vitest.group(4)))
    failed_tests.extend(re.findall(r"(?m)^\s*FAIL:\s*(\S+)", raw))
    test_cases = []
    for match in re.finditer(r"(?m)^\s*(.+?)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped(?:\s+.*)?)\s*$", raw, re.IGNORECASE):
        state = "passed" if match.group(2).casefold() == "ok" else "skipped" if match.group(2).casefold().startswith("skipped") else "failed"
        test_cases.append({"name": match.group(1), "status": state})
    for match in re.finditer(r"(?m)^\s*(\S+::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR)\s*$", raw, re.IGNORECASE):
        test_cases.append({"name": match.group(1), "status": "passed" if match.group(2).casefold() == "passed" else "skipped" if match.group(2).casefold() == "skipped" else "failed"})
    for match in re.finditer(r"(?m)^\s*\u2714\s+(.+?)\s+\([\d.]+ms\)\s*$", raw):
        test_cases.append({"name": match.group(1), "status": "passed"})
    for match in re.finditer(r"(?m)^\s*\u2716\s+(.+?)(?:\s+\([\d.]+ms\))?\s*$", raw):
        test_cases.append({"name": match.group(1), "status": "failed"})
    if counts["total"] is None and counts["passed"] is not None:
        counts["total"] = counts["passed"] + (counts["failed"] or 0) + (counts["skipped"] or 0)
    did_run_tests = bool(failed_tests) or (counts["total"] or 0) > 0
    no_test_execution_error = suite_kind == "tests" and (category == "missing_dependency" or (not did_run_tests and category in {"syntax_error", "import_error", "configuration_error", "test_environment"}))
    status = "cancelled" if cancelled else "timed_out" if timed_out else "error" if exit_code not in (0, None) and no_test_execution_error else "failed" if exit_code not in (0, None) or failed_tests or (counts["failed"] or 0) > 0 else "passed_with_skips" if exit_code == 0 and suite_kind == "tests" and (counts["skipped"] or 0) > 0 else "passed" if exit_code == 0 and (suite_kind != "tests" or (counts["total"] or 0) > 0) else "unparsed"
    if status == "passed": category = "none"; default_message = "Command completed successfully"
    elif status == "unparsed": category = "unparsed"; default_message = "Command exited successfully, but its test result format could not be verified"
    headline = next((line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith(("at ", "File \""))), default_message)[:500]
    location = None
    for pattern in (r'File "([^"\n]+)", line (\d+)', r"(?m)^\s*File\s+([^\s:]+):(\d+):", r"(?m)^\s*at\s+.*?\(?([^()\s]+):(\d+)(?::\d+)?\)?"):
        match = re.search(pattern, raw)
        if match:
            location = {"path": match.group(1), "line": int(match.group(2))}; break
    stack_lines = [line for line in raw.splitlines() if re.search(r"Traceback|File \"|\.py:\d+|\.tsx?:\d+|\.jsx?:\d+|\bat\s+", line)]
    return {"status": status, "category": category, "category_message": default_message, "message": headline,
            "failed_tests": failed_tests, "failed_test": failed_tests[0] if failed_tests else "", "counts": counts,
            "test_cases": test_cases[:500], "location": location, "stack_trace": "\n".join(stack_lines)[:16_000], "output": raw}


def related_context(root: Path, failure: dict, *, limit=8) -> list[dict]:
    queries = []
    results = []
    seen = set()
    location = failure.get("location") or {}
    if location.get("path"):
        path = Path(location["path"])
        rel = _inside(root, path if path.is_absolute() else root / path)
        if rel:
            queries.append(rel)
            try:
                source = project_intelligence.safe_path(root, rel)
                lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
                line = max(1, min(len(lines), int(location.get("line") or 1)))
                start, end = max(1, line-8), min(len(lines), line+8)
                excerpt = "\n".join(f"{number}: {lines[number-1]}" for number in range(start, end+1))
                results.append({"path": rel, "line_start": start, "line_end": end, "excerpt": excerpt, "symbol": None, "score": 100, "failure_location": True})
                seen.add(rel)
            except (OSError, ValueError): pass
    for failed_test in failure.get("failed_tests", [])[:2]:
        queries.append(failed_test)
        # Split test_case names such as test_multiply into searchable project
        # symbols so the function under test is retrieved, not just its test.
        queries.append(re.sub(r"[_./\\:]+", " ", str(failed_test)))
        queries.extend(re.findall(r"[A-Za-z][A-Za-z0-9]*", str(failed_test)))
    queries.extend([failure.get("message", ""), failure.get("category", "")])
    for query in queries:
        try: hits = project_intelligence.search(root, query, kind="keyword", limit=limit)
        except sqlite3.Error: hits = []
        for hit in hits:
            rel = str(hit.get("path", ""))
            if rel in seen: continue
            try:
                path = project_intelligence.safe_path(root, rel)
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, ValueError): continue
            start = max(1, int(hit.get("line_start") or 1))
            end = min(len(lines), max(start, int(hit.get("line_end") or start)))
            excerpt = "\n".join(f"{number}: {lines[number-1]}" for number in range(max(1, start-5), min(len(lines), end+5)+1))
            results.append({"path": rel, "line_start": max(1,start-5), "line_end": min(len(lines),end+5), "excerpt": excerpt, "symbol": hit.get("name"), "score": hit.get("score")})
            seen.add(rel)
            if len(results) >= limit: return results
    return results


def history_path(data_root: Path) -> Path:
    return data_root / "testing_history.jsonl"


def record(data_root: Path, item: dict) -> None:
    path = history_path(data_root); path.parent.mkdir(parents=True, exist_ok=True)
    safe = redact(json.dumps(item, ensure_ascii=False))
    with _history_lock:
        with path.open("a", encoding="utf-8") as handle: handle.write(safe + "\n")


def history(data_root: Path, *, root: Path | None = None, limit=100) -> list[dict]:
    path = history_path(data_root)
    if not path.exists(): return []
    latest = {}
    with _history_lock:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try: item = json.loads(line)
            except json.JSONDecodeError: continue
            latest[item.get("id", str(uuid.uuid4()))] = item
    rows = list(latest.values())
    if root: rows = [item for item in rows if item.get("project_root") == str(root.resolve())]
    return sorted(rows, key=lambda item: item.get("updated_at", ""), reverse=True)[:max(1,min(limit,200))]


def update_run(data_root: Path, run_id: str, **updates) -> dict:
    rows = history(data_root, limit=200)
    current = next((item for item in rows if item.get("id") == run_id), None)
    if not current: raise KeyError(run_id)
    current.update(updates); current["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record(data_root, current)
    return current
