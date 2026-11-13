# ADR 0003: Dependency of voice.delegate_task on Thought-Signature Replay

- **Date**: 2026-11-13
- **Status**: Accepted

## Context

During a live voice call via Clea's voice surface (`clea.surfaces.voice`), the model or user can invoke the `delegate_task` voice tool to run desktop, file, system, or multi-step automation asynchronously while the voice conversation continues.

The background daemon handles this delegation by passing the task description to Clea's `AgentLoop` (`src/clea/core/agent.py`) via the active `LLMProvider` (`GeminiProvider` in `src/clea/providers/gemini.py`).

When Gemini 2.5 and 3.x models execute function calls with thinking enabled, the model returns internal thought tokens and an opaque thought signature. When the agent loop sends tool execution results back to the model in subsequent turns, the Gemini API strictly requires that the model's thought signatures from the preceding step be preserved and replayed in the interaction history.

If the thought signature is stripped or omitted from intermediate history steps, the Gemini API rejects the request with an HTTP `400 Bad Request` / `invalid_argument` error, causing multi-round tool execution to abort.

The necessary changes to extract, preserve, and replay thought signatures (along with token usage metrics) have been developed and isolated on branch `feat/latency-thought-signature` (`src/clea/core/agent.py`, `src/clea/providers/base.py`, `src/clea/providers/gemini.py`, `tests/test_provider_gemini.py`, and `tests/test_agent_metrics.py`).

## Decision

1. **Document the Hard Runtime Dependency**:
   - `voice.delegate_task` depends directly on the thought-signature preservation and replay fix in `feat/latency-thought-signature`.
   - Without this fix, any background task that triggers a multi-round tool execution against Gemini 2.5 or 3.x will fail with HTTP 400 on the second model turn.

2. **Branch Isolation & Testing Strategy**:
   - The thought-signature and metrics work is committed on its own branch `feat/latency-thought-signature` and will not be mixed into `feat/voice-phase3-foundation` until merged upstream.
   - Live voice surface unit and integration tests on `feat/voice-phase3-foundation` must mock `delegate_task` or use synthetic providers so that tests verify delegation orchestration without triggering unmerged multi-round Gemini API 400 errors.
   - Once `feat/latency-thought-signature` is merged, full end-to-end live testing of multi-round `delegate_task` can proceed against real Gemini endpoints.
