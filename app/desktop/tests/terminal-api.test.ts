import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { codingApi } from "../src/services/api.ts";
import { loadTerminalUiSnapshot, persistTerminalUiSnapshot, terminalCanReceiveInput, terminalInputPayload } from "../src/components/coding/terminalSession.ts";

const originalFetch = globalThis.fetch;
const requests: Array<{ url: string; method: string; body: any }> = [];

function stubFetch(response: (url: string, method: string, body: any) => unknown = () => ({})) {
  requests.length = 0;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ url, method, body });
    const payload = response(url, method, body);
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

afterEach(() => { globalThis.fetch = originalFetch; });

test("interactive command preview retains explicit approval and project scope", async () => {
  stubFetch((_url, _method, body) => ({ id: "proposal-1", ...body, approval_required: true }));
  await codingApi.previewTerminal("python -i", "src", "interactive test", 0, undefined, true);
  assert.equal(requests[0].url, "http://127.0.0.1:8000/coding/terminal/preview");
  assert.equal(requests[0].body.interactive, true);
  assert.equal(requests[0].body.timeout, 0);
  assert.equal(requests[0].body.relative_path, "src");
});

test("multiline and sequential stdin submissions keep their exact payloads", async () => {
  stubFetch((_url, _method, body) => ({ accepted: true, eof: body?.eof || false, execution: { stdin_closed: false } }));
  await codingApi.sendTerminalInput("run/1", "first\nsecond\n");
  await codingApi.sendTerminalInput("run/1", "third\n");
  assert.deepEqual(requests.map(item => item.body.input), ["first\nsecond\n", "third\n"]);
  assert.deepEqual(requests.map(item => item.url), [
    "http://127.0.0.1:8000/coding/terminal/executions/run%2F1/input",
    "http://127.0.0.1:8000/coding/terminal/executions/run%2F1/input",
  ]);
});

test("EOF is an explicit input operation and invalid session errors are surfaced", async () => {
  stubFetch((_url, _method, body) => ({ accepted: true, eof: body?.eof || false, execution: { stdin_closed: true } }));
  const result = await codingApi.sendTerminalInput("run-1", "", true);
  assert.deepEqual(requests[0].body, { input: "", eof: true });
  assert.equal(result.execution.stdin_closed, true);

  globalThis.fetch = (async () => new Response(JSON.stringify({ detail: "Terminal execution not found" }), { status: 404 })) as typeof fetch;
  await assert.rejects(codingApi.terminalExecution("missing"), /Terminal execution not found/);
});

test("status polling exposes growing output and cancellation uses the existing execution endpoint", async () => {
  const snapshots = [
    { execution_id: "run-1", status: "running", stdout: "READY", stderr: "", interactive: true, stdin_closed: false },
    { execution_id: "run-1", status: "running", stdout: "READY\nreceived:hello", stderr: "warning", interactive: true, stdin_closed: false },
  ];
  let pollIndex = 0;
  stubFetch((url, method) => {
    if (url.endsWith("/executions")) return snapshots;
    if (method === "DELETE") return { stopping: true };
    return snapshots[Math.min(pollIndex++, snapshots.length - 1)];
  });
  const first = await codingApi.terminalExecution("run-1");
  const second = await codingApi.terminalExecution("run-1");
  await codingApi.stopTerminal("run-1");
  assert.equal(first.stdout, "READY");
  assert.equal(second.stdout, "READY\nreceived:hello");
  assert.equal(second.stderr, "warning");
  assert.equal(requests[2].method, "DELETE");
  assert.equal(requests[2].url, "http://127.0.0.1:8000/coding/terminal/executions/run-1");
  assert.equal((await codingApi.terminalExecutions()).length, 2);
});

test("legacy non-interactive execution API remains unchanged", async () => {
  stubFetch(() => ({ exit_code: 0, stdout: "ok", stderr: "", timed_out: false }));
  await codingApi.runTerminal("Write-Output ok", ".", true);
  assert.equal(requests[0].url, "http://127.0.0.1:8000/coding/terminal/run");
  assert.equal(requests[0].body.approved, true);
  assert.equal(requests[0].body.command, "Write-Output ok");
});

test("terminal input helper submits one line, multiline text, and only allows input for live sessions", () => {
  assert.equal(terminalInputPayload("hello"), "hello\n");
  assert.equal(terminalInputPayload("one\ntwo"), "one\ntwo\n");
  assert.equal(terminalInputPayload("already\n"), "already\n");
  assert.equal(terminalCanReceiveInput("running", true, false), true);
  assert.equal(terminalCanReceiveInput("completed", true, false), false);
  assert.equal(terminalCanReceiveInput("running", false, false), false);
  assert.equal(terminalCanReceiveInput("running", true, true), false);
});

test("terminal output and active execution survive a workspace panel remount", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  };
  const original = { executionId: "run-42", output: "prompt-without-newline", status: "running", interactive: true, stdinClosed: false };
  persistTerminalUiSnapshot(storage, original);
  assert.deepEqual(loadTerminalUiSnapshot(storage), original);
  persistTerminalUiSnapshot(storage, { ...original, executionId: "", status: "completed", output: `${original.output}\nfinished` });
  assert.equal(loadTerminalUiSnapshot(storage).executionId, "");
  assert.equal(loadTerminalUiSnapshot(storage).output, "prompt-without-newline\nfinished");
});
