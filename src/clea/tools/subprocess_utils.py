"""Centralized Windows-safe hidden subprocess helpers (zero console flashes)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
STARTF_USESHOWWINDOW: int = getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
SW_HIDE: int = 0

_ORIG_WHICH = shutil.which
_PS_EXE_CACHE: dict[bool, str] = {}


def hidden_subprocess_kwargs(*, extra_flags: int = 0) -> dict[str, Any]:
    """Return Windows kwargs that suppress console windows for background subprocesses.

    Applies both ``CREATE_NO_WINDOW`` (``0x08000000``) and a hidden ``STARTUPINFO``
    (``STARTF_USESHOWWINDOW`` + ``SW_HIDE``) on Windows. Returns ``{}`` on non-Windows.
    """
    if sys.platform != "win32":
        return {}
    kwargs: dict[str, Any] = {
        "creationflags": CREATE_NO_WINDOW | int(extra_flags),
    }
    if hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= STARTF_USESHOWWINDOW
        si.wShowWindow = SW_HIDE
        kwargs["startupinfo"] = si
    return kwargs


def resolve_powershell_exe(*, prefer_pwsh: bool = False) -> str | None:
    """Resolve ``powershell.exe`` (or ``pwsh``) via ``shutil.which`` with safe caching."""
    use_cache = shutil.which is _ORIG_WHICH
    if use_cache:
        cached = _PS_EXE_CACHE.get(prefer_pwsh)
        if cached and Path(cached).is_file():
            return cached

    candidates = (
        ("pwsh.exe", "pwsh", "powershell.exe", "powershell")
        if prefer_pwsh
        else ("powershell.exe", "powershell", "pwsh.exe", "pwsh")
    )
    for name in candidates:
        found = shutil.which(name)
        if found:
            if use_cache:
                _PS_EXE_CACHE[prefer_pwsh] = found
            return found
    return None


def _should_apply_hidden_kwargs(
    callable_obj: Any,
    default_fn: Any,
    *,
    force: bool = False,
) -> bool:
    """Return True when hidden Windows kwargs should be merged into a subprocess call."""
    if sys.platform != "win32":
        return False
    if force:
        return True
    return callable_obj is None or callable_obj is default_fn


def _merge_hidden_kwargs(kwargs: dict[str, Any], *, extra_flags: int = 0) -> dict[str, Any]:
    """Merge Windows hidden subprocess kwargs into *kwargs* without mutating the caller's dict."""
    merged = dict(kwargs)
    hidden = hidden_subprocess_kwargs(extra_flags=extra_flags)
    if "creationflags" in hidden:
        existing_flags = int(merged.get("creationflags", 0) or 0)
        merged["creationflags"] = existing_flags | int(hidden["creationflags"])
    if "startupinfo" in hidden and "startupinfo" not in merged:
        merged["startupinfo"] = hidden["startupinfo"]
    return merged


