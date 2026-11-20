"""Live local-model debugging acceptance smoke; project and app data are temporary."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "app" / "backend"))

with tempfile.TemporaryDirectory(prefix="local-ai-phase6-") as temporary:
    base = Path(temporary)
    os.environ["LOCAL_AI_HOME"] = str(base / "app-data")
    project = base / "sample-project"
    project.mkdir()
    (project / "calc.py").write_text("def multiply(a, b):\n    return a * b + 1\n", encoding="utf-8")
    (project / "test_calc.py").write_text(
        "import unittest\nfrom calc import multiply\n\nclass TestCalc(unittest.TestCase):\n    def test_multiply(self):\n        self.assertEqual(multiply(2, 3), 6)\n",
        encoding="utf-8",
    )

    from fastapi.testclient import TestClient
    from app import main
    from app.agent import orchestrator
    from unittest.mock import AsyncMock, patch

    with TestClient(main.app) as client:
        client.app.state.active_workspace = project
        selected = client.post("/coding/workspace/select", json={"path": str(project)})
        assert selected.status_code == 200, selected.text
        assert client.post("/models/discover").status_code == 200
        models = client.get("/agent/models/roles").json()["models"]
        planner = next((m for m in models if m["name"] == "qwen2.5:3b" and m["runtime"] == "ollama"), None)
        coder = next((m for m in models if m["name"] == "qwen2.5-coder:7b" and m["runtime"] == "ollama"), None)
        assert planner and coder, "The local Qwen planner and coder models are required"
        scan = client.post("/project-intelligence/scan")
        assert scan.status_code == 200, scan.text
        for _ in range(300):
            state = client.get(f"/project-intelligence/scan/{scan.json()['job_id']}").json()
            if state["status"] in {"indexed", "failed", "cancelled"}:
                break
            time.sleep(.1)
        assert state["status"] == "indexed", state

        started = time.monotonic()
        created = client.post("/agent/sessions", json={"mode": "planning", "model_id": planner["id"]})
        assert created.status_code == 200, created.text
        session_id = created.json()["id"]
        requirement = "Fix multiply so it returns the product without adding one, and keep the existing test unchanged."
        plan = {"objective": requirement, "requirements": ["multiply returns the exact product"], "assumptions": [], "technology_stack": ["Python"],
                "architecture_overview": "Correct the existing function and verify its existing unittest", "modules": [],
                "tasks": [{"id": "fix-multiply", "title": "Correct multiply", "description": requirement, "depends_on": [], "acceptance_criteria": ["The existing unittest passes"]}],
                "risks": [], "estimated_complexity": "low"}
        # The live path under test is the CPU-heavy coding diagnosis; keep planning
        # deterministic so its separate 90-second default cannot mask that result.
        with patch.object(orchestrator, "_complete", new=AsyncMock(return_value=json.dumps(plan))):
            planned = client.post(f"/agent/sessions/{session_id}/plan", json={"requirement": requirement, "model_id": planner["id"]})
        assert planned.status_code == 200, planned.text
        task_id = planned.json()["tasks"][0]["id"]
        assert client.post(f"/agent/sessions/{session_id}/approve-plan", json={"approved": True}).status_code == 200

        suite = next(item for item in client.get("/coding/tests/discovery").json()["suites"] if item["id"] == "python-unittest")
        prepared = client.post("/coding/tests/runs/prepare", json={"suite_id": suite["id"], "session_id": session_id, "task_id": task_id})
        assert prepared.status_code == 200, prepared.text
        run_id = prepared.json()["run"]["id"]
        failed = client.post(f"/coding/tests/runs/{run_id}/execute")
        assert failed.status_code == 200, failed.text
        for _ in range(400):
            result = client.get(f"/coding/tests/runs/{run_id}").json()
            if result["status"] != "running":
                break
            time.sleep(.1)
        assert result["result"]["status"] == "failed", result
        assert "a * b + 1" in (project / "calc.py").read_text(encoding="utf-8")

        print("Diagnosis and correction proposal using the installed local coding model…", flush=True)
        proposal = client.post(f"/agent/sessions/{session_id}/debug/fix", json={"run_id": run_id, "task_id": task_id, "model_id": coder["id"], "max_attempts": 1})
        assert proposal.status_code == 200, proposal.text
        payload = proposal.json()
        assert payload["changeset"] and payload["changeset"]["changes"], json.dumps(payload, indent=2)
        changeset = payload["changeset"]
        print(json.dumps({"diagnosis": payload["diagnosis"], "diffs": [{"path": change["path"], "diff": change["diff"]} for change in changeset["changes"]]}, indent=2), flush=True)
        for change in changeset["changes"]:
            approved = client.post(f"/agent/sessions/{session_id}/changes/{changeset['id']}/{change['id']}/review", json={"decision": "approved"})
            assert approved.status_code == 200, approved.text
        applied = client.post(f"/agent/sessions/{session_id}/changes/{changeset['id']}/apply", json={"unsaved_paths": []})
        assert applied.status_code == 200, applied.text

        retest = client.post("/coding/tests/runs/prepare", json={"suite_id": suite["id"], "session_id": session_id, "task_id": task_id, "repair_of_run_id": run_id})
        assert retest.status_code == 200, retest.text
        retest_id = retest.json()["run"]["id"]
        assert client.post(f"/coding/tests/runs/{retest_id}/execute").status_code == 200
        for _ in range(400):
            final = client.get(f"/coding/tests/runs/{retest_id}").json()
            if final["status"] != "running":
                break
            time.sleep(.1)
        assert final["result"]["status"] == "passed", final
        report = client.get("/coding/tests/report", params={"session_id": session_id}).json()
        assert report["passed_runs"] == 1 and report["failed_runs"] == 1, report
        print(json.dumps({"result": "passed", "plan_generation": "deterministic approved fixture", "coding_model": coder["name"], "first_run": result["result"]["status"], "retest": final["result"]["status"], "elapsed_seconds": round(time.monotonic() - started, 1)}))
