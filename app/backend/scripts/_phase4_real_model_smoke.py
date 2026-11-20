"""Temporary acceptance smoke for a real local model; all files/data live in a temp root."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "app" / "backend"))

with tempfile.TemporaryDirectory(prefix="local-ai-phase4-") as temporary:
    base = Path(temporary)
    os.environ["LOCAL_AI_HOME"] = str(base / "app-data")
    os.environ["AGENT_CODING_MAX_GENERATED_TOKENS"] = "6000"
    project = base / "sample-project"
    project.mkdir()
    original_app = "def greet(name):\n    return f'Hello, {name}!'\n"
    original_test = "import unittest\nfrom sample import greet\n\nclass SampleTests(unittest.TestCase):\n    def test_greet(self):\n        self.assertEqual(greet('Ada'), 'Hello, Ada!')\n\nif __name__ == '__main__':\n    unittest.main()\n"
    (project / "sample.py").write_text(original_app, encoding="utf-8")
    (project / "test_sample.py").write_text(original_test, encoding="utf-8")

    from fastapi.testclient import TestClient
    from app import main

    with TestClient(main.app) as client:
        client.app.state.active_workspace = project
        selected = client.post("/coding/workspace/select", json={"path": str(project)})
        assert selected.status_code == 200, selected.text
        discovery = client.post("/models/discover")
        assert discovery.status_code == 200, discovery.text
        models = client.get("/agent/models/roles").json()["models"]
        planner = next((m for m in models if m["name"] == "qwen2.5:3b" and m["runtime"] == "ollama"), None)
        coder = next((m for m in models if m["name"] == "qwen2.5-coder:7b" and m["runtime"] == "ollama"), None)
        assert planner and coder, "Local qwen2.5:3b planner and qwen2.5-coder:7b coding models are required for this live smoke"
        for role, model in (("planner",planner),("coding",coder)):
            assigned = client.put("/agent/models/roles", json={"role": role, "model_id": model["id"]})
            assert assigned.status_code == 200, assigned.text

        started = time.monotonic()
        print("Using local planner model", flush=True)
        created = client.post("/agent/sessions", json={"mode": "planning", "model_id": planner["id"]})
        assert created.status_code == 200, created.text
        session_id = created.json()["id"]
        requirement = "Add farewell(name) to sample.py returning Goodbye, {name}! and add a unittest for it in test_sample.py. Keep the existing greet behavior."
        plan_response = client.post(f"/agent/sessions/{session_id}/plan", json={"requirement": requirement, "model_id": planner["id"]})
        assert plan_response.status_code == 200, plan_response.text
        assert plan_response.json().get("plan"), plan_response.text
        plan_approval = client.post(f"/agent/sessions/{session_id}/approve-plan", json={"approved": True})
        assert plan_approval.status_code == 200 and plan_approval.json()["plan_status"] == "approved", plan_approval.text

        print("Plan approved; generating coordinated file proposal with local coding model", flush=True)
        generated = client.post(f"/agent/sessions/{session_id}/changes/generate", json={"model_id": coder["id"]})
        assert generated.status_code == 200, generated.text
        proposal = generated.json()
        paths = {change["path"] for change in proposal["changes"]}
        assert paths == {"sample.py", "test_sample.py"}, json.dumps(proposal["changes"], indent=2)
        by_path = {change["path"]: change for change in proposal["changes"]}
        if "farewell" not in by_path["sample.py"]["content"] or "test_farewell" not in by_path["test_sample.py"]["content"]:
            print("Local model proposal did not meet the sample acceptance criteria:", json.dumps(proposal["changes"], indent=2), flush=True)
            raise AssertionError("The generated diffs did not implement the requested farewell function and test")
        # The returned unified diffs are inspected before these explicit approvals.
        assert all(change["diff"].startswith("--- ") for change in proposal["changes"])
        for change in proposal["changes"]:
            reviewed = client.post(f"/agent/sessions/{session_id}/changes/{proposal['id']}/{change['id']}/review", json={"decision": "approved"})
            assert reviewed.status_code == 200, reviewed.text
        applied = client.post(f"/agent/sessions/{session_id}/changes/{proposal['id']}/apply", json={"unsaved_paths": []})
        assert applied.status_code == 200, applied.text
        assert "farewell" in (project / "sample.py").read_text(encoding="utf-8")
        assert "test_farewell" in (project / "test_sample.py").read_text(encoding="utf-8")
        rolled_back = client.post(f"/agent/sessions/{session_id}/changes/{proposal['id']}/rollback", json={"unsaved_paths": []})
        assert rolled_back.status_code == 200, rolled_back.text
        assert (project / "sample.py").read_text(encoding="utf-8") == original_app
        assert (project / "test_sample.py").read_text(encoding="utf-8") == original_test
        print(json.dumps({"result": "passed", "planner": planner["name"], "coder": coder["name"], "project": "temporary two-file Python sample", "generated_paths": sorted(paths), "elapsed_seconds": round(time.monotonic() - started, 1), "rollback": rolled_back.json()["status"]}))
