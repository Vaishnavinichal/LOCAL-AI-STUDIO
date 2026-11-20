# Autonomous AI Engineer implementation status

Assessment date: 2026-11-18

## Repository and preservation

- The checked out branch is `Version2` and tracks `origin/Version2`.
- The working tree already contained edits in the backend entry point, coding workspace, API client, agent modules, tests, and documentation. These were preserved and extended in place.
- Monaco, existing workspace selection, file history, local model management, terminal, debugger, project intelligence, and agent planning remain in use.
- No database schema change or migration was needed.

## Milestone 1: grounded project answers

Project context rereads retrieved source ranges from the selected root before constructing model context. Excerpt text and citation ranges are clipped together. Ask mode validates citations against retrieved ranges, retries invalid citations once, and falls back to an explicit inability-to-verify response. The Agent workspace displays retrieved evidence with its path and line range.

## Milestone 2: coding workspace shell

The Monaco workspace gained a compact activity rail, Explorer filtering, minimap and code folding, save shortcuts, a live status bar, resizable/collapsible Explorer and AI panels, and docked Chat, Code, Agent and Plan tabs. Agent activity is available in the bottom panel.

## Phase 4: multi-file coding engine

Approved plans can generate bounded file proposals tied to plan task IDs. The engine accepts create, modify, delete and rename operations; validates relative paths and symlink boundaries; rejects secret, dependency and cache targets; detects disk and unsaved-editor conflicts; and stores proposals and preimages outside the project tree. Each proposal includes a unified diff and per-file review state. The UI uses Monaco Diff Editor, supports per-file and batch decisions, confirms apply when deletion or rename is included, applies only approved files, refreshes open editor buffers and Explorer, and provides rollback.

Before apply, existing file preimages are added to the existing `coding_file_versions` history. Multi-file apply compensates partial failures, persists recovery state, and recovers interrupted applies when project files still match their preimage or postimage. Rollback checks for edits made after apply and saves current content to file history first. No schema migration was needed.

The Phase 4 real local-model smoke used a temporary Python project and temporary app database. Qwen2.5 3B generated the plan and Qwen2.5-Coder 7B generated changes for `sample.py` and `test_sample.py`; the smoke checked the diffs and acceptance content, applied both files, and rolled both back. It completed in 196.6 seconds on the local CPU-backed Ollama runtime. The temporary project and database were removed by the smoke harness.

## Phase 6: testing and debugging engine

Project discovery now identifies Python pytest/unittest/syntax checks and Node/npm test, build, and TypeScript checks from test files and manifests. It checks tool availability and never installs dependencies. Windows Node commands use `npm.cmd`. Proposed commands show their project working directory and effects in the existing terminal approval view and execute only through the tracked Phase 5 process engine. Runs capture stdout/stderr, exit code, duration, cancellation and timeout state; local JSONL history is additive, and interrupted runs are recovered as interrupted rather than passed.

Results are parsed for pytest, unittest, Jest/Vitest, and Node TAP summaries. Failures are categorized, stack locations are constrained to the selected project, and Project Intelligence supplies relevant source excerpts. Missing dependencies and collection/environment errors are reported as execution errors when tests did not run, rather than failed tests. Secret-like values are redacted before history storage. A diagnosis endpoint passes only retrieved failure and source evidence to the selected local coding model. Low-confidence or unsupported diagnoses stop without changes. Supported fixes become ordinary file changesets with diffs; users review each file and apply only approved files through the existing snapshot/rollback engine. A bounded repair history tracks proposals and retests, stops at its configured limit, and stops on the same failure after a retest.

The Coding Workspace Tests panel shows discovered suites/files, explicit command approval, run history, counts, failures, stack/source locations, diagnosis, proposed fix status, and retest controls. Agent progress shows the active debugging stage. Test execution is bound to the existing tracked process ID so Agent pause/cancel controls can stop it.

No database migration was required. Test run history is stored in an additive local JSONL file under application data, and session/task linkage uses existing agent events and records.

Real integration tests used temporary projects and app data: a passing Node built-in test, a real failing Python unittest, Python syntax compilation failure, missing Python dependency, command rejection and cancellation, interrupted-run recovery, an explicitly approved correction changeset, and a passing retest. The deterministic approval test mocks the coding model. A separate live Ollama smoke used `qwen2.5-coder:7b` to diagnose a real Python assertion failure, produce a `calc.py` diff, pass through explicit changeset approval, apply it, and rerun the test successfully in 103.4 seconds. Its plan was a deterministic fixture, so this verifies live local correction generation and apply/retest, not live plan generation. The successful run used temporary project data and a temporary app database. The Windows test runner uses unittest because pytest is not installed in the project virtual environment.

## Remaining phases and limitations

Phase 7 autonomous task orchestration and Phase 8 broader resource/reliability hardening remain incomplete. A local model can still return a poor diagnosis or code proposal; generated changes remain review-only until approved. Terminal execution requires user review and is not an OS sandbox: Windows commands inherit the current user's access. Process pause is implemented as a stop; resume requires the user to rerun the command.

