# Clea Architecture (Milestone 1 + Phase 2 Desktop UI)

Clea uses a **hub-and-spoke** architecture designed for a local, voice-first personal desktop assistant. Milestone 1 established the text-only Python core; Phase 2 adds handshake security, interactive tool approvals, read-only memory/conversation queries, and the web + Electron desktop UI.

## System Topology

```
+-----------------------------------+
| Surface: CLI (`clea chat`)        |
| (protocol + auth only)            |----+
+-----------------------------------+    |
                                         |  ws://127.0.0.1:8765
+-----------------------------------+    |  Sec-WebSocket-Protocol:
| Surface: Desktop UI (`ui/`)       |    |    clea.v1, clea.token.<token>
| - Browser dev: http://127.0.0.1:5173   |
| - Electron app: clea-app://app    |----+----> +---------------------------+
|   (Main window, Tray, Quick Ask)  |           | Core Daemon (`clea daemon`) |
+-----------------------------------+           | - Auth + Origin Allowlist |
                                                | - SessionManager          |
                                                | - AgentLoop               |
                                                | - ToolRegistry + Audit    |
                                                | - MemoryStore (Markdown)  |
                                                | - LLMProvider (Gemini)    |
                                                +---------------------------+
                                                              |
                                                              v
                                                OS User Data Dir (platformdirs)
                                                - session_token (per-run)
                                                - desktop_config.json (optional)
                                                - MEMORY.md
                                                - memory/YYYY-MM-DD.md
                                                - logs/audit.jsonl
```

## Components

### 1. Typed WebSocket Protocol (`src/clea/core/protocol.py` & `scripts/gen_ts_types.py`)
- Defines strict Pydantic v2 discriminated-union frames (`extra="forbid"`):
  - **Requests** (`RequestFrame`, discriminated by `method`): `health.ping`, `session.new`, `chat.send`, `approval.respond`, `memory.get`, `memory.get_daily`, `conversations.list`, `conversations.get`, `settings.get`.
  - **Responses** (`ResponseFrame`): paired by request `id`, carrying typed result models (`METHOD_RESULTS`).
  - **Events** (`EventFrame`, discriminated by `event`): `assistant_token`, `tool_call_started`, `tool_call_finished`, `approval_requested`, `approval_resolved`, `task_status`, `voice_state` (`idle | listening | thinking | speaking`), `error`.
- `scripts/gen_ts_types.py` compiles the Pydantic JSON Schema into `ui/src/protocol/generated.ts` via `json-schema-to-typescript` (`--check` fails on drift).

### 2. Core Daemon & Handshake Security (`src/clea/core/daemon.py`, `src/clea/core/auth.py`)
- **`CleaDaemon`**: Binds strictly to `127.0.0.1` (`localhost` / `::1`) using `websockets`.
- **Single-Instance & Background Mode**:
  - `InstanceLock` (`<user_data_dir>/daemon.lock`) guarantees only one daemon runs per data directory and prevents a second start from overwriting the active `session_token`.
  - `clea daemon --background` writes structured logs to `<user_data_dir>/logs/daemon.log` (`RotatingFileHandler`, 5 MB x 3 backups, UTF-8) instead of stdout/stderr.
  - On Windows, `clea autostart install | uninstall | status` configures a current-user Task Scheduler task (`CleaDaemon`) in the interactive logon session (`pythonw.exe`) rather than a Session 0 Windows Service (see [ADR 0002](decisions/0002-windows-autostart-session.md)).
- **Auth & Origin Check (`process_request`)**:
  - Generates a fresh 256-bit token (`secrets.token_urlsafe`) on every daemon start, writes it to `<user_data_dir>/session_token` (POSIX `0600` or Windows current-user SID NTFS ACL via `icacls`), and removes it on graceful stop.
  - Validates local `Host` header (DNS-rebinding defense), `Origin` against `CLEA_ALLOWED_ORIGINS` (defaults: `http://localhost:5173`, `http://127.0.0.1:5173`, `clea-app://app`; wildcards and `null` are rejected), and `Sec-WebSocket-Protocol` token via `hmac.compare_digest`.
