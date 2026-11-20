import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import main
from app.agent import orchestrator, project_intelligence


class DebugRepairIntegrationTests(unittest.TestCase):
    def test_interrupted_test_run_is_recovered_without_claiming_a_result(self):
        from app import testing_engine

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); data_root = base / "app-data"
            previous = main.DATA_ROOT; main.DATA_ROOT = data_root
            try:
                run = {"id": "interrupted-run", "project_root": str(base), "status": "running", "execution_id": "owned-process",
                       "command": "python -m unittest", "created_at": "2026-11-18T00:00:00Z", "updated_at": "2026-11-18T00:00:00Z"}
                testing_engine.record(data_root, run)
                terminal = {"execution_id": "owned-process", "status": "interrupted", "completed_at": "2026-11-18T00:01:00Z"}
                with patch.object(main, "coding_terminal_history", return_value=[terminal]):
                    main._recover_testing_runs()
                recovered = testing_engine.history(data_root, root=base)[0]
                self.assertEqual(recovered["status"], "interrupted")
                self.assertEqual(recovered["result"]["status"], "interrupted")
            finally:
                main.DATA_ROOT = previous

    def test_real_node_pass_python_syntax_error_and_missing_dependency(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); data_root = base / "data"
            saved = (main.DB_PATH, main.DB_DIR, main.DATA_ROOT, main.ACTIVE_CODING_WORKSPACE, orchestrator.DB_PATH, project_intelligence.DB_PATH)
            main.DB_PATH = base / "app.db"; main.DB_DIR = base; main.DATA_ROOT = data_root
            try:
                with TestClient(main.app) as client:
                    def select(root):
                        root.mkdir(parents=True, exist_ok=True)
                        response = client.post("/coding/workspace/select", json={"path": str(root)})
                        self.assertEqual(response.status_code, 200, response.text)
                        client.app.state.active_workspace = root
                        return root

                    def run_suite(suite_id):
                        prepared = client.post("/coding/tests/runs/prepare", json={"suite_id": suite_id, "timeout": 30})
                        self.assertEqual(prepared.status_code, 200, prepared.text)
                        run_id = prepared.json()["run"]["id"]
                        started = client.post(f"/coding/tests/runs/{run_id}/execute")
                        self.assertEqual(started.status_code, 200, started.text)
                        for _ in range(400):
                            result = client.get(f"/coding/tests/runs/{run_id}")
                            self.assertEqual(result.status_code, 200, result.text)
                            if result.json()["status"] != "running": return result.json()
                            time.sleep(.025)
                        self.fail(f"{suite_id} did not finish")

                    node = select(base / "node-project")
                    (node / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8")
                    (node / "test").mkdir()
                    (node / "test" / "sample.test.js").write_text("const test = require('node:test'); const assert = require('node:assert/strict'); test('addition', () => assert.equal(1 + 1, 2));\n", encoding="utf-8")
                    node_discovery = client.get("/coding/tests/discovery").json()
                    npm_suite = next(item for item in node_discovery["suites"] if item["id"] == "npm-test:.")
                    self.assertTrue(npm_suite["available"], npm_suite)
                    self.assertIn("npm.cmd", npm_suite["command"])
                    pending = client.post("/coding/tests/runs/prepare", json={"suite_id": npm_suite["id"]})
                    self.assertEqual(pending.status_code, 200, pending.text)
                    rejected = client.post(f"/coding/tests/runs/{pending.json()['run']['id']}/reject")
                    self.assertEqual(rejected.status_code, 200, rejected.text)
                    self.assertEqual(rejected.json()["status"], "rejected")
                    node_result = run_suite(npm_suite["id"])
                    self.assertEqual(node_result["result"]["status"], "passed", node_result)
                    self.assertEqual(node_result["result"]["counts"]["passed"], 1, node_result)

                    (node / "test" / "slow.test.js").write_text("const test = require('node:test'); test('slow', async () => new Promise(resolve => setTimeout(resolve, 20000)));\n", encoding="utf-8")
                    slow_prepared = client.post("/coding/tests/runs/prepare", json={"suite_id": npm_suite["id"], "timeout": 30})
                    self.assertEqual(slow_prepared.status_code, 200, slow_prepared.text)
                    slow_id = slow_prepared.json()["run"]["id"]
                    self.assertEqual(client.post(f"/coding/tests/runs/{slow_id}/execute").status_code, 200)
                    cancelled = client.delete(f"/coding/tests/runs/{slow_id}")
                    self.assertEqual(cancelled.status_code, 200, cancelled.text)
                    for _ in range(100):
                        cancelled_result = client.get(f"/coding/tests/runs/{slow_id}")
                        if cancelled_result.json()["status"] != "running": break
                        time.sleep(.03)
                    self.assertEqual(cancelled_result.json()["result"]["status"], "cancelled", cancelled_result.text)

                    syntax = select(base / "syntax-project")
                    (syntax / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
                    syntax_suite = next(item for item in client.get("/coding/tests/discovery").json()["suites"] if item["id"] == "python-syntax")
                    syntax_result = run_suite(syntax_suite["id"])
                    self.assertEqual(syntax_result["result"]["status"], "failed", syntax_result)
                    self.assertEqual(syntax_result["result"]["category"], "syntax_error", syntax_result)

                    missing = select(base / "missing-dependency-project")
                    (missing / "test_missing.py").write_text("import unittest\nimport phase6_dependency_that_is_not_installed\nclass TestMissing(unittest.TestCase):\n    def test_import(self): self.assertTrue(phase6_dependency_that_is_not_installed)\n", encoding="utf-8")
                    unittest_suite = next(item for item in client.get("/coding/tests/discovery").json()["suites"] if item["id"] == "python-unittest")
                    missing_result = run_suite(unittest_suite["id"])
                    self.assertEqual(missing_result["result"]["status"], "error", missing_result)
                    self.assertEqual(missing_result["result"]["category"], "missing_dependency", missing_result)
            finally:
                main.DB_PATH, main.DB_DIR, main.DATA_ROOT, main.ACTIVE_CODING_WORKSPACE, orchestrator.DB_PATH, project_intelligence.DB_PATH = saved

    def test_real_unittest_failure_approved_fix_and_successful_retest(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp); root = base / "sample-project"; root.mkdir()
            (root / "calc.py").write_text("def multiply(a, b):\n    return a * b + 1\n", encoding="utf-8")
            tests = root / "tests"; tests.mkdir(); (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_calc.py").write_text("import unittest\nfrom calc import multiply\n\nclass TestCalc(unittest.TestCase):\n    def test_multiply(self):\n        self.assertEqual(multiply(2, 3), 6)\n", encoding="utf-8")
            db_path = base / "app.db"; data_root = base / "data"
            saved = (main.DB_PATH, main.DB_DIR, main.DATA_ROOT, main.ACTIVE_CODING_WORKSPACE, orchestrator.DB_PATH, project_intelligence.DB_PATH)
            main.DB_PATH = db_path; main.DB_DIR = base; main.DATA_ROOT = data_root; main.ACTIVE_CODING_WORKSPACE = root
            try:
                with TestClient(main.app) as client:
                    client.app.state.active_workspace = root
                    self.assertEqual(client.post("/coding/workspace/select", json={"path": str(root)}).status_code, 200)
                    scan = client.post("/project-intelligence/scan").json()
                    for _ in range(200):
                        state = client.get(f"/project-intelligence/scan/{scan['job_id']}").json()
                        if state["status"] in {"indexed", "failed", "cancelled"}: break
                        time.sleep(.02)
                    self.assertEqual(state["status"], "indexed", state)
                    with orchestrator.connect() as db:
                        timestamp = orchestrator.now()
                        db.execute("INSERT INTO model_endpoints(id,model_id,name,runtime,endpoint_url,external_model_id,ownership,is_enabled,last_status,created_at,updated_at) VALUES('phase6-local',NULL,'Local test model','mock','http://127.0.0.1:1','mock','external',1,'online',?,?)", (timestamp, timestamp))
                    session = client.post("/agent/sessions", json={"mode": "planning", "model_id": "phase6-local"}).json()
                    session_id = session["id"]
                    plan = {"objective": "Correct the multiply implementation", "requirements": ["Make the test pass"], "assumptions": [], "technology_stack": ["Python"],
                            "architecture_overview": "Use the existing calculator function", "modules": [], "tasks": [{"id": "verify", "title": "Correct multiply and verify", "description": "Fix the defect and run its test", "depends_on": [], "acceptance_criteria": ["The unittest passes"]}], "risks": [], "estimated_complexity": "low"}
                    with patch.object(orchestrator, "_complete", new=AsyncMock(return_value=json.dumps(plan))):
                        planned = client.post(f"/agent/sessions/{session_id}/plan", json={"requirement": "Fix the incorrect multiplication result", "model_id": "phase6-local"})
                    self.assertEqual(planned.status_code, 200, planned.text)
                    task_id = planned.json()["tasks"][0]["id"]
                    self.assertEqual(client.post(f"/agent/sessions/{session_id}/approve-plan", json={"approved": True}).json()["plan_status"], "approved")
                    discovery = client.get("/coding/tests/discovery").json()
                    unittest_suite = next(item for item in discovery["suites"] if item["id"] == "python-unittest")
                    self.assertIn("tests/test_calc.py", unittest_suite["test_files"])

                    def run_suite(repair_of=None):
                        prepared = client.post("/coding/tests/runs/prepare", json={"suite_id": "python-unittest", "session_id": session_id, "task_id": task_id, "repair_of_run_id": repair_of})
                        self.assertEqual(prepared.status_code, 200, prepared.text)
                        self.assertEqual(prepared.json()["run"]["status"], "awaiting_approval")
                        started = client.post(f"/coding/tests/runs/{prepared.json()['run']['id']}/execute")
                        self.assertEqual(started.status_code, 200, started.text)
                        for _ in range(200):
                            result = client.get(f"/coding/tests/runs/{started.json()['id']}")
                            self.assertEqual(result.status_code, 200, result.text)
                            if result.json()["status"] != "running": return result.json()
                            time.sleep(.03)
                        self.fail("Test run did not finish")

                    first = run_suite()
                    self.assertEqual(first["result"]["status"], "failed", first)
                    self.assertEqual(first["result"]["category"], "assertion_failure")
                    self.assertEqual(first["result"]["location"]["path"].replace("\\", "/").split("/")[-2:], ["tests", "test_calc.py"])
                    self.assertTrue(any(item["path"] == "tests/test_calc.py" for item in first["result"]["related_context"]))
                    self.assertTrue(any(item["path"] == "calc.py" for item in first["result"]["related_context"]), first["result"]["related_context"])
                    self.assertIn("a * b + 1", (root / "calc.py").read_text(encoding="utf-8"), "Debugging must not write before review")
                    response = {"diagnosis": {"root_cause": "multiply adds one to the product", "confidence": 0.98, "alternatives": ["The test expectation is wrong"], "evidence_paths": ["calc.py"], "uncertainty": ""},
                                "changes": [{"operation": "modify", "path": "calc.py", "content": "def multiply(a, b):\n    return a * b\n", "summary": "Remove the unintended increment", "task_ids": ["verify"]}]}
                    with patch.object(orchestrator, "_complete", new=AsyncMock(return_value=json.dumps(response))):
                        fix = client.post(f"/agent/sessions/{session_id}/debug/fix", json={"run_id": first["id"], "task_id": task_id, "model_id": "phase6-local"})
                    self.assertEqual(fix.status_code, 200, fix.text)
                    self.assertEqual(fix.json()["repair_attempt"], 1)
                    changeset = fix.json()["changeset"]
                    change = changeset["changes"][0]
                    self.assertIn("return a * b", change["diff"])
                    self.assertIn("a * b + 1", (root / "calc.py").read_text(encoding="utf-8"))
                    reviewed = client.post(f"/agent/sessions/{session_id}/changes/{changeset['id']}/{change['id']}/review", json={"decision": "approved"})
                    self.assertEqual(reviewed.status_code, 200, reviewed.text)
                    applied = client.post(f"/agent/sessions/{session_id}/changes/{changeset['id']}/apply", json={"unsaved_paths": []})
                    self.assertEqual(applied.status_code, 200, applied.text)
                    self.assertIn("return a * b\n", (root / "calc.py").read_text(encoding="utf-8"))
                    second = run_suite(first["id"])
                    self.assertEqual(second["result"]["status"], "passed", second)
                    report = client.get("/coding/tests/report", params={"session_id": session_id}).json()
                    self.assertEqual(report["passed_runs"], 1)
                    self.assertEqual(report["failed_runs"], 1)
            finally:
                main.DB_PATH, main.DB_DIR, main.DATA_ROOT, main.ACTIVE_CODING_WORKSPACE, orchestrator.DB_PATH, project_intelligence.DB_PATH = saved


if __name__ == "__main__":
    unittest.main()