Frontend production compilation passed, but this checkout has no frontend interaction-test framework installed, so automated React interaction tests were not run. Real UI interaction still needs manual verification. A failed command with an unknown output format is reported as `unparsed`, never as passed. Framework discovery is intentionally limited to supported Python and Node/npm conventions; unusual runners need an explicit command through the existing approval flow.

The existing version table stores preimages per file while the changeset manifest groups them into one recoverable operation. Rollback is compensating and conflict-checked; it cannot guarantee atomicity against external programs changing project files during filesystem operations.

## Phase 5: terminal execution

The Coding Workspace reviews an immutable PowerShell command proposal before the user executes it. The preview shows the exact command, selected project working directory, reason, timeout, and detected file/network effects. Proposals expire after five minutes, can run once, and are invalidated if the selected workspace changes. Known high-impact destructive commands are blocked. Approved commands run asynchronously with captured stdout/stderr, exit status, timestamps, timeout, cancellation, and persistent local command history. Windows uses a kill-on-close Job Object when assignment is available; only processes created and tracked by this engine can be stopped. Command results are added to linked agent session events, which the Agent panel refreshes while open.

The bottom panel includes Terminal, Problems, Output, Debug Console, Tests, Agent Logs, and Command History. Terminal output can be copied or cleared, and tracked processes can be stopped. An approved plan can offer a verification command into the Terminal review flow. Session titles default to the first user request, can be renamed, and retain manual titles; title changes use existing agent events, so no schema migration was required. Explorer, AI panel, and bottom panel dimensions/collapse state persist locally. Hiding the AI panel keeps the Agent component and session state mounted.

The selected project is enforced as the process working directory and API file paths remain root-validated. PowerShell is not an OS sandbox: it inherits the current Windows user's permissions and can access other paths if the approved command requests them. The approval view says this explicitly. The command block list is a guardrail rather than a complete PowerShell security policy. The compatibility `/coding/terminal/run` endpoint retains its explicit `approved` flag and synchronous response; the desktop uses the preview/execute endpoints.

## Local verification

- Full backend suite: 54 passed, 1 symlink test skipped because symlink creation is unavailable in this Windows environment. This includes real Windows PowerShell terminal tests and temporary Python/Node testing workflows.
- Frontend type check and production build: passed with `npm.cmd run build` (Vite reports a 1.2 MB JavaScript bundle warning).
- Real local-model Phase 4 smoke: passed using a temporary project and database. Rerun with `python app/backend/scripts/_phase4_real_model_smoke.py`; it requires local Ollama models `qwen2.5:3b` and `qwen2.5-coder:7b`.
- Tauri release build passed with `npm.cmd run tauri -- build --no-bundle`, producing `app/desktop/src-tauri/target/release/desktop.exe`. The MSI bundle was not produced: Wix had to be downloaded from GitHub, and that download timed out even after network access was approved.
- Phase 6 live correction smoke passed with local `qwen2.5-coder:7b`; plan generation was a deterministic approved fixture. Rerun with `python app/backend/scripts/_phase6_real_model_smoke.py`.
- No production database was changed.

## Agent workspace and approved-action integration update (2026-11-18)

- The embedded Agent is a docked right-side workspace region. Its header now remains visible and includes session and running/idle indicators, a width focus toggle, and a visible close button. Closing collapses only the panel; the mounted Agent state, session, history, proposals, and approvals remain available. The activity rail reopens Agent.
- Explorer, Agent, and bottom-panel dimensions/collapse states persist in local storage. Editor focus and Agent focus can be toggled; Reset layout restores the default panel arrangement.
- Approved plan file generation now cross-checks plan task records against persisted session tasks, verifies dependency edges, orders tasks topologically, prevents duplicate generation in one process, and returns the existing Phase 4 review proposal. It does not complete tasks or apply files.
- The Agent verification-command action uses the existing terminal preview endpoint. The bottom Terminal shows an immutable command, working directory, reason, timeout and risk indicators. Reject records a rejection event; the separate Approve & Execute button is the only UI path that executes that preview.
- No database schema migration was needed. Session validation is applied when an Agent-linked command preview is created.
- Verified: frontend TypeScript and production build; backend unittest suite (55 tests, one symlink-platform skip); real local qwen2.5-coder:7b integration against a temporary Python calculator project generated and reviewed two file diffs, explicitly approved/applied them, passed `python -m unittest -v`, rejected one terminal preview without execution, separately approved and executed the previewed retest through the terminal engine (exit code 0), then rolled the file changes back.
- Rendered UI verification in Edge confirmed the Agent X hides the panel, preserves its selected session, and the activity-rail Agent control reopens it. Screenshot: `docs/screenshots/agent-workspace.png`.
- Limitations: the backend test suite emits a Starlette/httpx deprecation warning; no dedicated frontend interaction test runner is configured in the desktop package. Responsive behavior was exercised at the desktop Tauri-sized viewport; the UI test did not automate narrow-window resize handles.
