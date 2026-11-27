---
name: clea-worktree-workflow
description: >-
  Provision, verify, and safely clean up Windows git worktrees for Clea using NTFS
  junctions for .venv and ui\node_modules without corrupting the main repo or
  importing master's src via editable .pth. Use when creating, verifying, or
  removing a Clea feature worktree.
---

# Clea Windows Worktree Workflow

## 1. Provisioning an Isolated Worktree

1. Create the worktree off `master`:
   ```powershell
   git -C C:\Projects\Clea worktree add -b "<branch>" "<wt>" master
   ```
2. Provision `.venv` and `ui\node_modules`:
   - If `pyproject.toml` / dependencies are unchanged, junction `.venv` and `ui\node_modules`:
     ```powershell
     cmd /c mklink /J "<wt>\.venv" "C:\Projects\Clea\.venv"
     cmd /c mklink /J "<wt>\ui\node_modules" "C:\Projects\Clea\ui\node_modules"
     ```
   - Whenever `pyproject.toml` dependencies change, use an **isolated** `.venv` inside `<wt>` instead of junctioning `C:\Projects\Clea\.venv`.
   - **Never** run `pnpm install` inside a junctioned worktree.
3. Copy `.env` from `C:\Projects\Clea\.env` if present.

## 2. Running Codegen & Verification Inside a Junctioned Worktree

Because a junctioned `.venv` carries an editable `.pth` pointing to `C:\Projects\Clea\src`, direct Python scripts silently import `master`'s `src` unless `PYTHONPATH` is set:
- `pytest` prepends local `src` automatically via `pyproject.toml`.
- Direct scripts (especially `scripts/gen_ts_types.py`) **MUST** set `$env:PYTHONPATH = "src"`:
  ```powershell
  $env:PYTHONPATH = "src"
  uv run --no-sync python scripts/gen_ts_types.py --check
  uv run --no-sync ruff check .
  uv run --no-sync pytest -q --ignore=tests/test_live_e2e.py
  pnpm --dir ui typecheck
  pnpm --dir ui test --run
  ```

## 3. Safe Cleanup of a Merged Worktree

Before removing `<wt>`:
1. Stop any running `electron.exe` launched from Clea and verify ports `5176`/`8768` are clear if rebuilding UI.
2. Inspect `LinkType` on `.venv` and `ui\node_modules` inside `<wt>` and unlink **only** junctions (never delete real directories):
   ```powershell
   foreach ($p in @("<wt>\.venv", "<wt>\ui\node_modules")) {
     if (Test-Path $p) {
       $item = Get-Item -Force $p
       if ($item.LinkType -eq 'Junction') { cmd /c rmdir "$p" }
     }
   }
   ```
3. Remove any gitignored `.env` inside `<wt>`, then run:
   ```powershell
   git -C C:\Projects\Clea worktree remove --force "<wt>"
   git -C C:\Projects\Clea worktree prune
   git -C C:\Projects\Clea branch -d "<merged-branch>"
   ```
