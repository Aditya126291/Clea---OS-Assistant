# Clea Repository Agent Rules

Read [CLAUDE.md](./CLAUDE.md) for full architecture, protocol, security, and voice pipeline invariants. In addition, follow these non-negotiable rules on every task in this repository:

## Architecture & Model Constraints
- **Strictly Generalized Desktop Automation:** Never add app-specific hacks or rules tailored to individual apps (e.g., Spotify, Settings, Calculator). Every perception, input, and verification mechanism must generalize across Win32, UWP/WinUI3, Electron/CEF, custom-rendered canvases, and browsers.
- **Cloud-Only LLMs:** Never introduce local ML or LLM models. Built-in Windows OS APIs (Win32 COM UIA, GDI/DWM, WinRT APIs such as `Windows.Media.Control` / `Windows.Media.Ocr`) and cloud Gemini APIs are permitted.
- **Verification-First Honesty & Telemetry:** Never claim a mutating OS goal succeeded when `goal_verified` is `false`. Log sanitized execution telemetry in `audit.jsonl` (including structured error audits on pre-completion exceptions), never logging raw typed or clipboard secrets.

## Workspace & Runtime Safety
- **Isolation:** Keep `C:\Projects\Clea` (`master`) and `%LOCALAPPDATA%\clea` untouched during feature worktree development and tests. Use deterministic mocked unit tests with zero live desktop hooks.
- **No Live `8768` Processes or Unrequested Pushes:** Never start live daemon/voice processes on port `8768` in tests and never push to git remotes without explicit user approval.
- **No Review Subagents:** Do not spawn code-review subagents unless explicitly requested by the user.
- **Protocol Single Source of Truth:** After modifying `src/clea/core/protocol.py`, regenerate `ui/src/protocol/generated.ts` via `scripts/gen_ts_types.py` (with `PYTHONPATH=src` inside junctioned worktrees) and verify `--check` passes.
