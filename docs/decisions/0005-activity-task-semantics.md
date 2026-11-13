# ADR 0005: Activity Task Semantics and Separation from Chat Turns

- **Date**: 2026-11-13
- **Status**: Accepted

## Context

Clea previously emitted a `task_status` protocol event for every user chat turn, causing standard conversation interactions to appear in the Activity tab as failed/succeeded tasks. The Activity tab was intended for real background work (scheduled jobs, delegated tasks, agent runs, and background maintenance), while chat turns already live naturally in the conversation view.

## Decision

1. **Protocol `TaskKind` Classification**:
   - `TaskKind`: Literal `["chat_turn", "background_task", "scheduled", "agent_run"]`.
   - `TaskState`: Literal `["queued", "running", "succeeded", "failed", "cancelled"]`.
   - `TaskStatusData` fields: `task_id`, `title`, `status`, `started_at`, `kind`, `finished_at`, `next_run_at`, and `detail`.

2. **Filtering Semantics**:
   - `chat_turn`: Emitted strictly for live conversation UI updates (e.g. status banners and thinking states). Filtered out completely from the Activity tab.
   - `background_task`: Long-running or asynchronous daemon operations such as memory consolidation (`"Summarising session 18:11"`).
   - `scheduled`: Automated recurring jobs with `next_run_at` timestamps.
   - `agent_run`: Voice-delegated tasks and autonomous subagent execution.

3. **Activity UI Structure**:
   - "Running and upcoming": Displays active tasks (`running`, `queued`, or tasks with `next_run_at`).
   - "Recent": Displays completed tasks (`succeeded`, `failed`, `cancelled`), capped at 50 items.
   - Empty state: Display clean message *"Nothing running. Scheduled jobs and background tasks will appear here."*
   - Error summaries: Expandable details on failed tasks showing concise error explanations without exposing raw API payloads or credentials.
   - Dev testing: Under development mode (`import.meta.env.DEV`), provide an "Emit sample tasks (dev)" action invoking `dev.emit_sample_task`.
