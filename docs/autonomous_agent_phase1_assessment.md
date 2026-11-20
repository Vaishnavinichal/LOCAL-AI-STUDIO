# Autonomous AI Engineer — Phases 1–3 Assessment

Assessment date: 17 November 2026. This report describes the repository inspected on branch `Version2` and the local runtime observations recorded in [model_benchmark_results.json](model_benchmark_results.json).

## Existing application capabilities

- FastAPI currently lives in `app/backend/app/main.py`; it provides chat, project messages, Coding Workspace, file-version history, diff review, approval-gated writes/terminal actions, model management, and document tools.
- SQLite tables are initialized additively by `initialize_storage()`. The existing model registry stores runtime, provider, metadata, and generic capability assignments. Ollama discovery uses `/api/tags`; llama.cpp discovery uses `/v1/models`.
- The React Coding Workspace uses Monaco and the Tauri folder picker. Its backend already enforces a selected-root boundary, rejects traversal and symlinks, and bounds text reads. The active workspace is process memory state.
- The existing model manager is the source for agent model choices. The new role assignments use its `model_capability_assignments` table; no second model registry was added.
- Existing file write proposals, diff approval, terminal approval, and version history remain separate from this release. The new agent has no write-file or command tool.

## Reusable components and new boundaries

- Reused: local Ollama/llama.cpp discovery, installed model registry, capability assignment table, SQLite connection conventions, active Coding Workspace root, Tauri project selection, and existing UI theme.
- Added backend modules: `app/backend/app/agent/orchestrator.py` for sessions, planning, task state, safe tools, roles, and benchmark records; `app/backend/app/agent/project_intelligence.py` for scanning, parsing, incremental indexing, embeddings, summaries, and retrieval.
- The API is mounted from `main.py`; the two services own their business logic and their additive schemas. A later code-generation phase can add write tools behind the existing review workflow without turning the current read-only registry into arbitrary execution.

## Hardware and model feasibility

The machine is an Intel Core i5-1340P with 16 GB RAM and integrated graphics. Live Ollama discovery found local `qwen2.5:3b`, `qwen2.5:7b`, `qwen2.5-coder:7b`, `smollm2:135m`, `qwen3-vl:2b`, `embeddinggemma:latest`, and `nomic-embed-text:latest`. Three cloud registrations were also present and are excluded from agent use. The llama.cpp chat endpoint at `127.0.0.1:8081` was unavailable at discovery time.

Controlled completions ran sequentially, with `keep_alive=0` requested between runs. On these prompts, Qwen 3B measured roughly 8.36–11.83 generated tokens/s; the two Qwen 7B models measured roughly 4.45–5.09 tokens/s on successful cases. These are local measurements from a small prompt set, not performance guarantees. The 7B long-context case timed out at the 90-second request limit. No model memory use was measured. Role recommendations estimate working set from registered model file size plus conservative runtime overhead and live available RAM; they are guidance, not measurement.

EmbeddingGemma returned a valid local embedding in 2.56 seconds and Nomic Embed Text in 1.12 seconds in the controlled embedding prompt. An end-to-end smoke run then discovered EmbeddingGemma, assigned it to the existing Embedding capability, indexed a one-file Python project, and retrieved its source semantically with cosine score 0.46317. SQLite stores vectors; there is no added vector database.

A separate live workflow smoke run used `qwen2.5:3b` with a selected project: the model returned a structured two-task plan, the plan was stored and approved, and pause/resume restored the approved state. This verifies the real local runtime path for planning in addition to the mocked API regression suite.

## Capability matrix from measured prompts

| Model | Code generation | Explanation | Requirement/planning/JSON/tool/summarization | Controlled context prompt | Embeddings |
|---|---:|---:|---:|---:|---:|
| `qwen2.5:3b` | Passed | Passed | Passed | Passed | Not applicable |
| `qwen2.5-coder:7b` | Passed | Passed | Passed | Timed out | Not applicable |
| `qwen2.5:7b` | Passed | Passed | Passed | Timed out | Not applicable |
| `embeddinggemma:latest` | Not applicable | Not applicable | Not applicable | Not applicable | Valid 768-dimension vector |
| `nomic-embed-text:latest` | Not applicable | Not applicable | Not applicable | Not applicable | Valid 768-dimension vector |

“Passed” means the bounded benchmark assertion or JSON-shape check passed; it does not establish production code quality. The context prompt was a controlled ~3.5 KB input, not a maximum context-window test. Detailed response times, token rates, and prompt results are in the JSON artifact. Ollama generation token counts/durations were available for completions; embedding memory use and token rate were not measured.

## Scanner and parser support

Language and manifest detection includes Python, JavaScript, TypeScript, React/Node, Flask/FastAPI, HTML/CSS, Java, and C/C++. Framework and entry-point detection is based on manifests and known filenames. Python structure uses the standard-library AST and preserves parse errors per file. JavaScript/TypeScript structure and imports use bounded regular expressions because no parser dependency was already installed. The automated fixtures exercise Python AST, a React/Vite package manifest, secret exclusions, incremental hashes, dependency resolution, and syntax-error continuation; the live smoke test exercises semantic retrieval. Java, C/C++, Flask, and standalone JavaScript projects were not separately fixture-tested, so their detection remains heuristic rather than a verified parser claim.

## Known limits and integration risks

- The existing backend is a large `main.py` module. New business logic is split into service modules, but router mounting and legacy runtime/workspace state still meet in `main.py`.
- Source parsing is partial: Python and JS/TS imports are indexed; Java/C/C++ imports, method bodies, and framework-specific database models are not fully parsed. Unresolved import edges are preserved rather than guessed.
- Semantic retrieval requires a locally installed Ollama embedding model assigned to the Embedding role. Without one, or if it fails, keyword search remains available and index status records the embedding outcome.
- Scanning is restricted to the selected workspace, excludes common generated/cache/binary/secret files, supports additional relative exclusions, and is cancellable with live file-count progress. It does not follow symlinks. File and chunk size limits bound memory.
- Agent sessions are scoped to the current selected folder. Since the existing workspace selection itself is in memory, after a backend restart the user must reopen that folder before accessing its sessions.
- There are two database locations in this checkout: the application’s existing default is `D:\LocalAIStudio\data\database\local_ai.db`, while the supplied repository path is `D:\LOCAL-AI-STUDIO\data\database\local_ai.db`. The code preserves the existing default rather than silently switching databases. The configured default database and supplied repository database were both backed up before additive schema initialization/benchmark persistence.
- No cloud inference, automatic downloads, unrestricted filesystem access, or arbitrary command execution was added.

## Recommended follow-on architecture

Keep the orchestrator as the state machine and route model calls through a dedicated provider adapter shared with the existing local inference code. Add Phase 4 file proposals as a separate permission-classified tool implementation that returns diffs to the current approval/version-history path. Add command/test execution only through the existing explicit approval workflow. For stronger JS/TS analysis, evaluate a maintained parser only after measuring its install/runtime cost on this Windows setup.
