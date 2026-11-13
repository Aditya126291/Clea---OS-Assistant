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
                                                - memory/MEMORY.md (+ .bak, MEMORY_ARCHIVE.md)
                                                - memory/episodes/YYYY-MM-DD.md
                                                - logs/transcripts/<session_id>.jsonl
                                                - logs/transcripts_trash/
                                                - logs/audit.jsonl
                                                - logs/daemon.log
```

## Components

### 1. Typed WebSocket Protocol (`src/clea/core/protocol.py` & `scripts/gen_ts_types.py`)
- Defines strict Pydantic v2 discriminated-union frames (`extra="forbid"`):
  - **Requests** (`RequestFrame`, discriminated by `method`): `health.ping`, `session.new`, `chat.send`, `approval.respond`, `memory.get`, `memory.get_daily`, `memory.episodes`, `memory.facts`, `memory.archive`, `memory.status`, `memory.reconsolidate`, `transcripts.list`, `transcripts.get`, `conversations.list`, `conversations.get`, `conversation.list`, `conversation.get`, `conversation.resume`, `conversation.rename`, `conversation.pin`, `conversation.export`, `conversation.delete`, `conversation.delete_all`, `conversation.undo_delete`, `settings.get`, `dev.emit_sample_task`.
  - **Responses** (`ResponseFrame`): paired by request `id`, carrying typed result models (`METHOD_RESULTS`).
  - **Events** (`EventFrame`, discriminated by `event`): `assistant_token`, `tool_call_started`, `tool_call_finished`, `approval_requested`, `approval_resolved`, `task_status` (annotated with `kind`, `next_run_at`, error summary), `voice_state` (`idle | listening | thinking | speaking`), `conversation_updated`, `conversation_deleted`, `error`.
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
- **Latency & Streaming Resilience**:
  - Configurable pre-header first-event timeout (default 7.0 s, `CLEA_FIRST_EVENT_TIMEOUT`) with at most ONE retry before stream tokens arrive.
  - Replays `thought_signature` before `function_call` steps to satisfy API contracts.
  - Automatic rollback of failed turns so subsequent turns succeed without session corruption.

### 3. Tool Registry & Permissions (`src/clea/tools/registry.py`)
- **`RiskLevel`**: `read_only` (auto-allowed), `reversible` (asks confirmation), `destructive` (asks confirmation + explicit second click in UI / explicit `yes` in CLI).
- **`AuditLogger`**: Appends a JSONL record to `<user_data_dir>/logs/audit.jsonl` for every tool invocation (`ok`, `denied`, `error`).
- **Memory Tools**: `remember` (`reversible`) writes explicit stated facts to `MEMORY.md`; `forget` (`destructive`) matches entries and shows matching facts before user confirmation.

### 4. Memory Store & Consolidation (`src/clea/memory/store.py`, `src/clea/memory/consolidation.py`)
- **Three-Tier Storage** (all outside repo via `platformdirs`):
  1. `memory/MEMORY.md`: Durable facts and preferences only, organized into fixed sections (`Profile`, `Preferences`, `Environment`, `Projects`, `Decisions`, `Open Threads`) formatted as `- [id] (YYYY-MM-DD, source) text`. Includes atomic replacement and `.bak` backups.
  2. `memory/episodes/YYYY-MM-DD.md`: Per-session structured summaries (3-5 lines per session: session ID, start time, topics, outcomes, unresolved items).
  3. `logs/transcripts/<session_id>.jsonl`: Complete, raw session transcripts with JSON metadata header and structured message lines.
- **Migration**: Automatic, idempotent migration from legacy daily logs into structured JSONL transcripts with backups in `backup_legacy_logs`.
- **Consolidation**:
  - Triggers on session completion, 15-minute idle inactivity, or startup crash recovery for unconsolidated sessions.
  - Evaluates transcripts via structured JSON schema (`ConsolidationOutput`), deduplicating or resolving contradictions against existing facts. Supports provider-constrained `response_format` when available, and tolerates harmless aliases (`value` -> `text`).
  - Safety filters reject secrets/tokens, ignore instructions embedded in tool outputs or web data, and skip small talk.
  - Trivial sessions with fewer than N user turns (default 2, configured via `CLEA_CONSOLIDATION_MIN_TURNS`) and no tool use bypass model calls and write non-LLM stub episodes.
  - Validation failures trigger one correction retry with exact Pydantic error feedback and prior candidate text. Unresolved sessions are queued in `pending_sessions.json` with exponential backoff (1m, 5m, 15m; cap 3).
  - Enforces configurable size caps (~2,000 tokens); when exceeded, oldest `inferred-low-confidence` entries are archived to `memory/MEMORY_ARCHIVE.md` (stated and confirmed entries are never dropped).
  - Emits `background_task` status events visible in the Activity tab.
  - CLI operations `clea memory status` and `clea memory reconsolidate <session_id>` allow inspection and manual retries.
- **Context Injection**: At turn start, injects size-capped `MEMORY.md` and up to the last 5 episode summaries (capped by token count). Raw transcripts are never injected into context.
- **History Cap**: Sliding window message buffer with older turns condensed to a brief summary.
- **Conversation Resume & Management**:
  - `conversation.resume` reconstructs active sessions from transcripts. To ensure compatibility with Gemini 3 without HTTP 400 errors, past tool executions are replayed strictly as plain assistant text notes (`[used tool <name>: <args>]`) without fabricated `tool_calls` or `thought_signature`.
  - Transcripts can be renamed, pinned to top, exported as Markdown, or deleted. Soft-delete places transcripts in `logs/transcripts_trash/` with a 10s undo window and auto-purge (see [ADR 0006](decisions/0006-conversation-resume-and-management.md)).
  - Concurrency control rejects overlapping turns with 409 Conflict. Broadcast events keep multi-window sessions in sync.

### 5. Desktop UI (`ui/`)
- **Pure Client**: React 19, TypeScript, Vite 8, Tailwind CSS v4, shadcn-style primitives + `cmdk`, Zustand (`ui/src/protocol/store.ts`), `CleaClient` with exponential backoff + jitter (`ui/src/protocol/client.ts`).
- **Screens**:
  - **Chat**: streaming Markdown (`react-markdown` + `remark-gfm`), collapsible tool cards, inline approval cards (two-click confirmation for `destructive`), `Enter` send / `Shift+Enter` newline, "Continuing earlier conversation" marker on resumed threads, and a "Still working…" indicator after ~3 s.
  - **Sidebar & Command Palette (`Ctrl/Cmd+K`)**: navigation to Chat, Activity, Memory, Settings; pinned conversation group above date groups; three-dots conversation menu (Rename, Pin/Unpin, Export Markdown, Delete with confirmation and 10s undo toast); History header Delete All with strong confirmation.
  - **Activity**: displays only `background_task`, `scheduled`, and `agent_run` tasks (chat turns are filtered out) divided into "Running and upcoming" and "Recent" (capped at 50) with expandable error summaries.
  - **Memory**: view selector for Durable facts (fixed sections, search filter), Episode summaries (date grouped, short-session toggle), Archive (read-only with reasons and dates), and Consolidation status (metrics, pending/failed queues, retry triggers), plus an amber recoverable warning banner and transcripts subview.
  - **Settings**: non-secret daemon status, provider/model, permission tiers, registered tools.
  - **Call Overlay**: floating glass orb driven by `voice_state` events (`prefers-reduced-motion` respected). Shows active states (listening, thinking, speaking) and surfaces explicit error states if the voice process is offline or connection fails. Dev-only simulation panel (`DevVoiceMockPanel.tsx`) is dynamically imported only under `import.meta.env.DEV && VITE_CLEA_MOCKS === "true"` and excluded from production builds.
  - **Daemon Offline Screen**: clear startup instructions (`uv run clea daemon`) and auto-reconnect.
- **Browser Dev Token (`ui/dev-token-plugin.ts`)**: dev-only Vite plugin serving `/__clea/token` to localhost Host/Origin with `Cache-Control: no-store` and no CORS headers; excluded from production builds.

### 6. Electron Shell (`ui/electron/`)
- Built with `electron-vite` (uses `app.requestSingleInstanceLock()`). Does not bundle the daemon, though the offline screen offers an explicit **Start daemon** action in Electron that spawns the repo venv's `pythonw.exe -m clea daemon --background` (no shell).
- **Hardening**: `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`, `webSecurity: true`, strict CSP, navigation and `window.open` blocked.
- **Custom Protocol (`clea-app://app`)**: registered as `standard` + `secure` with Fetch/WebSocket support, strictly scoped to `out/renderer/` (rejects path traversal).
- **Preload (`ui/electron/preload.ts`)**: exposes `window.cleaDesktop` with `isElectron`, `getConnection()` (re-reads `session_token` fresh from disk on every connect/reconnect, never cached), `onQuickAskFocus()`, `closeQuickAsk()`, `getAutoLaunch()`, `setAutoLaunch()`, and `startDaemon()`.
- **System Tray, Auto-Launch & Quick Ask**: Tray menu (`Open`, `Quick Ask`, `Quit`), close-to-tray behavior, optional **Launch Clea at login** toggle in Settings (default off; starts hidden in tray via `--hidden` / `wasOpenedAtLogin`), and a configurable global shortcut (default `CommandOrControl+Alt+Space`, overridable via `<user_data_dir>/desktop_config.json`) opening an always-on-top glass Quick-Ask window.