def run_hidden(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    capture_output: bool = True,
    text: bool = True,
    encoding: str | None = "utf-8",
    errors: str | None = "replace",
    check: bool = False,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Execute a background command with ``shell=False`` and hidden Windows console flags."""
    if kwargs.get("shell"):
        raise ValueError("run_hidden strictly enforces shell=False.")

    effective_runner = runner if runner is not None else subprocess.run
    call_kwargs: dict[str, Any] = dict(kwargs)
    call_kwargs["shell"] = False
    call_kwargs["capture_output"] = capture_output
    call_kwargs["text"] = text
    call_kwargs["check"] = check
    if text:
        if encoding is not None:
            call_kwargs["encoding"] = encoding
        if errors is not None:
            call_kwargs["errors"] = errors
    if timeout is not None:
        call_kwargs["timeout"] = timeout
    if cwd is not None:
        call_kwargs["cwd"] = str(cwd)
    if env is not None:
        call_kwargs["env"] = dict(env)

    if _should_apply_hidden_kwargs(runner, subprocess.run):
        call_kwargs = _merge_hidden_kwargs(call_kwargs)

    return effective_runner(list(args), **call_kwargs)


def popen_hidden(
    args: Sequence[str],
    *,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    stdin: Any = subprocess.DEVNULL,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    popen_fn: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> subprocess.Popen[Any]:
    """Spawn a background subprocess with ``shell=False`` and hidden Windows console flags."""
    if kwargs.get("shell"):
        raise ValueError("popen_hidden strictly enforces shell=False.")

    effective_popen = popen_fn if popen_fn is not None else subprocess.Popen
    call_kwargs: dict[str, Any] = dict(kwargs)
    call_kwargs["shell"] = False
    call_kwargs["stdout"] = stdout
    call_kwargs["stderr"] = stderr
    call_kwargs["stdin"] = stdin
    if cwd is not None:
        call_kwargs["cwd"] = str(cwd)
    if env is not None:
        call_kwargs["env"] = dict(env)

    if _should_apply_hidden_kwargs(popen_fn, subprocess.Popen):
        call_kwargs = _merge_hidden_kwargs(call_kwargs)

    return effective_popen(list(args), **call_kwargs)


async def create_subprocess_hidden(
    program: str,
    *args: str,
    stdout: Any = asyncio.subprocess.PIPE,
    stderr: Any = asyncio.subprocess.PIPE,
    stdin: Any = asyncio.subprocess.DEVNULL,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """Async wrapper around ``asyncio.create_subprocess_exec`` with hidden Windows flags."""
    call_kwargs: dict[str, Any] = dict(kwargs)
    call_kwargs["stdout"] = stdout
    call_kwargs["stderr"] = stderr
    call_kwargs["stdin"] = stdin
    if cwd is not None:
        call_kwargs["cwd"] = str(cwd)
    if env is not None:
        call_kwargs["env"] = dict(env)
    if sys.platform == "win32":
        call_kwargs = _merge_hidden_kwargs(call_kwargs)

    return await asyncio.create_subprocess_exec(program, *args, **call_kwargs)


async def run_hidden_async(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> subprocess.CompletedProcess[str]:
    """Run a background command asynchronously without blocking the event loop or flashing."""
    cmd = list(args)
    if not cmd:
        raise ValueError("args must contain at least the executable name.")

    proc = await create_subprocess_hidden(
        cmd[0],
        *cmd[1:],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        env=env,
    )
    try:
        if timeout is not None:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        else:
            raw_out, raw_err = await proc.communicate()
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 0.0) from exc

    stdout_text = (raw_out or b"").decode(encoding, errors=errors)
    stderr_text = (raw_err or b"").decode(encoding, errors=errors)
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=int(proc.returncode or 0),
        stdout=stdout_text,
        stderr=stderr_text,
    )


def run_powershell_hidden(
    command: str,
    *,
    encoded: bool = False,
    timeout: float = 8.0,
    ps_exe: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a background PowerShell command silently with ``CREATE_NO_WINDOW`` and hidden startup."""
    exe = ps_exe or resolve_powershell_exe() or "powershell.exe"
    cmd: list[str] = [
        exe,
        "-NoProfile",
        "-NonInteractive",
    ]
    if encoded:
        encoded_payload = base64.b64encode(command.encode("utf-16le")).decode("ascii")
        cmd.extend(["-EncodedCommand", encoded_payload])
    else:
        cmd.extend(["-Command", command])

    return run_hidden(
        cmd,
        timeout=timeout,
        runner=runner,
        **kwargs,
    )


__all__ = [
    "CREATE_NO_WINDOW",
    "STARTF_USESHOWWINDOW",
    "SW_HIDE",
    "create_subprocess_hidden",
    "hidden_subprocess_kwargs",
    "popen_hidden",
    "resolve_powershell_exe",
    "run_hidden",
    "run_hidden_async",
    "run_powershell_hidden",
]
