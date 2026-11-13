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
   - Three storage tiers: `memory/MEMORY.md` (durable facts with fixed sections: Profile, Preferences, Environment, Projects, Decisions, Open Threads), `memory/episodes/` (session summaries), and `logs/transcripts/` (structured JSONL transcripts) MUST be stored in `platformdirs.user_data_dir("clea", appauthor=False)` (or `CLEA_DATA_DIR` in tests/dev), never inside the repo.
   - Consolidation runs as a background task on session completion, 15m idle, or startup crash recovery. Over-cap entries are archived to `memory/MEMORY_ARCHIVE.md`.
   - Consolidation resilience: supports schema-constrained JSON `response_format` where available, accepts harmless schema aliases (`value` -> `text`), passes specific validation errors and prior outputs on correction retry (max 1 retry), and tracks failed sessions in `pending_sessions.json` with exponential backoff (1m, 5m, 15m; cap 3). Short/trivial sessions (< N user turns, default 2 without tool use) skip model calls and log a stub episode.
   - CLI commands `clea memory status` and `clea memory reconsolidate <session_id>` communicate over WebSocket.
   - Memory UI provides a view selector (Durable facts, Episode summaries, Archive, Consolidation status) with persistent preference, plus an amber recoverable warning banner.
6. **Activity semantics**:
   - `task_status` events carry `kind` (`chat_turn`, `background_task`, `scheduled`, `agent_run`). Chat turns are excluded from the Activity tab, which shows only real background, scheduled, and agent work.
7. **Provider abstraction**:
   - Core code depends only on `LLMProvider` in `src/clea/providers/base.py`, never directly on a vendor SDK.
8. **Dev mocks stay out of production**:
   - Any UI mock panels or dev-server helpers MUST be gated behind `import.meta.env.DEV` (and `VITE_CLEA_MOCKS` where applicable) so they are dead-code-eliminated from production builds.
9. **Conversation resume and management**:
   - Past conversations resume from transcripts (`conversation.resume`) surviving daemon restarts and navigation. Replaying past turns formats tool steps as plain assistant text notes without fabricating `tool_calls` or `thought_signature`.
   - Transcripts can be renamed, pinned, exported (Markdown), and soft-deleted into `<user_data_dir>/logs/transcripts_trash/` with a 10s undo window before permanent purge (see `docs/decisions/0006-conversation-resume-and-management.md`). Deletions are audit-logged and never exposed to model tools. Strict regex (`^[a-zA-Z0-9_-]{1,64}$`) and path containment prevent path traversal.
10. **Voice Surface & Audio Pipeline**:
   - `clea.surfaces.voice` runs as a client surface connecting to the daemon over WebSocket.
   - Audio input streams MUST negotiate hardware native rates (`negotiate_input_sample_rate`) and downmix/resample to 16 kHz mono (`resample_to_16k_mono`) to prevent PortAudio `PaErrorCode -9997` on Windows WASAPI (see `docs/decisions/0007-windows-wasapi-sample-rate-negotiation-and-wakeword-framing.md`).
   - All wake word and Gemini Live audio input MUST pass through strict 1280-sample (80 ms / 2560 bytes) FIFO chunking (`AudioChunkBuffer`).
   - Diagnostic tools (`clea voice doctor`) MUST run purely in-memory (no saving recorded audio, logs, or screenshots to disk; no secrets logged).

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
