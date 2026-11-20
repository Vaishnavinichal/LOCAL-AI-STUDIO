"""End-to-end smoke check for live Ollama discovery and semantic project retrieval."""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import main


def run():
    with tempfile.TemporaryDirectory() as tmp:
        base=Path(tmp); root=base/"project"; root.mkdir()
        (root/"math_utils.py").write_text("def clamp(value, lower, upper):\n    return max(lower, min(value, upper))\n",encoding="utf-8")
        old=(main.DB_PATH,main.DB_DIR,main.DATA_ROOT,main.ACTIVE_CODING_WORKSPACE)
        main.DB_PATH=base/"app.db"; main.DB_DIR=base; main.DATA_ROOT=base/"data"; main.ACTIVE_CODING_WORKSPACE=root
        try:
            with TestClient(main.app) as client:
                client.app.state.active_workspace=root
                discovery=client.post("/models/discover",timeout=20); discovery.raise_for_status()
                models=client.get("/agent/models/roles").json()["models"]
                candidate=next((m for m in models if "embed" in m["name"].casefold() and not m["name"].casefold().endswith(":cloud")),None)
                if not candidate: raise RuntimeError("No installed local embedding model was discovered")
                assigned=client.put("/agent/models/roles",json={"role":"embedding","model_id":candidate["id"]}); assigned.raise_for_status()
                job=client.post("/project-intelligence/scan",json={"exclude_paths":[]}); job.raise_for_status(); job_id=job.json()["job_id"]
                for _ in range(600):
                    status=client.get(f"/project-intelligence/scan/{job_id}"); status.raise_for_status(); state=status.json()
                    if state["status"] in {"indexed","failed","cancelled"}: break
                    time.sleep(.5)
                if state["status"]!="indexed": raise RuntimeError(f"Project scan failed: {state}")
                if state["result"]["embedding"]["status"]!="indexed": raise RuntimeError(f"Embedding index unavailable: {state['result']['embedding']}")
                results=client.get("/project-intelligence/search",params={"q":"numeric value bounded within a range","mode":"semantic"}); results.raise_for_status(); matches=results.json()["results"]
                if not matches or matches[0].get("rank_mode")!="semantic": raise RuntimeError(f"Semantic retrieval did not return a result: {results.json()}")
                planner=next((m for m in models if m["name"]=="qwen2.5:3b"),None)
                if not planner: raise RuntimeError("The locally discovered qwen2.5:3b planning model is unavailable")
                session=client.post("/agent/sessions",json={"mode":"planning","model_id":planner["id"]}); session.raise_for_status(); session_id=session.json()["id"]
                plan=client.post(f"/agent/sessions/{session_id}/plan",json={"requirement":"Add a JSON health endpoint to this project with a small test.","model_id":planner["id"]}); plan.raise_for_status()
                if plan.json()["session"]["status"]!="waiting_for_approval" or not plan.json()["tasks"]: raise RuntimeError(f"Planner failed to persist a reviewable plan: {plan.json()}")
                approved=client.post(f"/agent/sessions/{session_id}/approve-plan",json={"approved":True}); approved.raise_for_status()
                paused=client.post(f"/agent/sessions/{session_id}/pause"); paused.raise_for_status()
                resumed=client.post(f"/agent/sessions/{session_id}/resume"); resumed.raise_for_status()
                print({"embedding_model":candidate["name"],"files_indexed":state["result"]["files_indexed"],"embedding_status":state["result"]["embedding"]["status"],"semantic_result":matches[0]["path"],"semantic_score":matches[0]["score"],"planner_model":planner["name"],"plan_tasks":len(plan.json()["tasks"]),"approval_status":approved.json()["plan_status"],"resume_status":resumed.json()["status"]})
        finally:
            main.DB_PATH,main.DB_DIR,main.DATA_ROOT,main.ACTIVE_CODING_WORKSPACE=old

if __name__=="__main__": run()
