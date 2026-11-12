# Clea

Clea is a local-first, hub-and-spoke personal AI assistant for the desktop.

Milestone 1 provides the text-only foundation:
- A localhost-only WebSocket daemon (`clea daemon`) owning the agent loop, sessions, tools, permissions, and memory.
- A terminal chat client (`clea chat`) that streams replies over the typed protocol.
- Human-readable Markdown memory (`MEMORY.md` and `memory/YYYY-MM-DD.md`) and JSONL tool audit logs stored outside the repo in your OS user data directory.
- A swappable `LLMProvider` interface with `GeminiProvider` (`google-genai`, `gemini-3.8-flash`).

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

Connect the chat client in terminal 2:

```powershell
uv run clea chat
```

## Development Checks

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

See [docs/architecture.md](docs/architecture.md) and [CLAUDE.md](CLAUDE.md) for architecture details and rules.
