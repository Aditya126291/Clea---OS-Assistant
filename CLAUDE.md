# CLAUDE.md — Clea Architecture & Engineering Conventions

This file defines the non-negotiable rules for working in the Clea repository.

## Architecture Rules

1. **Hub-and-spoke only**:
   - `clea.core.daemon` owns the agent loop, sessions, memory, tool registry, and permissions.
   - Surfaces under `src/clea/surfaces/` (CLI now; voice and desktop UI later) MUST communicate with the daemon exclusively over the typed WebSocket protocol (`src/clea/core/protocol.py`).
   - Surfaces MUST NOT import `agent`, `sessions`, `memory`, `tools`, or `providers` internals.
2. **Protocol stability**:
   - All WebSocket messages are one of `RequestFrame`, `ResponseFrame`, or `EventFrame` defined in `src/clea/core/protocol.py`.
3. **Localhost-only binding**:
   - The daemon MUST bind to `127.0.0.1` (`localhost` / `::1`) only. Never expose it on `0.0.0.0` or add remote listeners.
4. **Tools, risk levels, and permissions**:
   - Every capability is registered in `ToolRegistry` (`src/clea/tools/registry.py`) with a name, description, MCP-compatible JSON Schema, and `RiskLevel` (`read_only`, `reversible`, `destructive`).
   - `destructive` tools MUST be refused unless an explicit confirmation callback approves them.
   - Every tool invocation MUST be logged to `<user_data_dir>/logs/audit.jsonl`.
   - Future tool preference order: APIs/MCP/CLIs -> direct OS calls -> accessibility-tree UI automation -> screenshots as last resort.
5. **Memory outside the repo**:
   - `MEMORY.md` and `memory/YYYY-MM-DD.md` MUST be stored in `platformdirs.user_data_dir("clea", appauthor=False)` (or `CLEA_DATA_DIR` in tests/dev), never inside the repo.
6. **Provider abstraction**:
   - Core code depends only on `LLMProvider` in `src/clea/providers/base.py`, never directly on a vendor SDK.

## Development Workflow

- Package manager & Python: `uv` (`Python 3.12`), `src/` layout.
- Lint & format: `uv run ruff check .` and `uv run ruff format .`
- Tests: `uv run pytest`
- Never commit `.env`, secrets, memory files, or logs.
- Use small, conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`). Do not push without explicit approval.
