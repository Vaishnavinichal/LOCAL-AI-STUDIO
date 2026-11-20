export function terminalInputPayload(input: string): string {
  return input && !/[\r\n]$/.test(input) ? `${input}\n` : input;
}

export function terminalCanReceiveInput(status: string, interactive: boolean, stdinClosed: boolean): boolean {
  return interactive && status === "running" && !stdinClosed;
}

export type TerminalUiSnapshot = { executionId: string; output: string; status: string; interactive: boolean; stdinClosed: boolean };

const TERMINAL_STORAGE_PREFIX = "las.ide.terminal.";

export function loadTerminalUiSnapshot(storage: Pick<Storage, "getItem">): TerminalUiSnapshot {
  const executionId = storage.getItem(`${TERMINAL_STORAGE_PREFIX}executionId`) || "";
  return {
    executionId,
    output: storage.getItem(`${TERMINAL_STORAGE_PREFIX}output`) || "Terminal ready. Commands require confirmation.\n",
    status: storage.getItem(`${TERMINAL_STORAGE_PREFIX}status`) || (executionId ? "running" : ""),
    interactive: storage.getItem(`${TERMINAL_STORAGE_PREFIX}interactive`) === "true",
    stdinClosed: storage.getItem(`${TERMINAL_STORAGE_PREFIX}stdinClosed`) === "true",
  };
}

export function persistTerminalUiSnapshot(storage: Pick<Storage, "setItem" | "removeItem">, snapshot: TerminalUiSnapshot): void {
  storage.setItem(`${TERMINAL_STORAGE_PREFIX}output`, snapshot.output);
  storage.setItem(`${TERMINAL_STORAGE_PREFIX}status`, snapshot.status);
  storage.setItem(`${TERMINAL_STORAGE_PREFIX}interactive`, String(snapshot.interactive));
  storage.setItem(`${TERMINAL_STORAGE_PREFIX}stdinClosed`, String(snapshot.stdinClosed));
  if (snapshot.executionId) storage.setItem(`${TERMINAL_STORAGE_PREFIX}executionId`, snapshot.executionId);
  else storage.removeItem(`${TERMINAL_STORAGE_PREFIX}executionId`);
}
