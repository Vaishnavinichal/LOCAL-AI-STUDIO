"""Exercise the confirmed failed session over HTTP using a read-only DB snapshot."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SESSION_ID = "27916ce4-59da-44ec-915b-f4abf47894c3"
SOURCE_DB = Path(r"D:\LOCAL-AI-STUDIO\data\database\local_ai.db")
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "app" / "backend"))

if not SOURCE_DB.is_file():
    raise SystemExit(f"The reported database was not found: {SOURCE_DB}")

with tempfile.TemporaryDirectory(prefix="local-ai-session-read-api-") as temporary:
    local_home = Path(temporary) / "local-home"
    database = local_home / "database" / "local_ai.db"
    database.parent.mkdir(parents=True)
    source = sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(database)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    os.environ["LOCAL_AI_HOME"] = str(local_home)
    from fastapi.testclient import TestClient
    from app import main

    main.ACTIVE_CODING_WORKSPACE = None
    with TestClient(main.app) as client:
        client.app.state.active_workspace = None
        events = client.get(f"/agent/sessions/{SESSION_ID}/events")
        plan = client.get(f"/agent/sessions/{SESSION_ID}/plan")
        session = client.get(f"/agent/sessions/{SESSION_ID}")
        assert events.status_code == 200, (events.status_code, events.text)
        assert plan.status_code == 404, (plan.status_code, plan.text)
        assert session.status_code == 200 and session.json()["status"] == "failed", (session.status_code, session.text)
        event_types = [item["event_type"] for item in events.json()]
        assert event_types == ["session_created", "planning_started"], event_types
        assert plan.json() == {"detail": "No plan exists for this session"}, plan.json()
        print({"database": "isolated read-only snapshot", "active_project": None,
               "session_http": session.status_code, "session_status": session.json()["status"],
               "failure_reason": session.json()["error"],
               "events_http": events.status_code, "event_types": event_types,
               "plan_http": plan.status_code, "plan_response": plan.json()})