### 7. Live Voice Surface & Audio Pipeline (`src/clea/surfaces/voice/`, `src/clea/surfaces/cli/voice.py`)
- **Process Architecture**:
  - Voice runs as a dedicated client surface process (`clea voice` / `VoiceRunner`) connecting to the core daemon over the typed WebSocket protocol.
  - Implements an OS instance lock (`voice.lock`) preventing concurrent voice surface processes.
- **Audio I/O & Windows WASAPI Negotiation (`AudioManager`, `audio.py`)**:
  - Dynamically probes and negotiates hardware native sample rates (`negotiate_input_sample_rate`) and channels (e.g. 48000 Hz or 44100 Hz) to eliminate PortAudio `PaErrorCode -9997` on Windows WASAPI devices (see [ADR 0007](decisions/0007-windows-wasapi-sample-rate-negotiation-and-wakeword-framing.md)).
  - Disambiguates endpoints by name substring and prioritizes `preferred_hostapi="WASAPI"` over MME/DirectSound (`resolve_audio_device`).
  - Polyphase rational resampling downmixes multi-channel frames to mono and resamples to 16 kHz `int16` (`resample_to_16k_mono`).
  - Thread-safe FIFO `AudioChunkBuffer` guarantees strictly non-overlapping 1280-sample (80 ms / 2560 bytes) chunks for both wake word detection and Live session streaming.
- **Wake Word Detection (`WakeWordDetector`, `wake_word.py`)**:
  - Built-in `openWakeWord` inference on 1D `numpy.int16` arrays using `hey_jarvis_v0.1.onnx` with local melspectrogram and embedding models.
  - Default threshold calibrated to `0.30` based on physical hardware measurements.
- **Gemini Live Backend & Watchdogs (`GeminiLiveBackend`, `gemini_live.py`)**:
  - Full-duplex bidirectional streaming of 16 kHz PCM input and 24 kHz PCM output with barge-in interruption.
  - VAD mode defaults to server-side activity detection (`vad_mode = "server"`).
  - Stall watchdog monitors outbound audio with zero server responses (default 15 s), logging loudly and reconnecting once via session resumption handles before error shutdown.
  - Full error surfacing connects the UI `CallOverlay` to daemon error broadcasts if the voice process is offline or disconnected.
