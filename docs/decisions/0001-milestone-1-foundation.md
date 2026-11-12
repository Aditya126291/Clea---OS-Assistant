# ADR 0001: Milestone 1 Foundation Choices

- **Date**: 2026-11-12
- **Status**: Accepted

## Decisions

1. **Python 3.12 managed by `uv` (`src` layout)**: Keeps the runtime isolated from the host Python (`3.14.3`) and reproducible via `uv.lock`.
2. **Gemini API via `google-genai` Interactions API (`gemini-3.8-flash`)**: Uses `client.aio.interactions.create(..., stream=True, store=False)` with stateless step history translated from Clea's provider-agnostic `Message` list so other providers (Anthropic, Ollama) can be swapped behind `LLMProvider`.
3. **Localhost-only WebSocket transport (`127.0.0.1`)**: `CleaDaemon` rejects non-localhost bind hosts to prevent accidental LAN/WAN exposure.
4. **External Markdown memory & JSONL audit log via `platformdirs`**: User conversations and tool logs never live inside the Git repository.
