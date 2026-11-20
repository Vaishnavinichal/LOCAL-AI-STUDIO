# Agent session reads and planning recovery

## Root cause

`session_get()` loaded a session and then called `root_for(request)` to compare its stored project root with the active Coding Workspace. The read-only events, plan, history and session detail routes reused that operation guard. With no selected workspace, `root_for()` returned HTTP 400 before SQLite could return the existing session data.

Read-only session routes now retrieve by session ID and return 404 only when the session does not exist. The root comparison remains in `session_get()` and is still used by operations that read or modify project files, approve plans, generate proposals, or run terminal commands.

## Recovery behavior

- Session listing can show saved sessions when no workspace is active.
- Coding Workspace provides a saved Agent sessions recovery view when no project is selected.
- Failed planning sessions show their recorded error and a deliberate **Retry Planning** action. Startup does not restart inference.
- Retry requires a failed Planning session, a saved requirement, a valid stored project directory, no saved plan, and no active retry for that session. It records `planning_retry_requested`, then reuses the existing local planning path and approval workflow.
- A retry does not replace an existing plan. Failed sessions with saved plans remain readable.
- The Agent stops polling after the session leaves `planning`; a polling API error stops polling as well. Events and plan/history reads remain available.

## Persistence and tests

No schema migration was added. The supplied production database was inspected read-only. The exact interrupted session was exercised through HTTP using a temporary SQLite backup snapshot, so application startup and tests did not write to the production database.

Verified responses for `27916ce4-59da-44ec-915b-f4abf47894c3` with no active project:

- `GET /agent/sessions/27916ce4-59da-44ec-915b-f4abf47894c3/events` → HTTP 200, event types `session_created` and `planning_started`.
- `GET /agent/sessions/27916ce4-59da-44ec-915b-f4abf47894c3/plan` → HTTP 404, `{"detail":"No plan exists for this session"}`.

Backend regressions cover missing sessions, failed session metadata, no-plan and saved-plan reads without an active project, deliberate retry, duplicate-plan prevention, and project-bound terminal/file proposal operations.
