# ADR 0004: Three-Tier Memory Storage and Background Consolidation

- **Date**: 2026-11-13
- **Status**: Accepted

## Context

Clea previously stored all chat interactions by directly appending raw messages to daily Markdown files (`memory/YYYY-MM-DD.md`). `MEMORY.md` was left with placeholder text, resulting in Clea never learning durable facts or preferences across sessions. Furthermore, displaying raw daily transcripts in the Memory tab cluttered the UI with headings like `17:01:47 India Standard Time (session <hash>)`, and injecting raw transcripts into LLM context consumed excessive tokens and exposed untrusted tool data.

## Decision

1. **Three Storage Tiers (Outside Repository in User Data Dir)**:
   - `memory/MEMORY.md`: Durable facts and preferences only. Structured into fixed sections (`Profile`, `Preferences`, `Environment`, `Projects`, `Decisions`, `Open Threads`). Format: `- [id] (YYYY-MM-DD, source) text`, where source is `stated`, `confirmed`, or `inferred-low-confidence`.
   - `memory/episodes/YYYY-MM-DD.md`: Short per-session summaries (3-5 lines each with session ID, start time, topics, outcomes, unresolved items).
   - `logs/transcripts/<session_id>.jsonl`: Structured raw transcripts (one JSON line per message with role, text, ISO 8601 offset timestamp, and tool call summaries; initial line contains session ID, start timestamp, and title).

2. **Idempotent Migration**:
   - Existing daily Markdown files (`memory/YYYY-MM-DD.md`) are converted into `logs/transcripts/<session_id>.jsonl` without losing messages.
   - Original legacy files are safely archived to `backup_legacy_logs/` in the user data dir.
   - Migration is safe to run repeatedly.

3. **Background Consolidation Engine & Resilience**:
   - Triggers: Session end (`session.new` or client disconnect), session idle timeout (default 15 minutes), daemon startup crash recovery, or manual request.
   - Structured Output & Alias Tolerance: Providers supporting schema constraints receive `response_format` JSON schema generated from Pydantic. `ConsolidationOperation` accepts harmless alias drift (`value` -> `text`) while keeping other fields strict.
   - Fault-Tolerant Correction Retry: On validation failure, error details and prior candidate JSON are fed back to the model for a single targeted correction retry.
   - Retry Queue & Backoff: Unresolved sessions are recorded in `pending_sessions.json` outside the repository with exponential backoff delays (1m, 5m, 15m; cap 3 attempts). Pending sessions are retried on daemon start or explicit command.
   - Trivial Session Skipping: Sessions with fewer than N user turns (default 2, configurable via `CLEA_CONSOLIDATION_MIN_TURNS`) and no tool usage bypass LLM calls and log a non-LLM stub episode flagged `trivial`.
   - Cap & Archive: When `MEMORY.md` exceeds the token cap (default ~2,000 tokens), older `inferred-low-confidence` entries are archived to `memory/MEMORY_ARCHIVE.md`. Facts explicitly stated or confirmed by the user are never dropped.
   - State and safety: State is tracked in `consolidation_state.json` and `pending_sessions.json`. Safety filters reject secrets (API keys, tokens, credentials) and ignore instructions in untrusted web/tool outputs.
   - Atomic writes: Temporary file write followed by atomic rename, creating `MEMORY.md.bak` before every merge.

4. **Memory Protocol, CLI, & UI Experience**:
   - WebSocket Protocol: Typed methods `memory.facts`, `memory.episodes`, `memory.archive`, `memory.status`, and `memory.reconsolidate` provide structured access without raw file reading by surfaces.
   - CLI: `clea memory status` reports pending, failed, and completed session metrics; `clea memory reconsolidate <session_id>` forces re-evaluation.
   - UI Architecture:
     - Recoverable Amber Warning Banner: Replaces raw red error text with a plain-language notification ("Couldn't update memory from one conversation"), sanitized error details expansion, retry action, and dismissibility (persisted in localStorage).
     - View Selector: Dropdown selector (shadcn Select) remembering selection across restarts:
       1. Durable Facts: Fixed sections with collapsible cards, entry count badges, search filtering, and empty-state guidance.
       2. Episode Summaries: Grouped by date, newest first, with a "Show short sessions" toggle and transcript links.
       3. Archive: Read-only list of archived facts with timestamps and rationales.
       4. Consolidation Status: High-level metrics, pending retries with backoff schedules, failed sessions, and manual retry buttons.
     - Transcripts Subview: Preserved for inspecting full JSONL transcripts.

5. **Memory Injection & Tools**:
   - At session start, Clea injects only size-capped `MEMORY.md` plus the last N episode summaries (default 5, token-capped).
   - `remember` tool (reversible risk tier) stores stated facts directly.
   - `forget` tool (destructive risk tier) requires user confirmation, showing matched entries before deletion.
