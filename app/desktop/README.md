# LOCAL AI STUDIO desktop and browser frontend

The same React + Vite frontend runs inside Tauri or in a web browser. Both modes use the existing local FastAPI backend; model inference, document processing, and coding workspace file operations stay on the backend machine.

## Run in a browser

Start the backend from `app/backend` using the project's Python environment:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Start Vite from `app/desktop`:

```powershell
npm run dev
```

Open `http://localhost:1420`. In browser mode, the Change Folder action asks the local FastAPI backend to open the operating system's folder picker on the backend machine. The picker endpoint requires a restarted backend after upgrading. Document upload and DOM file drag-and-drop use browser file APIs. Tauri retains its native folder picker and native file-drop handling.

Set `VITE_API_BASE_URL` at frontend build/dev time when the API is not at `http://127.0.0.1:8000`. For a non-default browser origin, add that exact origin to the backend's comma-separated `LOCAL_AI_CORS_ORIGINS` environment variable. CORS remains limited to explicit local origins by default.

The Tauri desktop build continues to use the same `npm run build` output and native platform adapters.

## Recommended IDE Setup

- [VS Code](https://code.visualstudio.com/) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer)