- **Concurrent Turns & Approvals**:
  - Each `chat.send` runs as an asyncio task so the connection receive loop stays live for `approval.respond`.
  - Pending approvals resolve on user response, timeout (defaults to `deny`), or disconnect (cancelled).

### 3. Tool Registry & Permissions (`src/clea/tools/registry.py`)
- **`RiskLevel`**: `read_only` (auto-allowed), `reversible` (asks confirmation), `destructive` (asks confirmation + explicit second click in UI / explicit `yes` in CLI).
- **`AuditLogger`**: Appends a JSONL record to `<user_data_dir>/logs/audit.jsonl` for every tool invocation (`ok`, `denied`, `error`).

### 4. Memory Store (`src/clea/memory/store.py` & `src/clea/paths.py`)
- Stores `MEMORY.md` and `memory/YYYY-MM-DD.md` outside the repo under `platformdirs.user_data_dir("clea", appauthor=False)` (or `CLEA_DATA_DIR`).
- Provides read-only `list_days()`, `read_daily_on()` (with strict `YYYY-MM-DD` validation), `list_conversations()`, and `get_conversation()` exposed over the daemon protocol.

### 5. Desktop UI (`ui/`)
- **Pure Client**: React 19, TypeScript, Vite 8, Tailwind CSS v4, shadcn-style primitives + `cmdk`, Zustand (`ui/src/protocol/store.ts`), `CleaClient` with exponential backoff + jitter (`ui/src/protocol/client.ts`).
- **Screens**:
  - **Chat**: streaming Markdown (`react-markdown` + `remark-gfm`), collapsible tool cards, inline approval cards (two-click confirmation for `destructive`), `Enter` send / `Shift+Enter` newline.
  - **Sidebar & Command Palette (`Ctrl/Cmd+K`)**: navigation to Chat, Activity, Memory, Settings; read-only historical conversations grouped by date; system/dark/light theme toggle.
  - **Activity**: live/completed `task_status` list.
  - **Memory**: read-only `MEMORY.md` and daily logs fetched from the daemon.
  - **Settings**: non-secret daemon status, provider/model, permission tiers, registered tools.
  - **Call Overlay**: floating glass orb driven by `voice_state` events (`prefers-reduced-motion` respected). Dev-only simulation panel (`DevVoiceMockPanel.tsx`) is dynamically imported only under `import.meta.env.DEV && VITE_CLEA_MOCKS === "true"` and excluded from production builds.
  - **Daemon Offline Screen**: clear startup instructions (`uv run clea daemon`) and auto-reconnect.
- **Browser Dev Token (`ui/dev-token-plugin.ts`)**: dev-only Vite plugin serving `/__clea/token` to localhost Host/Origin with `Cache-Control: no-store` and no CORS headers; excluded from production builds.

### 6. Electron Shell (`ui/electron/`)
- Built with `electron-vite` (uses `app.requestSingleInstanceLock()`). Does not bundle the daemon, though the offline screen offers an explicit **Start daemon** action in Electron that spawns the repo venv's `pythonw.exe -m clea daemon --background` (no shell).
- **Hardening**: `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`, `webSecurity: true`, strict CSP, navigation and `window.open` blocked.
- **Custom Protocol (`clea-app://app`)**: registered as `standard` + `secure` with Fetch/WebSocket support, strictly scoped to `out/renderer/` (rejects path traversal).
- **Preload (`ui/electron/preload.ts`)**: exposes `window.cleaDesktop` with `isElectron`, `getConnection()` (re-reads `session_token` fresh from disk on every connect/reconnect, never cached), `onQuickAskFocus()`, `closeQuickAsk()`, `getAutoLaunch()`, `setAutoLaunch()`, and `startDaemon()`.
- **System Tray, Auto-Launch & Quick Ask**: Tray menu (`Open`, `Quick Ask`, `Quit`), close-to-tray behavior, optional **Launch Clea at login** toggle in Settings (default off; starts hidden in tray via `--hidden` / `wasOpenedAtLogin`), and a configurable global shortcut (default `CommandOrControl+Alt+Space`, overridable via `<user_data_dir>/desktop_config.json`) opening an always-on-top glass Quick-Ask window.
