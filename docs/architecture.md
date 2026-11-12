# Clea Architecture (Milestone 1)

Clea uses a **hub-and-spoke** architecture designed for a local, voice-first personal desktop assistant. Milestone 1 establishes the text-only core foundation.

## System Topology

```
+---------------------------+        ws://127.0.0.1:8765        +---------------------------+
| Surface: CLI (`clea chat`) | <==============================> | Core Daemon (`clea daemon`) |
| (imports protocol only)   |   Request / Event / Response JSON |                           |
+---------------------------+                                   |  - SessionManager         |
                                                                |  - AgentLoop              |
  Future surfaces:                                              |  - ToolRegistry + Audit   |
  - Voice process                                               |  - MemoryStore (Markdown) |
  - Desktop UI                                                  |  - LLMProvider (Gemini)   |
                                                                +---------------------------+
                                                                              |
                                                                              v
                                                                OS User Data Dir (platformdirs)
                                                                - MEMORY.md
                                                                - memory/YYYY-MM-DD.md
                                                                - logs/audit.jsonl
```

## Components

### 1. Typed WebSocket Protocol (`src/clea/core/protocol.py`)
- Defines three Pydantic v2 frame types discriminated by `frame_type`:
  - `RequestFrame` (`chat.send`, `session.new`, `health.ping`)
  - `ResponseFrame` (`ok`, `result`, `error`, matched by request `id`)
  - `EventFrame` (`text.delta`, `tool.start`, `tool.end`, `turn.done`, `error`)
- Contains zero imports from core internals so surfaces remain decoupled.

### 2. Core Daemon & Agent Loop (`src/clea/core/`)
- **`daemon.py` (`CleaDaemon`)**: Binds strictly to `127.0.0.1` (`localhost` / `::1`) using `websockets`. Rejects non-localhost bind addresses.
- **`sessions.py` (`SessionManager`)**: Holds per-session conversation history (`Message` list) in memory.
- **`agent.py` (`AgentLoop`)**: Prepends `MEMORY.md` to the system instruction, streams tokens from `LLMProvider`, executes any requested tools via `ToolRegistry`, feeds tool results back into the conversation loop, and persists each completed turn to the daily Markdown log.

### 3. Tool Registry & Permissions (`src/clea/tools/`)
- **`registry.py` (`ToolRegistry`)**: Registers tools with `name`, `description`, JSON Schema `input_schema` (exportable in MCP format via `to_mcp_tool()`), and `RiskLevel` (`read_only`, `reversible`, `destructive`).
- Refuses `destructive` tools with `PermissionDeniedError` unless an explicit confirmation callback returns `True`.
- **`AuditLogger`**: Appends a JSONL record to `<user_data_dir>/logs/audit.jsonl` for every tool invocation (allowed, denied, or errored).
- **`system_info.py`**: Provides `get_system_info` (`read_only`), returning local time/timezone and basic host OS details.

### 4. Memory Store (`src/clea/memory/store.py`)
- Stores human-readable Markdown outside the repository under `platformdirs.user_data_dir("clea", appauthor=False)` (overridable via `CLEA_DATA_DIR`):
  - `MEMORY.md`: long-term memory loaded into the system prompt.
  - `memory/YYYY-MM-DD.md`: append-only daily conversation logs.

### 5. LLM Provider Interface (`src/clea/providers/`)
- **`base.py` (`LLMProvider`)**: Abstract streaming interface (`stream_turn`) using provider-agnostic `Message`, `ToolCall`, and `StreamChunk` models.
- **`gemini.py` (`GeminiProvider`)**: Implements `LLMProvider` on top of the `google-genai` Interactions API (`gemini-3.8-flash` by default, `store=False`).

### 6. Terminal Surface (`src/clea/surfaces/cli/main.py`)
- Provides `clea daemon` and `clea chat`.
- `clea chat` communicates exclusively via `clea.core.protocol` over `ws://127.0.0.1:8765`, streaming `text.delta` events live to stdout.
