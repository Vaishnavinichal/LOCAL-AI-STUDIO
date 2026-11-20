"""Real local coding-model integration smoke in an isolated temporary project/database."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "app" / "backend"))

with tempfile.TemporaryDirectory(prefix="local-ai-agent-calculator-") as temp:
    base = Path(temp)
    os.environ["LOCAL_AI_HOME"] = str(base / "app-data")
    os.environ["AGENT_CODING_MAX_GENERATED_TOKENS"] = "4500"
    root = base / "calculator-project"
    root.mkdir()
    original_calculator = "def add(left, right):\n    return left + right\n"
    original_tests = "import unittest\nfrom calculator import add\n\nclass CalculatorTests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\nif __name__ == '__main__':\n    unittest.main()\n"
    (root / "calculator.py").write_text(original_calculator, encoding="utf-8")
    (root / "test_calculator.py").write_text(original_tests, encoding="utf-8")

    from fastapi.testclient import TestClient
    from app import main
    from app.agent import orchestrator

    with TestClient(main.app) as client:
        selected = client.post("/coding/workspace/select", json={"path": str(root)})
        assert selected.status_code == 200, selected.text
        discovered = client.post("/models/discover")
        assert discovered.status_code == 200, discovered.text
        models = client.get("/agent/models/roles").json()["models"]
        coder = next(model for model in models if model["name"] == "qwen2.5-coder:7b" and model["runtime"] == "ollama")
        client.put("/agent/models/roles", json={"role": "coding", "model_id": coder["id"]})
        session = client.post("/agent/sessions", json={"mode": "planning", "model_id": coder["id"]}).json()
        session_id = session["id"]
        requirement = "Add subtract(left, right) to calculator.py and add a unittest for it in test_calculator.py, preserving add."
        planned = {"objective": "Add subtraction to the calculator", "requirements": ["Implement subtraction", "Test subtraction"],
                   "assumptions": [], "technology_stack": ["Python", "unittest"], "architecture_overview": "Extend the existing calculator module and test file.",
                   "modules": [], "tasks": [
                       {"id": "subtraction", "title": "Add subtract function", "description": "Add subtract(left, right) preserving add.", "depends_on": [], "acceptance_criteria": ["subtract returns left minus right"]},
                       {"id": "test-subtraction", "title": "Test subtract function", "description": "Add a unittest for subtraction.", "depends_on": ["subtraction"], "acceptance_criteria": ["Test verifies subtraction"]}],
                   "risks": [], "estimated_complexity": "low", "verification_command": "python -m unittest -v"}
        with patch.object(orchestrator, "_complete", new=AsyncMock(return_value=json.dumps(planned))):
            plan_response = client.post(f"/agent/sessions/{session_id}/plan", json={"requirement": requirement, "model_id": coder["id"]})
        assert plan_response.status_code == 200, plan_response.text
        approved = client.post(f"/agent/sessions/{session_id}/approve-plan", json={"approved": True})
        assert approved.status_code == 200, approved.text
        assert (root / "calculator.py").read_text(encoding="utf-8") == original_calculator
        assert (root / "test_calculator.py").read_text(encoding="utf-8") == original_tests

        start = time.monotonic()
        proposal_response = client.post(f"/agent/sessions/{session_id}/changes/generate", json={"model_id": coder["id"]})
        assert proposal_response.status_code == 200, proposal_response.text
        proposal = proposal_response.json()
        paths = {item["path"] for item in proposal["changes"]}
        assert paths == {"calculator.py", "test_calculator.py"}, json.dumps(proposal, indent=2)
        assert all(item["diff"].startswith("--- ") for item in proposal["changes"])
        assert "subtract" in next(item["content"] for item in proposal["changes"] if item["path"] == "calculator.py")
        assert "subtract" in next(item["content"] for item in proposal["changes"] if item["path"] == "test_calculator.py")
        # Proposal generation is review-only; prove that no workspace file has changed.
        assert (root / "calculator.py").read_text(encoding="utf-8") == original_calculator
        assert (root / "test_calculator.py").read_text(encoding="utf-8") == original_tests

        for change in proposal["changes"]:
            reviewed = client.post(f"/agent/sessions/{session_id}/changes/{proposal['id']}/{change['id']}/review", json={"decision": "approved"})
            assert reviewed.status_code == 200, reviewed.text
        applied = client.post(f"/agent/sessions/{session_id}/changes/{proposal['id']}/apply", json={"unsaved_paths": []})
        assert applied.status_code == 200, applied.text
        result = __import__("subprocess").run([sys.executable, "-m", "unittest", "-v"], cwd=root, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr

        command_preview = client.post("/coding/terminal/preview", json={"command": "Write-Output 'calculator-preview'", "reason": "Approved plan verification", "session_id": session_id})
        assert command_preview.status_code == 200, command_preview.text
        assert command_preview.json()["approval_required"] is True
        rejected = client.post(f"/coding/terminal/reject/{command_preview.json()['id']}")
        assert rejected.status_code == 200 and rejected.json()["executed"] is False
        execute_preview = client.post("/coding/terminal/preview", json={"command": "python -m unittest -v", "reason": "Explicitly approved calculator retest", "session_id": session_id})
        assert execute_preview.status_code == 200, execute_preview.text
        assert execute_preview.json()["approval_required"] is True
        assert (root / "calculator.py").read_text(encoding="utf-8").startswith("def subtract") or "subtract" in (root / "calculator.py").read_text(encoding="utf-8")
        terminal_started = client.post(f"/coding/terminal/execute/{execute_preview.json()['id']}")
        assert terminal_started.status_code == 200, terminal_started.text
        deadline = time.monotonic() + 30
        terminal_result = None
        while time.monotonic() < deadline:
            terminal_result = client.get(f"/coding/terminal/executions/{terminal_started.json()['execution_id']}").json()
            if terminal_result["status"] not in {"running", "cancel_requested"}:
                break
            time.sleep(0.1)
        assert terminal_result and terminal_result["status"] == "completed" and terminal_result["exit_code"] == 0, terminal_result
        rollback = client.post(f"/agent/sessions/{session_id}/changes/{proposal['id']}/rollback", json={"unsaved_paths": []})
        assert rollback.status_code == 200, rollback.text
        assert (root / "calculator.py").read_text(encoding="utf-8") == original_calculator
        print(json.dumps({"result": "passed", "model": coder["name"], "files_proposed": sorted(paths), "approved_apply": applied.json()["status"],
                          "retest_exit_code": result.returncode, "preview_rejected_without_execution": rejected.json()["executed"],
                          "terminal_approval_exit_code": terminal_result["exit_code"], "rollback": rollback.json()["status"],
                          "generation_seconds": round(time.monotonic() - start, 1)}))
