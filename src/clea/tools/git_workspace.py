"""Read-only Git workspace inspection tool.

Strictly executes read-only ``git`` subcommands with ``shell=False`` and a
bounded timeout. Never exposes or runs mutating Git flags or commands.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clea.tools.subprocess_utils import run_hidden

DEFAULT_WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[3]
GIT_TIMEOUT_SECONDS: float = 5.0
MAX_DIFF_CHARS: int = 4000

ALLOWED_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "rev-parse",
        "status",
        "worktree",
        "log",
        "diff",
    }
)


def resolve_git_repo_path(
    repo_path: str | Path | None = None,
    *,
    default_root: Path | None = None,
) -> Path:
    """Validate and resolve a Git repository or sibling worktree root directory."""
    base_root = (default_root or DEFAULT_WORKSPACE_ROOT).resolve()

    if repo_path is None:
        resolved = base_root
    else:
        raw_str = str(repo_path).strip()
        if not raw_str:
            resolved = base_root
        else:
            if any(ord(ch) < 32 for ch in raw_str):
                raise ValueError("repo_path contains invalid control characters.")
            expanded = os.path.expandvars(raw_str)
            candidate = Path(expanded).expanduser()
            if candidate.is_absolute():
                resolved = candidate.resolve()
            else:
                from_base = (base_root / candidate).resolve()
                resolved = from_base if from_base.exists() else candidate.resolve()

    if not resolved.is_dir():
        raise ValueError(f"Git repository directory does not exist: {resolved}")
    if not (resolved / ".git").exists():
        raise ValueError(f"Directory is not a Git repository or worktree: {resolved}")
    return resolved


def _run_git(
    repo_dir: Path,
    args: list[str],
    *,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Execute a strictly allowlisted read-only git subcommand with ``shell=False``."""
    if not args or args[0] not in ALLOWED_GIT_SUBCOMMANDS:
        raise ValueError(f"Disallowed git subcommand: {args!r}")
    if args[0] == "worktree" and (len(args) < 2 or args[1] != "list"):
        raise ValueError(f"Only 'git worktree list' is permitted: {args!r}")

    proc = run_hidden(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        runner=run_fn,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {detail or 'unknown error'}"
        )
    return proc.stdout


def _parse_status_lines(status_output: str) -> tuple[list[str], list[str], list[str]]:
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    for line in status_output.splitlines():
        if not line or line.startswith("## "):
            continue
        if len(line) < 4:
            continue
        xy = line[:2]
        path_part = line[3:].strip()
        if not path_part:
            continue
        if xy == "??":
            untracked.append(path_part)
            continue
        if xy[0] not in (" ", "?"):
            staged.append(path_part)
        if xy[1] not in (" ", "?"):
            unstaged.append(path_part)

    return staged, unstaged, untracked


def _parse_worktree_porcelain(output: str) -> list[dict[str, str]]:
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None and current.get("path"):
            worktrees.append(
                {
                    "path": current.get("path", ""),
                    "head": current.get("head", ""),
                    "branch": current.get("branch", ""),
                }
            )
        current = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            _flush()
            continue
        if line.startswith("worktree "):
            _flush()
            current = {"path": line[len("worktree ") :].strip(), "head": "", "branch": ""}
        elif current is not None:
            if line.startswith("HEAD "):
                current["head"] = line[len("HEAD ") :].strip()
            elif line.startswith("branch "):
                raw_branch = line[len("branch ") :].strip()
                current["branch"] = raw_branch.removeprefix("refs/heads/")
            elif line == "detached" and not current["branch"]:
                current["branch"] = "(detached)"
            elif line == "bare" and not current["branch"]:
                current["branch"] = "(bare)"

    _flush()
    return worktrees


def _parse_recent_commits(output: str) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        sha, _, subject = line.partition(" ")
        commits.append(
            {
                "sha": sha,
                "subject": subject.strip(),
                "oneline": line,
            }
        )
    return commits


def inspect_git_workspace(
    repo_path: str | Path | None = None,
    include_diff: bool = False,
    diff_stat_only: bool = True,
    *,
    default_root: Path | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Inspect a Git repository or worktree strictly via read-only commands."""
    repo_dir = resolve_git_repo_path(repo_path, default_root=default_root)

    branch_out = _run_git(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"], run_fn=run_fn)
    branch = branch_out.strip() or "HEAD"

    status_out = _run_git(repo_dir, ["status", "--short", "--branch"], run_fn=run_fn)
    status_clean_text = status_out.rstrip("\r\n")
    staged, unstaged, untracked = _parse_status_lines(status_clean_text)

    worktree_out = _run_git(repo_dir, ["worktree", "list", "--porcelain"], run_fn=run_fn)
    worktrees = _parse_worktree_porcelain(worktree_out)

    log_out = _run_git(repo_dir, ["log", "-n", "5", "--oneline"], run_fn=run_fn)
    recent_commits = _parse_recent_commits(log_out)

    diff_text: str | None = None
    diff_truncated = False
    if include_diff:
        diff_args = ["diff", "--stat"] if diff_stat_only else ["diff"]
        raw_diff = _run_git(repo_dir, diff_args, run_fn=run_fn).rstrip("\r\n")
        if not diff_stat_only and len(raw_diff) > MAX_DIFF_CHARS:
            diff_text = raw_diff[:MAX_DIFF_CHARS]
            diff_truncated = True
        else:
            diff_text = raw_diff

    return {
        "ok": True,
        "repo_path": str(repo_dir),
        "branch": branch,
        "is_clean": not staged and not unstaged and not untracked,
        "status": status_clean_text,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "worktrees": worktrees,
        "recent_commits": recent_commits,
        "diff": diff_text,
        "diff_truncated": diff_truncated,
    }
