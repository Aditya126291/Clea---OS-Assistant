# ADR 0006: Conversation Resume from Transcripts and Management Lifecycle

- **Date**: 2026-11-13
- **Status**: Accepted

## Context

Past conversations in Clea could previously only be viewed as read-only historical transcripts. Navigating away to another screen (such as Memory or Activity) or experiencing a daemon restart locked conversations into read-only mode or started a fresh session with an empty message list. Furthermore, users had no mechanism to manage past chats (renaming cryptic first-message titles, pinning key threads, exporting conversations as Markdown, or deleting conversations).

Additionally, Gemini 3 with `store=False` strictly validates past turn histories. When replaying previous turns, reconstructing synthetic `function_call` or `tool` result steps without the original provider `thought_signature` causes HTTP 400 Bad Request errors. Rebuilding conversation context must prevent token exhaustion while maintaining provider safety.

## Decisions

1. **Resume from Transcript Store**:
   - `conversation.resume(session_id)` reconstructs the daemon's active in-memory session directly from `logs/transcripts/<session_id>.jsonl`.
   - If a client sends a turn for a session evicted from daemon memory or after a daemon restart, the daemon transparently auto-resumes the session from its transcript file.
   - **Safe Tool Replay (Gemini 3 Compatibility)**: Transcripts store tool executions as summaries. During resume history reconstruction, past tool steps are rendered strictly as plain user and assistant text notes (e.g. `[used tool <name>: <args>]`) rather than fabricating synthetic `tool_calls` or `thought_signature` blocks. Verified with strict replay mock providers.
   - History sliding window and summary caps are applied during resume to enforce context token budgets.

2. **Concurrency & Multi-Window Synchronization**:
   - Enforce exactly one active turn per session ID: if a second request is issued while a turn is in flight, it is rejected with a conflict error (`Turn already running for session`).
   - Broadcast `conversation_updated` and `conversation_deleted` events across connected WebSocket clients to synchronize title updates, pin toggles, and deletion states across windows.

3. **Soft-Delete, 10-Second Undo Window, and Purge**:
   - Deletions (`conversation.delete`) move transcripts into `<user_data_dir>/logs/transcripts_trash/<session_id>_<timestamp>.jsonl` with companion `.meta.json` records.
   - The UI surfaces an Undo toast with a 10-second timer. If `conversation.undo_delete` is called within the window, the transcript is restored to `logs/transcripts/`.
   - On expiration of the timer or during daemon startup (`purge_trash()`), soft-deleted files are permanently unlinked.
   - "Delete all conversations" on the History section header requires the user to type `DELETE` in a strong confirmation modal.
   - Deletion includes an optional parameter (`delete_episodes: bool`) that removes corresponding session episode entries from `memory/episodes/` without modifying memory consolidation logic.
   - When the currently active conversation is deleted, the chat interface resets to a clean new chat.

4. **Security, Path Traversal Defenses, and Audit Logging**:
   - Strict session ID validation (`^[a-zA-Z0-9_-]{1,64}$`) and resolved path containment (`is_relative_to`) reject traversal attacks (`../`, absolute paths, control characters).
   - Administrative conversation endpoints are restricted to authenticated WebSocket clients; LLM tool registries MUST NOT expose conversation deletion or renaming tools to the model.
   - Deletion and administrative actions are logged to `logs/audit.jsonl` with action and session ID without logging conversation content or leaking user messages into `daemon.log`.

5. **Desktop & Web UI**:
   - Pinned conversations are rendered in a dedicated "Pinned" group above date-grouped history.
   - History items feature a three-dots menu (keyboard and hover accessible) for inline rename (with HTML stripping and length limits), pin toggle, Markdown export, and deletion.
   - Export downloads the conversation as Markdown via the Electron main process (`dialog.showSaveDialog`) or browser Blob download.
   - Command palette (`Ctrl/Cmd+K`) provides quick actions to Rename, Pin, Export, or Delete the active conversation.
   - Subtle "Continuing earlier conversation" divider separates previous transcript turns from newly appended turns.

6. **Interplay with Memory Consolidation**:
   - Resumed sessions preserve their original session ID. When a resumed session ends or becomes idle, `consolidate_session(session_id)` inspects consolidation status (`is_consolidated(session_id)`). Because the session was already consolidated upon its initial completion, it safely no-ops without duplicating episode summaries.
