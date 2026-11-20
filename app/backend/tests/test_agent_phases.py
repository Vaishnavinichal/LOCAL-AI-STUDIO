import json
import asyncio
import os
import time
from unittest.mock import AsyncMock, patch
import tempfile
import unittest
from pathlib import Path
import httpx

from app.agent import orchestrator, project_intelligence


class ProjectIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sample"
        self.root.mkdir()
        project_intelligence.initialize(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_detects_languages_framework_entry_tests_and_python_symbols(self):
        (self.root / "package.json").write_text(json.dumps({"dependencies":{"react":"^19","vite":"^8"}}),encoding="utf-8")
        (self.root / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n\ndef health():\n    return {'ok': True}\n",encoding="utf-8")
        (self.root / "test_main.py").write_text("def test_health():\n    assert True\n",encoding="utf-8")
        result=project_intelligence.scan_and_index(self.root)
        self.assertEqual(result["status"],"indexed")
        self.assertIn("Python",result["summary"]["languages"])
        self.assertIn("React",result["summary"]["frameworks"])
        self.assertIn("FastAPI",result["summary"]["frameworks"])
        self.assertIn("main.py",result["summary"]["entry_points"])
        self.assertTrue(result["summary"]["tests"])
        names={item.get("name") for item in project_intelligence.search(self.root,"health")}
        self.assertIn("health",names)

    def test_excludes_secrets_generated_directories_and_path_traversal(self):
        (self.root / ".env").write_text("PASSWORD=do-not-index",encoding="utf-8")
        (self.root / "credential.txt").write_text("api_key = 'supersecret123'",encoding="utf-8")
        (self.root / "config.py").write_text("access_key = 'AKIA1234567890123456'",encoding="utf-8")
        (self.root / "safe.py").write_text("def safe(): return True\n",encoding="utf-8")
        (self.root / "node_modules").mkdir(); (self.root / "node_modules" / "noise.js").write_text("function noise() {}",encoding="utf-8")
        files=project_intelligence.enumerate_files(self.root)
        names={p.name for p in files}
        self.assertEqual(names,{"safe.py","config.py"})
        with self.assertRaises(ValueError): project_intelligence.safe_path(self.root,"../outside.txt")
        result=project_intelligence.scan_and_index(self.root)
        self.assertEqual(project_intelligence.search(self.root,"supersecret"),[])
        self.assertEqual(result["files_indexed"],2)

    def test_syntax_errors_do_not_abort_scan(self):
        (self.root / "broken.py").write_text("def broken(:\n",encoding="utf-8")
        (self.root / "valid.py").write_text("class Useful:\n    pass\n",encoding="utf-8")
        result=project_intelligence.scan_and_index(self.root)
        self.assertEqual(result["status"],"indexed")
        self.assertEqual(len(result["parse_errors"]),1)
        self.assertTrue(any(x.get("name")=="Useful" for x in project_intelligence.search(self.root,"Useful")))

    def test_incremental_hash_change_and_keyword_context_bound(self):
        source=self.root/"module.py"; source.write_text("def original(): return 1\n",encoding="utf-8")
        first=project_intelligence.scan_and_index(self.root)
        with project_intelligence.connect() as db:
            first_chunk=db.execute("select content_hash from project_chunks where project_key=?",(first["project_key"],)).fetchone()[0]
        again=project_intelligence.scan_and_index(self.root)
        with project_intelligence.connect() as db:
            again_chunk=db.execute("select content_hash from project_chunks where project_key=?",(again["project_key"],)).fetchone()[0]
        self.assertEqual(first_chunk,again_chunk)
        source.write_text("def revised(): return 'needle'\n",encoding="utf-8")
        project_intelligence.scan_and_index(self.root)
        self.assertTrue(project_intelligence.search(self.root,"needle"))
        ctx=project_intelligence.context(self.root,"needle",max_chars=256)
        self.assertLessEqual(ctx["characters"],256)
        source.write_text("def revised(): return 'needle changed'\n",encoding="utf-8")
        self.assertEqual(project_intelligence.get_status(self.root)["status"],"outdated")

    def test_context_returns_exact_lines_and_ranges_after_truncation(self):
        source=self.root/"module.py"
        source.write_text("needle_unique\n"+"\n".join(f"line_{i}" for i in range(2,121)),encoding="utf-8")
        project_intelligence.scan_and_index(self.root)
        ctx=project_intelligence.context(self.root,"needle_unique",max_chars=280)
        item=ctx["items"][0]
        excerpt=ctx["context"].split("\n",1)[1]
        self.assertEqual(item["line_start"],1)
        self.assertEqual(item["line_end"],len(excerpt.splitlines()))
        self.assertLessEqual(ctx["characters"],280)
        self.assertTrue(excerpt.startswith("needle_unique"))

    def test_dependency_relationships_and_semantic_keyword_fallback(self):
        (self.root/"src").mkdir(); (self.root/"requirements.txt").write_text("fastapi>=0.1\n",encoding="utf-8")
        (self.root/"src"/"util.py").write_text("def helper(): return 1\n",encoding="utf-8")
        (self.root/"src"/"main.py").write_text("from .util import helper\nimport fastapi\nimport missing_package\n",encoding="utf-8")
        result=project_intelligence.scan_and_index(self.root)
        with project_intelligence.connect() as db:
            edges=db.execute("SELECT target,resolution FROM project_dependencies WHERE project_key=?",(result["project_key"],)).fetchall()
        edges={(row["target"],row["resolution"]) for row in edges}
        self.assertIn(("src/util.py","internal"),edges)
        self.assertIn(("fastapi","external"),edges)
        self.assertIn(("missing_package","unresolved"),edges)
        semantic=project_intelligence.search(self.root,"helper",kind="semantic")
        self.assertTrue(all(item["rank_mode"]=="keyword" for item in semantic))

    def test_configurable_exclusions(self):
        (self.root/"src").mkdir(); (self.root/"src"/"keep.py").write_text("def keep(): pass\n",encoding="utf-8")
        (self.root/"generated").mkdir(); (self.root/"generated"/"bundle.js").write_text("function generated() {}",encoding="utf-8")
        result=project_intelligence.scan_and_index(self.root,exclusions=["generated"])
        self.assertEqual(result["files_indexed"],1)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        orchestrator.initialize(Path(self.tmp.name)/"agent.db")
        with orchestrator.connect() as db:
            db.execute("CREATE TABLE model_registry(id TEXT PRIMARY KEY,name TEXT,runtime TEXT,source_type TEXT,status TEXT,metadata_json TEXT,file_size_bytes INTEGER)")
            db.execute("INSERT INTO model_registry VALUES('local','qwen2.5:3b','ollama','ollama','installed','{}',3000000000)")
            db.execute("INSERT INTO model_registry VALUES('cloud','glm:cloud','ollama','ollama','installed','{\"is_cloud\":true}',0)")

    def tearDown(self): self.tmp.cleanup()

    def _plan(self, depends=None):
        return {"objective":"Add health endpoint","requirements":["Expose health"],"assumptions":[],"technology_stack":["Python"],"architecture_overview":"Add one route","modules":[],"tasks":[{"id":"a","title":"Add route","depends_on":depends or [],"acceptance_criteria":["Returns 200"]}],"risks":[],"estimated_complexity":"low"}

    def test_citation_validation_rejects_unretrieved_paths_and_ranges(self):
        items=[{"path":"app/main.py","line_start":4,"line_end":12}]
        self.assertEqual(orchestrator._invalid_citations("Uses FastAPI [app/main.py:5-8]",items),[])
        self.assertTrue(orchestrator._invalid_citations("Uses Flask [app/main.py:1-20]",items))
        self.assertTrue(orchestrator._invalid_citations("Uses Flask [app/other.py:1-2]",items))

    def test_structured_plan_validation_and_dependency_cycles(self):
        self.assertEqual(orchestrator._validate_plan(self._plan())["tasks"][0]["id"],"a")
        proposed=self._plan(); proposed["project_structure"]=[{"path":"src/app.py","purpose":"Entry point"}]; proposed["commands"]=[{"command":"python -m unittest","reason":"Run tests"}]
        normalized=orchestrator._validate_plan(proposed)
        self.assertEqual(normalized["project_structure"][0]["path"],"src/app.py")
        self.assertEqual(normalized["commands"][0]["command"],"python -m unittest")
        unsafe=self._plan(); unsafe["project_structure"]=[{"path":"src/../../outside.py"}]
        with self.assertRaises(ValueError): orchestrator._validate_plan(unsafe)
        bad=self._plan(); bad["tasks"][0]["depends_on"]=["missing"]
        with self.assertRaises(ValueError): orchestrator._validate_plan(bad)
        cyclic=self._plan(); cyclic["tasks"][0]["depends_on"]=["a"]
        with self.assertRaises(ValueError): orchestrator._validate_plan(cyclic)
        with self.assertRaises(json.JSONDecodeError): orchestrator._parse_json("not json")

    def test_approved_plan_tasks_are_ordered_by_dependencies(self):
        plan={"tasks":[{"id":"dependent","title":"Add calculator CLI","depends_on":["core"],"acceptance_criteria":[]},{"id":"core","title":"Add calculator module","depends_on":[],"acceptance_criteria":[]}]}
        persisted=[{"id":"db-dependent","title":"Add calculator CLI","dependencies":["db-core"]},{"id":"db-core","title":"Add calculator module","dependencies":[]}]
        result=orchestrator._ordered_approved_plan_tasks(plan,persisted)
        self.assertEqual([task["id"] for task in result],["core","dependent"])

    def test_persistent_session_events_and_status_constraints(self):
        now=orchestrator.now()
        with orchestrator.connect() as db:
            db.execute("INSERT INTO agent_sessions(id,project_key,project_root,mode,status,requirement,model_id,created_at,updated_at,error) VALUES(?,?,?,?,?,?,?,?,?,?)",("s1","project","/tmp/project","planning","ready","Add route",None,now,now,""))
            orchestrator.event(db,"s1","session_created","created")
            event=db.execute("SELECT event_type,message FROM agent_events WHERE session_id='s1'").fetchone()
            self.assertEqual(tuple(event),("session_created","created"))
            with self.assertRaises(Exception):
                db.execute("UPDATE agent_sessions SET status='not-a-state' WHERE id='s1'")

    def test_missing_model_cloud_rejection_and_resource_aware_selection(self):
        with orchestrator.connect() as db:
            with self.assertRaises(Exception): orchestrator._selected_model(db,"missing")
            with self.assertRaises(Exception): orchestrator._selected_model(db,"cloud")
            model,endpoint=orchestrator._selected_model(db,"local")
            self.assertEqual(model["id"],"local"); self.assertIsNone(endpoint)
        models=[{"id":"large","name":"qwen2.5-coder:32b","runtime":"ollama","file_size_bytes":30_000_000_000,"metadata":{}},{"id":"small","name":"qwen2.5:3b","runtime":"ollama","file_size_bytes":3_000_000_000,"metadata":{}},{"id":"cloud","name":"model:cloud","runtime":"ollama","file_size_bytes":0,"metadata":{"is_cloud":True}}]
        recommendations=orchestrator.recommend_models(models,"planner",available_bytes=8_000_000_000)
        self.assertEqual([row["id"] for row in recommendations],["small"])

    def test_benchmark_records_persist_and_runtime_failure_is_detectable(self):
        with orchestrator.connect() as db:
            model,_=orchestrator._selected_model(db,"local")
            with self.assertRaises(Exception):
                orchestrator._selected_model(db,"not-installed")
        previous=os.environ.get("OLLAMA_URL"); os.environ["OLLAMA_URL"]="http://127.0.0.1:1"
        try:
            with self.assertRaises(Exception): asyncio.run(orchestrator._complete(model,None,"small prompt"))
        finally:
            if previous is None: os.environ.pop("OLLAMA_URL",None)
            else: os.environ["OLLAMA_URL"]=previous
        with orchestrator.connect() as db:
            db.execute("INSERT INTO agent_benchmarks VALUES(?,?,?,?,?,?)",("b1","local","qwen2.5:3b","ollama",json.dumps({"structured_output":{"status":"invalid"}}),orchestrator.now()))
            stored=db.execute("SELECT results_json FROM agent_benchmarks WHERE id='b1'").fetchone()
        self.assertEqual(json.loads(stored[0])["structured_output"]["status"],"invalid")

    def _run_http_mock(self, handler, profile="default", observed=None):
        real_client=httpx.AsyncClient
        transport=httpx.MockTransport(handler)
        def client_factory(**kwargs):
            if observed is not None: observed["timeout"]=kwargs.get("timeout")
            return real_client(transport=transport,**kwargs)
        with patch.object(orchestrator.httpx,"AsyncClient",side_effect=client_factory):
            return asyncio.run(orchestrator._complete_metrics({"id":"local","name":"qwen2.5:3b","runtime":"ollama"},None,"safe test prompt",profile=profile))

    def test_inference_mocked_ollama_success_and_request_model(self):
        import httpx
        seen={}
        def handler(request):
            seen["url"]=str(request.url); seen["body"]=json.loads(request.content)
            return httpx.Response(200,json={"response":"Answer","eval_count":5,"eval_duration":1000})
        result=self._run_http_mock(handler)
        self.assertEqual(result["text"],"Answer")
        self.assertEqual(seen["body"]["model"],"qwen2.5:3b")
        self.assertTrue(seen["url"].endswith("/api/generate"))

    def test_ask_inference_uses_long_cpu_settings_without_changing_default_profile(self):
        seen={}; client_settings={}
        def handler(request):
            seen["body"]=json.loads(request.content)
            return httpx.Response(200,json={"response":"Answer","eval_count":3})
        self._run_http_mock(handler,profile="ask",observed=client_settings)
        self.assertEqual(seen["body"]["keep_alive"],"5m")
        self.assertEqual(seen["body"]["options"]["num_predict"],600)
        self.assertEqual(client_settings["timeout"].read,300)
        self.assertEqual(client_settings["timeout"].connect,5)
        defaults=orchestrator._inference_settings()
        self.assertEqual(defaults,{"read_timeout_seconds":90.0,"connect_timeout_seconds":5.0,"keep_alive":"0","max_generated_tokens":1800})

    def test_coding_inference_allows_slow_local_file_generation(self):
        settings=orchestrator._inference_settings("coding")
        self.assertEqual(settings["read_timeout_seconds"],900.0)
        self.assertEqual(settings["connect_timeout_seconds"],5.0)

    def test_planning_inference_allows_slow_local_models(self):
        settings=orchestrator._inference_settings("planning")
        self.assertEqual(settings["read_timeout_seconds"],300.0)
        self.assertEqual(settings["connect_timeout_seconds"],5.0)

    def test_inference_mocked_http_error_and_timeout_have_categories(self):
        import httpx
        with self.assertRaises(orchestrator.InferenceFailure) as status:
            self._run_http_mock(lambda request:httpx.Response(503,text="runtime unavailable"))
        self.assertEqual(status.exception.category,"http_status")
        def timeout(request): raise httpx.ReadTimeout("")
        with self.assertRaises(orchestrator.InferenceFailure) as timed_out:
            self._run_http_mock(timeout,profile="ask")
        self.assertEqual(timed_out.exception.category,"timeout")
        self.assertTrue(str(timed_out.exception))

    def test_inference_mocked_invalid_json_empty_payload_and_connect_error(self):
        import httpx
        with self.assertRaises(orchestrator.InferenceFailure) as invalid_json:
            self._run_http_mock(lambda request:httpx.Response(200,content=b"{broken",headers={"content-type":"application/json"}))
        self.assertEqual(invalid_json.exception.category,"invalid_json")
        with self.assertRaises(orchestrator.InferenceFailure) as unexpected:
            self._run_http_mock(lambda request:httpx.Response(200,json={"done":True}))
        self.assertEqual(unexpected.exception.category,"unexpected_response")
        def disconnected(request): raise httpx.ConnectError("")
        with self.assertRaises(orchestrator.InferenceFailure) as connection:
            self._run_http_mock(disconnected)
        self.assertEqual(connection.exception.category,"connection")

    def test_safe_endpoint_and_error_details_redact_credentials(self):
        self.assertEqual(orchestrator._safe_endpoint("https://user:pass@example.test:8443/api?token=private"),"https://example.test:8443/api")
        safe=orchestrator._safe_error_detail("Bearer abc123 api_key=secret123 password:pass123")
        self.assertNotIn("abc123",safe); self.assertNotIn("secret123",safe); self.assertNotIn("pass123",safe)


class AgentApiIntegrationTests(unittest.TestCase):
    def test_read_only_session_routes_work_without_an_active_project_and_retry_is_explicit(self):
        from fastapi.testclient import TestClient
        from app import main
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"stored-project"; root.mkdir(); (root/"main.py").write_text("def hello(): return 'hello'\n",encoding="utf-8")
            db_path=Path(tmp)/"app.db"; data_root=Path(tmp)/"data"
            globals_before=(main.DB_PATH,main.DB_DIR,main.DATA_ROOT,main.ACTIVE_CODING_WORKSPACE)
            main.DB_PATH=db_path;main.DB_DIR=db_path.parent;main.DATA_ROOT=data_root;main.ACTIVE_CODING_WORKSPACE=root
            try:
                with TestClient(main.app) as client:
                    client.app.state.active_workspace=root
                    created=client.post("/agent/sessions",json={"mode":"planning","model_id":"mock-endpoint"})
                    self.assertEqual(created.status_code,200,created.text); session_id=created.json()["id"]
                    now=orchestrator.now()
                    with orchestrator.connect() as db:
                        db.execute("INSERT INTO model_endpoints(id,model_id,name,runtime,endpoint_url,external_model_id,ownership,is_enabled,last_status,created_at,updated_at) VALUES('mock-endpoint',NULL,'Mock local','mock','http://127.0.0.1:1','mock','external',1,'online',?,?)",(now,now))
                        db.execute("UPDATE agent_sessions SET status='failed',requirement='Add a documented hello function',error='Planning was interrupted by application shutdown' WHERE id=?",(session_id,))
                        orchestrator.event(db,session_id,"planning_started","Planning started",{})
                    client.app.state.active_workspace=None
                    main.ACTIVE_CODING_WORKSPACE=None
                    events=client.get(f"/agent/sessions/{session_id}/events")
                    self.assertEqual(events.status_code,200,events.text)
                    self.assertEqual([item["event_type"] for item in events.json()], ["session_created","planning_started"])
                    no_plan=client.get(f"/agent/sessions/{session_id}/plan")
                    self.assertEqual(no_plan.status_code,404)
                    self.assertEqual(no_plan.json()["detail"],"No plan exists for this session")
                    failed=client.get(f"/agent/sessions/{session_id}")
                    self.assertEqual(failed.status_code,200,failed.text)
                    self.assertEqual(failed.json()["status"],"failed")
                    self.assertEqual(failed.json()["error"],"Planning was interrupted by application shutdown")
                    self.assertEqual(client.get(f"/agent/sessions/{session_id}/history").status_code,200)
                    self.assertIn(session_id,[row["id"] for row in client.get("/agent/sessions").json()])
                    self.assertEqual(client.get("/agent/sessions/missing/events").status_code,404)
                    self.assertEqual(client.get("/agent/sessions/missing/plan").status_code,404)
                    self.assertEqual(client.post("/coding/terminal/preview",json={"command":"Write-Output restricted","reason":"Must remain project-bound","session_id":session_id}).status_code,400)
                    self.assertEqual(client.post(f"/agent/sessions/{session_id}/changes/generate",json={"model_id":"mock-endpoint"}).status_code,400)

                    planned={"objective":"Document hello","requirements":["Document function"],"assumptions":[],"technology_stack":["Python"],"architecture_overview":"Update the existing module documentation.","modules":[],"tasks":[{"id":"docs","title":"Document hello","description":"Add a docstring to hello.","depends_on":[],"acceptance_criteria":["Function has a docstring"]}],"risks":[],"estimated_complexity":"low"}
                    with patch.object(orchestrator,"_project_evidence",return_value={"file_count":1,"summary":"Python project"}), patch.object(orchestrator,"_complete",new=AsyncMock(return_value=json.dumps(planned))):
                        retried=client.post(f"/agent/sessions/{session_id}/plan/retry")
                    self.assertEqual(retried.status_code,200,retried.text)
                    self.assertEqual(retried.json()["session"]["status"],"waiting_for_approval")
                    event_types=[item["event_type"] for item in client.get(f"/agent/sessions/{session_id}/events").json()]
                    self.assertIn("planning_retry_requested",event_types)
                    self.assertIn("plan_ready",event_types)
                    with orchestrator.connect() as db: db.execute("UPDATE agent_sessions SET status='failed',error='Later task failure' WHERE id=?",(session_id,))
                    self.assertEqual(client.get(f"/agent/sessions/{session_id}/plan").status_code,200)
                    self.assertEqual(client.post(f"/agent/sessions/{session_id}/plan/retry").status_code,409)
                    self.assertEqual(client.post(f"/agent/sessions/{session_id}/approve-plan",json={"approved":True}).status_code,400)
            finally:
                main.DB_PATH,main.DB_DIR,main.DATA_ROOT,main.ACTIVE_CODING_WORKSPACE=globals_before

    def test_project_scan_plan_approval_session_controls_and_events(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.agent import orchestrator
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"project"; root.mkdir(); (root/"main.py").write_text("def health(): return {'status':'ok'}\n",encoding="utf-8")
            db_path=Path(tmp)/"app.db"; data_root=Path(tmp)/"data"
            globals_before=(main.DB_PATH,main.DB_DIR,main.DATA_ROOT,main.ACTIVE_CODING_WORKSPACE)
            main.DB_PATH=db_path; main.DB_DIR=db_path.parent; main.DATA_ROOT=data_root; main.ACTIVE_CODING_WORKSPACE=root
            try:
                with TestClient(main.app) as client:
                    client.app.state.active_workspace=root
                    self.assertEqual(client.get("/health").status_code,200)
                    self.assertEqual(client.get("/models").status_code,200)
                    conversation=client.post("/conversations",json={"title":"Regression smoke"})
                    self.assertEqual(conversation.status_code,200,conversation.text)
                    conversation_id=conversation.json()["id"]
                    self.assertEqual(client.post(f"/conversations/{conversation_id}/messages",json={"role":"user","content":"Check project health."}).status_code,200)
                    self.assertEqual(len(client.get(f"/conversations/{conversation_id}/messages").json()),1)
                    selected=client.post("/coding/workspace/select",json={"path":str(root)})
                    self.assertEqual(selected.status_code,200,selected.text)
                    self.assertEqual(client.get("/coding/workspace/read",params={"relative_path":"main.py"}).json()["name"],"main.py")
                    scanned=client.post("/project-intelligence/scan")
                    self.assertEqual(scanned.status_code,200,scanned.text)
                    job_id=scanned.json()["job_id"]
                    for _ in range(100):
                        scan_state=client.get(f"/project-intelligence/scan/{job_id}").json()
                        if scan_state["status"] in {"indexed","failed","cancelled"}: break
                        time.sleep(.02)
                    self.assertEqual(scan_state["status"],"indexed",scan_state)
                    self.assertEqual(scan_state["result"]["files_indexed"],1)
                    self.assertEqual(client.get("/project-intelligence/search",params={"q":"health"}).status_code,200)
                    self.assertEqual(client.get("/project-intelligence/dependencies").status_code,200)
                    with orchestrator.connect() as db:
                        timestamp=orchestrator.now()
                        db.execute("INSERT INTO model_endpoints(id,model_id,name,runtime,endpoint_url,external_model_id,ownership,is_enabled,last_status,created_at,updated_at) VALUES('mock-endpoint',NULL,'Mock local','mock','http://127.0.0.1:1','mock','external',1,'online',?,?)",(timestamp,timestamp))
                    created=client.post("/agent/sessions",json={"mode":"planning","model_id":"mock-endpoint"})
                    self.assertEqual(created.status_code,200,created.text); session_id=created.json()["id"]
                    preview=client.post("/coding/terminal/preview",json={"command":"Write-Output 'agent-linked-terminal-ok'","reason":"Approved plan verification","session_id":session_id})
                    self.assertEqual(preview.status_code,200,preview.text)
                    self.assertEqual(preview.json()["session_id"],session_id)
                    self.assertTrue(preview.json()["approval_required"])
                    rejected=client.post(f"/coding/terminal/reject/{preview.json()['id']}")
                    self.assertEqual(rejected.json(),{"id":preview.json()["id"],"status":"rejected","executed":False})
                    self.assertFalse(any(row.get("command")=="Write-Output 'agent-linked-terminal-ok'" for row in main.coding_terminal_history()))
                    preview=client.post("/coding/terminal/preview",json={"command":"Write-Output 'agent-linked-terminal-ok'","reason":"Approved plan verification","session_id":session_id})
                    self.assertEqual(preview.status_code,200,preview.text)
                    started=client.post(f"/coding/terminal/execute/{preview.json()['id']}")
                    self.assertEqual(started.status_code,200,started.text)
                    for _ in range(100):
                        terminal_state=client.get(f"/coding/terminal/executions/{started.json()['execution_id']}").json()
                        if terminal_state["status"] not in {"running","cancel_requested"}: break
                        time.sleep(.02)
                    self.assertEqual(terminal_state["status"],"completed",terminal_state)
                    self.assertIn("agent-linked-terminal-ok",terminal_state["stdout"])
                    self.assertTrue(any(event["event_type"]=="terminal_finished" for event in client.get(f"/agent/sessions/{session_id}/events").json()))
                    plan={"objective":"Add a health route","requirements":["Return status JSON"],"assumptions":[],"technology_stack":["Python"],"architecture_overview":"Use existing app module","modules":[],"project_structure":[{"path":"api/health.py","purpose":"Health route"}],"commands":[{"command":"python -m unittest","reason":"Run the project tests"}],"tasks":[{"id":"route","title":"Add route","description":"Add a read-only route","depends_on":[],"acceptance_criteria":["Returns status"],"subtasks":[{"id":"test","title":"Add route test","description":"Verify status response","depends_on":["route"],"acceptance_criteria":["Test passes"]}]}],"risks":[],"estimated_complexity":"low"}
                    vague=client.post(f"/agent/sessions/{session_id}/plan",json={"requirement":"Build an app","model_id":"mock-endpoint"})
                    self.assertEqual(vague.status_code,200, vague.text)
                    self.assertEqual(vague.json()["session"]["status"],"awaiting_input")
                    self.assertEqual(len(vague.json()["clarification_questions"]),2)
                    with patch.object(orchestrator,"_complete",new=AsyncMock(return_value=json.dumps(plan))):
                        planned=client.post(f"/agent/sessions/{session_id}/plan",json={"requirement":"Add a health route","model_id":"mock-endpoint"})
                    self.assertEqual(planned.status_code,200,planned.text)
                    self.assertEqual(planned.json()["session"]["status"],"waiting_for_approval")
                    self.assertEqual(planned.json()["plan"]["project_structure"][0]["path"],"api/health.py")
                    self.assertEqual(planned.json()["plan"]["commands"][0]["command"],"python -m unittest")
                    self.assertEqual(planned.json()["session"]["title"],"Build an app")
                    failed_session=client.post("/agent/sessions",json={"mode":"planning","model_id":"mock-endpoint"}).json()
                    with patch.object(orchestrator,"_project_evidence",return_value={"file_count":1,"summary":"Python project"}), patch.object(orchestrator,"_complete",new=AsyncMock(side_effect=orchestrator.InferenceFailure("timeout","The local planning request timed out."))):
                        failed_plan=client.post(f"/agent/sessions/{failed_session['id']}/plan",json={"requirement":"Add a documented health route","model_id":"mock-endpoint"})
                    self.assertEqual(failed_plan.status_code,502,failed_plan.text)
                    self.assertEqual(client.get(f"/agent/sessions/{failed_session['id']}").json()["status"],"failed")
                    self.assertTrue(any(item["event_type"]=="plan_failed" for item in client.get(f"/agent/sessions/{failed_session['id']}/events").json()))
                    renamed=client.patch(f"/agent/sessions/{session_id}/title",json={"title":"Health route implementation"})
                    self.assertEqual(renamed.status_code,200,renamed.text)
                    self.assertTrue(renamed.json()["title_manually_edited"])
                    self.assertEqual(client.get("/agent/sessions").json()[0]["title"],"Health route implementation")
                    tasks=client.get(f"/agent/sessions/{session_id}/tasks").json()
                    self.assertEqual(len(tasks),2)
                    unapproved_proposal=client.post(f"/agent/sessions/{session_id}/changes/generate",json={"model_id":"mock-endpoint"})
                    self.assertEqual(unapproved_proposal.status_code,409)
                    approved=client.post(f"/agent/sessions/{session_id}/approve-plan",json={"approved":True})
                    self.assertEqual(approved.json()["plan_status"],"approved")
                    generated={"changes":[
                        {"operation":"modify","path":"main.py","content":"def health(): return {'status':'ready'}\n","summary":"Return ready status","task_ids":["route"]},
                        {"operation":"create","path":"health_utils.py","content":"def status_payload():\n    return {'status': 'ready'}\n","summary":"Add shared status payload","task_ids":["test"]}
                    ]}
                    with patch.object(orchestrator,"_complete",new=AsyncMock(return_value=json.dumps(generated))):
                        proposal=client.post(f"/agent/sessions/{session_id}/changes/generate",json={"model_id":"mock-endpoint"})
                    self.assertEqual(proposal.status_code,200,proposal.text)
                    changeset=proposal.json(); self.assertEqual(len(changeset["changes"]),2)
                    main_change=next(change for change in changeset["changes"] if change["path"]=="main.py")
                    utility_change=next(change for change in changeset["changes"] if change["path"]=="health_utils.py")
                    self.assertIn("-def health",main_change["diff"])
                    self.assertEqual(client.post(f"/agent/sessions/{session_id}/changes/{changeset['id']}/{main_change['id']}/review",json={"decision":"approved"}).status_code,200)
                    self.assertEqual(client.post(f"/agent/sessions/{session_id}/changes/{changeset['id']}/{utility_change['id']}/review",json={"decision":"rejected"}).status_code,200)
                    applied=client.post(f"/agent/sessions/{session_id}/changes/{changeset['id']}/apply",json={"unsaved_paths":[]})
                    self.assertEqual(applied.status_code,200,applied.text)
                    self.assertIn("ready",(root/"main.py").read_text(encoding="utf-8"))
                    self.assertFalse((root/"health_utils.py").exists())
                    approved_version=client.get("/coding/workspace/versions",params={"relative_path":"main.py"}).json()
                    self.assertTrue(any(row["reason"]==f"agent:{changeset['id']}" for row in approved_version))
                    rolled_back=client.post(f"/agent/sessions/{session_id}/changes/{changeset['id']}/rollback",json={"unsaved_paths":[]})
                    self.assertEqual(rolled_back.status_code,200,rolled_back.text)
                    self.assertEqual((root/"main.py").read_text(encoding="utf-8"),"def health(): return {'status':'ok'}\n")
                    self.assertGreaterEqual(len(client.get(f"/agent/sessions/{session_id}/events").json()),5)
                    self.assertEqual(client.patch(f"/agent/sessions/{session_id}/tasks/{tasks[1]['id']}",json={"status":"in_progress"}).status_code,409)
                    self.assertEqual(client.patch(f"/agent/sessions/{session_id}/tasks/{tasks[0]['id']}",json={"status":"in_progress"}).status_code,200)
                    self.assertEqual(client.patch(f"/agent/sessions/{session_id}/tasks/{tasks[0]['id']}",json={"status":"completed"}).json()["status"],"completed")
                    self.assertEqual(client.patch(f"/agent/sessions/{session_id}/tasks/{tasks[1]['id']}",json={"status":"in_progress"}).status_code,200)
                    self.assertEqual(client.patch(f"/agent/sessions/{session_id}/tasks/{tasks[1]['id']}",json={"status":"completed"}).json()["status"],"completed")
                    self.assertEqual(client.post(f"/agent/sessions/{session_id}/pause").json()["status"],"paused")
                    self.assertEqual(client.post(f"/agent/sessions/{session_id}/resume").json()["status"],"plan_approved")
                    self.assertEqual(client.post(f"/agent/sessions/{session_id}/cancel").json()["status"],"cancelled")
                    self.assertGreaterEqual(len(client.get(f"/agent/sessions/{session_id}/events").json()),5)
                    self.assertEqual(client.get("/agent/sessions/invalid-id").status_code,404)
                    self.assertEqual(client.post("/agent/sessions",json={"mode":"execute"}).status_code,422)
                    ask_session=client.post("/agent/sessions",json={"mode":"ask","model_id":"mock-endpoint"}).json()
                    with patch.object(orchestrator,"_complete",new=AsyncMock(return_value="The health function is in main.py:1.")):
                        answer=client.post(f"/agent/sessions/{ask_session['id']}/ask",json={"requirement":"Where is the health function?","model_id":"mock-endpoint"})
                    self.assertEqual(answer.status_code,200,answer.text)
                    self.assertEqual(len(client.get(f"/agent/sessions/{ask_session['id']}/history").json()),2)
            finally:
                main.DB_PATH,main.DB_DIR,main.DATA_ROOT,main.ACTIVE_CODING_WORKSPACE=globals_before


if __name__ == "__main__": unittest.main()
