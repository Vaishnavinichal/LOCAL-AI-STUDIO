import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { isTauriRuntime, selectProjectDirectory, subscribeToNativeFileDrop } from "../src/services/platform.ts";

const originalWindow = globalThis.window;
const originalIsTauri = (globalThis as typeof globalThis & { isTauri?: boolean }).isTauri;

afterEach(() => {
  if (originalWindow === undefined) delete (globalThis as typeof globalThis & { window?: Window }).window;
  else Object.defineProperty(globalThis, "window", { value: originalWindow, configurable: true, writable: true });
  if (originalIsTauri === undefined) delete (globalThis as typeof globalThis & { isTauri?: boolean }).isTauri;
  else (globalThis as typeof globalThis & { isTauri?: boolean }).isTauri = originalIsTauri;
});

test("browser mode prompts for a backend-local project directory and skips native drop APIs", async () => {
  (globalThis as typeof globalThis & { isTauri?: boolean }).isTauri = false;
  Object.defineProperty(globalThis, "window", { value: { prompt: () => "  D:\\Projects\\demo  " }, configurable: true, writable: true });
  assert.equal(isTauriRuntime(), false);
  assert.equal(await selectProjectDirectory(), "D:\\Projects\\demo");
  let received = false;
  const unlisten = await subscribeToNativeFileDrop(() => { received = true; });
  unlisten();
  assert.equal(received, false);
});

test("browser folder selection can be cancelled", async () => {
  (globalThis as typeof globalThis & { isTauri?: boolean }).isTauri = false;
  Object.defineProperty(globalThis, "window", { value: { prompt: () => null }, configurable: true, writable: true });
  assert.equal(await selectProjectDirectory(), null);
});

test("desktop mode is detected without invoking a native plugin from the browser branch", () => {
  (globalThis as typeof globalThis & { isTauri?: boolean }).isTauri = true;
  assert.equal(isTauriRuntime(), true);
});
