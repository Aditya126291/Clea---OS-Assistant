# Clea

Clea is a local-first, hub-and-spoke personal AI assistant for the desktop.

Milestone 1 provides the text-only foundation:
- A localhost-only WebSocket daemon (`clea daemon`) owning the agent loop, sessions, tools, permissions, and memory.
- A terminal chat client (`clea chat`) that streams replies over the typed protocol.
- Three-tier memory stored in your OS user data directory: durable facts in `memory/MEMORY.md`, per-session summaries in `memory/episodes/`, and structured transcripts in `logs/transcripts/`, with automated background consolidation, safety filtering, and size-cap archiving.
- Activity task semantics tracking real background tasks, scheduled jobs, and agent runs (filtering out chat turns).
- Low-latency streaming with pre-header timeout retry, thought-signature replay, turn rollback on error, and UI progress feedback.
- A swappable `LLMProvider` interface with `GeminiProvider` (`google-genai`).

## Setup

1. Install dependencies (Python 3.12 via `uv`):

   ```powershell
   uv sync
   ```

2. Configure your API key:

   ```powershell
   Copy-Item .env.example .env
   ```

   Edit `.env` and set `GEMINI_API_KEY`.

## Running

Start the daemon in terminal 1:

```powershell
uv run clea daemon
```

Or run it in background logging mode (writes to `%LOCALAPPDATA%\clea\logs\daemon.log` on Windows):

```powershell
uv run clea daemon --background
```

Connect the chat client in terminal 2:

```powershell
uv run clea chat
```

## Windows Auto-Start at Login

To register a current-user Task Scheduler logon task (`CleaDaemon`) that runs `pythonw.exe -m clea daemon --background` hidden in your interactive session (no admin rights required):

```powershell
uv run clea autostart install
uv run clea autostart status
uv run clea autostart uninstall
```

In the Electron desktop app, open **Settings -> Launch Clea at login** (off by default) to start the app minimized to the system tray at login. When the daemon restarts, the desktop app re-reads `<user_data_dir>/session_token` automatically on reconnect (manual check: restart the daemon with `clea daemon --background` while the Electron window is open and verify it reconnects without restarting the app).

## Development Checks

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/gen_ts_types.py --check
```

UI & Electron checks (`ui/`):

```powershell
cd ui
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm desktop:build
```

See [docs/architecture.md](docs/architecture.md), [docs/decisions/0002-windows-autostart-session.md](docs/decisions/0002-windows-autostart-session.md), and [CLAUDE.md](CLAUDE.md) for architecture details and rules.
