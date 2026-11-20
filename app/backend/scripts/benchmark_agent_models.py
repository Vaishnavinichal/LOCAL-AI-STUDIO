"""Run the bounded Phase 1 local Ollama benchmark suite sequentially."""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.agent import orchestrator


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",default="../../docs/model_benchmark_results.json")
    parser.add_argument("--models",nargs="*",help="Optional exact Ollama model names; default benchmarks all locally installed non-cloud registry entries")
    args=parser.parse_args()
    orchestrator.initialize()
    base=os.environ.get("OLLAMA_URL","http://127.0.0.1:11434").rstrip("/")
    async with httpx.AsyncClient(timeout=6) as client:
        response=await client.get(base+"/api/tags"); response.raise_for_status(); payload=response.json()
    live={item.get("name","") for item in payload.get("models",[]) if not item.get("name","").casefold().endswith(":cloud")}
    with orchestrator.connect() as db:
        rows=db.execute("SELECT id,name FROM model_registry WHERE status='installed' AND runtime='ollama' ORDER BY name COLLATE NOCASE").fetchall()
    installed={row["name"]:row["id"] for row in rows if row["name"] in live and not row["name"].casefold().endswith(":cloud")}
    selected=args.models or ["qwen2.5-coder:7b","qwen2.5:7b","qwen2.5:3b","embeddinggemma:latest","nomic-embed-text:latest"]
    results=[]
    for name in selected:
        model_id=installed.get(name)
        if not model_id:
            results.append({"model_name":name,"status":"unavailable","reason":"Not both present in the live Ollama tags and installed model registry."})
            continue
        result=await orchestrator.benchmark(orchestrator.BenchmarkRequest(model_id=model_id))
        results.append(result)
        print(json.dumps({"model_name":name,"statuses":{key:value.get("status") for key,value in result["results"].items()}},ensure_ascii=False),flush=True)
    report={"measured_at":datetime.now(timezone.utc).isoformat(),"runtime":"Ollama (localhost)","discovered_live_models":sorted(live),"benchmarks":results,"hardware_context":{"ram_gb":16,"cpu":"Intel Core i5-1340P","gpu":"integrated graphics"},"limitations":["Models were benchmarked sequentially; keep_alive=0 was requested to limit concurrent residency.","Ollama token counts and evaluation duration are used when supplied; fallback token rates are estimates.","Memory use and long production-context behavior were not measured.","The long-context case is a controlled short retrieval prompt, not a maximum-context test."]}
    output=Path(args.output).resolve(); output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    print(f"Wrote {len(results)} benchmark record(s) to {output}")

if __name__=="__main__": asyncio.run(main())
