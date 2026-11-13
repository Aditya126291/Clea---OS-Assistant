# CLAUDE.md — Clea Architecture & Engineering Conventions

This file defines the non-negotiable rules for working in the Clea repository.

## Architecture Rules

1. **Hub-and-spoke only**:
   - `clea.core.daemon` owns the agent loop, sessions, memory, tool registry, and permissions.
   - Surfaces under `src/clea/surfaces/` (CLI) and `ui/` (browser UI and Electron desktop shell) MUST communicate with the daemon exclusively over the typed WebSocket protocol (`src/clea/core/protocol.py`).
   - Surfaces MUST NOT import `agent`, `sessions`, `memory`, `tools`, or `providers` internals, and `ui/` MUST NOT read memory/config files directly or hold secrets.
2. **Protocol stability & codegen**:
   - All WebSocket messages are discriminated Pydantic v2 models (`RequestFrame`, `ResponseFrame`, `EventFrame`) in `src/clea/core/protocol.py`.
   - `src/clea/core/protocol.py` is the single source of truth. Regenerate `ui/src/protocol/generated.ts` with `uv run python scripts/gen_ts_types.py` whenever the protocol changes; `scripts/gen_ts_types.py --check` fails on drift.
3. **Localhost-only binding & handshake security**:
   - The daemon MUST bind to `127.0.0.1` (`localhost` / `::1`) only. Never expose it on `0.0.0.0` or add remote listeners.
   - Every WebSocket handshake requires a fresh per-start 256-bit session token (`<user_data_dir>/session_token`, restricted to the current user via POSIX `0600` or Windows SID NTFS ACL) passed in `Sec-WebSocket-Protocol` (`clea.v1, clea.token.<token>`), a local `Host` header, and (when `Origin` is present) an exact match in `CLEA_ALLOWED_ORIGINS` (defaults: `http://localhost:5173`, `http://127.0.0.1:5173`, `clea-app://app`). Wildcard (`*`) and `null` origins are forbidden.
   - Single-instance lock (`<user_data_dir>/daemon.lock`) prevents a second daemon from overwriting a running daemon's `session_token`, and clients re-read `session_token` fresh on every connect/reconnect.
4. **Tools, risk levels, and permissions**:
   - Every capability is registered in `ToolRegistry` (`src/clea/tools/registry.py`) with a name, description, MCP-compatible JSON Schema, and `RiskLevel` (`read_only`, `reversible`, `destructive`).
   - `read_only` tools run automatically. `reversible` and `destructive` tools MUST be refused unless an explicit confirmation callback approves them (timeout defaults to deny). Destructive actions require an explicit second confirmation step in surfaces.
   - Every tool invocation MUST be logged to `<user_data_dir>/logs/audit.jsonl`.
   - Future tool preference order: APIs/MCP/CLIs -> direct OS calls -> accessibility-tree UI automation -> screenshots as last resort.
5. **Memory outside the repo**:
   - Three storage tiers: `memory/MEMORY.md` (durable facts with fixed sections), `memory/episodes/` (session summaries), and `logs/transcripts/` (structured JSONL transcripts) MUST be stored in `platformdirs.user_data_dir("clea", appauthor=False)` (or `CLEA_DATA_DIR` in tests/dev), never inside the repo.
   - Consolidation runs as a background task on session completion, 15m idle, or startup crash recovery. Over-cap entries are archived to `memory/MEMORY_ARCHIVE.md`.
6. **Activity semantics**:
   - `task_status` events carry `kind` (`chat_turn`, `background_task`, `scheduled`, `agent_run`). Chat turns are excluded from the Activity tab, which shows only real background, scheduled, and agent work.
7. **Provider abstraction**:
   - Core code depends only on `LLMProvider` in `src/clea/providers/base.py`, never directly on a vendor SDK.
8. **Dev mocks stay out of production**:
   - Any UI mock panels or dev-server helpers MUST be gated behind `import.meta.env.DEV` (and `VITE_CLEA_MOCKS` where applicable) so they are dead-code-eliminated from production builds.
9. **Conversation resume and management**:
   - Past conversations resume from transcripts (`conversation.resume`) surviving daemon restarts and navigation. Replaying past turns formats tool steps as plain assistant text notes without fabricating `tool_calls` or `thought_signature`.
   - Transcripts can be renamed, pinned, exported (Markdown), and soft-deleted into `<user_data_dir>/logs/transcripts_trash/` with a 10s undo window before permanent purge. Deletions are audit-logged and never exposed to model tools. Strict regex (`^[a-zA-Z0-9_-]{1,64}$`) and path containment prevent path traversal.

## Development Workflow

- Python: `uv` (`Python 3.12`), `src/` layout:
  - `uv run pytest`
  - `uv run ruff check .` and `uv run ruff format --check .`
  - `uv run python scripts/gen_ts_types.py --check`
- UI & Electron (`ui/`): `pnpm`:
  - `pnpm test`, `pnpm lint`, `pnpm typecheck`, `pnpm format:check`
  - `pnpm dev` (browser dev), `pnpm build` (web prod), `pnpm desktop:dev`, `pnpm desktop:build`
- Never commit `.env`, secrets, tokens, memory files, or logs.
- Use small, conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`). Do not push without explicit approval.
