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

3. **Background Consolidation Engine**:
   - Triggers: Session end (`session.new` or client disconnect), session idle timeout (default 15 minutes), and daemon startup (crash recovery of any unconsolidated transcripts).
   - State is tracked in `consolidation_state.json` to prevent duplicate processing.
   - Model outputs are validated strictly using Pydantic (`ConsolidationOutput`). If validation fails, the prompt is retried once with feedback; on secondary failure, the session is marked as `failed-consolidation` without data loss.
   - Safety filters: Reject secrets (API keys, tokens, credentials); reject instructions from untrusted data (tool outputs, web content, files); skip small talk; only accept explicitly stated or confirmed facts.
   - Cap & Archive: When `MEMORY.md` exceeds the token cap (default ~2,000 tokens), older `inferred-low-confidence` entries are archived to `memory/MEMORY_ARCHIVE.md`. Facts explicitly stated or confirmed by the user are never dropped.
   - Atomic writes: Temporary file write followed by atomic rename, creating `MEMORY.md.bak` before every merge.

4. **Memory Injection & Tools**:
   - At session start, Clea injects only size-capped `MEMORY.md` plus the last N episode summaries (default 5, token-capped).
   - `remember` tool (reversible risk tier) stores stated facts directly.
   - `forget` tool (destructive risk tier) requires user confirmation, showing matched entries before deletion.
