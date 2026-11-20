import { isTauri } from "@tauri-apps/api/core";

export type NativeDropEvent = { type: "enter" | "over" | "leave" | "drop"; paths: string[] };

export function isTauriRuntime(): boolean {
  return isTauri();
}

/** Use native capabilities in the desktop shell and browser-safe fallbacks on the web. */
export async function selectProjectDirectory(): Promise<string | null> {
  if (isTauriRuntime()) {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const selected = await open({ directory: true, multiple: false });
    return typeof selected === "string" ? selected : null;
  }
  const path = window.prompt("Enter the absolute folder path on the computer running the LOCAL AI STUDIO backend:");
  return path?.trim() || null;
}

/** Native file drops carry OS paths in Tauri; browsers use the DOM FileList drop handler. */
export async function subscribeToNativeFileDrop(handler: (event: NativeDropEvent) => void | Promise<void>): Promise<() => void> {
  if (!isTauriRuntime()) return () => undefined;
  const { getCurrentWebview } = await import("@tauri-apps/api/webview");
  return getCurrentWebview().onDragDropEvent(event => {
    const type = event.payload.type;
    if (type === "enter" || type === "over" || type === "leave" || type === "drop") {
      void handler({ type, paths: type === "drop" ? event.payload.paths || [] : [] });
    }
  });
}
