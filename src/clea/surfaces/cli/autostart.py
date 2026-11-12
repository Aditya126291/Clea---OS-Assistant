"""Windows Task Scheduler integration for ``clea autostart``."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "CleaDaemon"
DAEMON_BG_ARGS = "-m clea daemon --background"


@dataclass(slots=True)
class AutostartStatus:
    """Summary returned by ``clea autostart`` commands."""

    installed: bool
    task_name: str
    command: str | None = None
    working_dir: str | None = None
    state: str | None = None
    raw_output: str = ""


def resolve_background_python(executable: str | Path | None = None) -> Path:
    """Return ``pythonw.exe`` in the same environment so the daemon runs with no console."""
    exe = Path(executable or sys.executable).resolve()
    if exe.name.lower() == "python.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
    return exe


def resolve_project_workdir(python_exe: Path | None = None) -> Path:
    """Find the Clea repository/working directory containing ``pyproject.toml``."""
    candidate = (python_exe or Path(sys.executable)).resolve()
    for parent in [candidate, *candidate.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd().resolve()


def current_windows_user(env: dict[str, str] | os._Environ[str] | None = None) -> str:
    """Return ``DOMAIN\\Username`` (or ``Username``) for the active interactive user."""
    source = env if env is not None else os.environ
    username = source.get("USERNAME") or ""
    domain = source.get("USERDOMAIN") or ""
    if domain and username:
        return f"{domain}\\{username}"
    return username


def build_task_xml(*, pythonw_path: Path, working_dir: Path, user_id: str) -> str:
    """Render Task Scheduler 1.2 XML for an interactive, hidden, current-user logon task."""
    esc_user = escape(user_id)
    esc_cmd = escape(str(pythonw_path))
    esc_args = escape(DAEMON_BG_ARGS)
    esc_dir = escape(str(working_dir))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Clea personal AI assistant localhost daemon</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{esc_user}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{esc_user}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>true</Hidden>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <RestartOnFailure>\n"
        "      <Interval>PT1M</Interval>\n"
        "      <Count>3</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{esc_cmd}</Command>\n"
        f"      <Arguments>{esc_args}</Arguments>\n"
        f"      <WorkingDirectory>{esc_dir}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("clea autostart is only supported on Windows.")


def install_autostart(
    *,
    task_name: str = TASK_NAME,
    python_exe: Path | None = None,
    working_dir: Path | None = None,
    user_id: str | None = None,
    runner: object = subprocess.run,
) -> AutostartStatus:
    """Register the current-user Windows Task Scheduler logon task."""
    _require_windows()
    resolved_py = resolve_background_python(python_exe)
    resolved_dir = working_dir or resolve_project_workdir(resolved_py)
    resolved_user = user_id or current_windows_user()
    if not resolved_user:
        raise RuntimeError("Could not determine the current Windows user for Task Scheduler.")

    xml = build_task_xml(
        pythonw_path=resolved_py,
        working_dir=resolved_dir,
        user_id=resolved_user,
    )

    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-16",
        suffix=".xml",
        delete=False,
    )
    xml_path = Path(tmp_file.name)
    try:
        tmp_file.write(xml)
        tmp_file.close()
        proc = runner(  # type: ignore[operator]
            ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        xml_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to register Task Scheduler task '{task_name}': {detail}")

    return AutostartStatus(
        installed=True,
        task_name=task_name,
        command=f'"{resolved_py}" {DAEMON_BG_ARGS}',
        working_dir=str(resolved_dir),
        state="Ready",
        raw_output=(proc.stdout or "").strip(),
    )


def uninstall_autostart(
    *,
    task_name: str = TASK_NAME,
    runner: object = subprocess.run,
) -> bool:
    """Remove the Windows Task Scheduler logon task. Returns True if removed, False if absent."""
    _require_windows()
    proc = runner(  # type: ignore[operator]
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return True
    detail = f"{proc.stdout or ''} {proc.stderr or ''}".strip()
    if "cannot find" in detail.lower() or "does not exist" in detail.lower():
        return False
    raise RuntimeError(f"Failed to remove Task Scheduler task '{task_name}': {detail}")


def status_autostart(
    *,
    task_name: str = TASK_NAME,
    runner: object = subprocess.run,
) -> AutostartStatus:
    """Query Windows Task Scheduler for the current status of ``CleaDaemon``."""
    _require_windows()
    proc = runner(  # type: ignore[operator]
        ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return AutostartStatus(installed=False, task_name=task_name)

    raw = (proc.stdout or "").strip()
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip().lower()] = val.strip()

    return AutostartStatus(
        installed=True,
        task_name=task_name,
        command=fields.get("task to run"),
        working_dir=fields.get("start in"),
        state=fields.get("status"),
        raw_output=raw,
    )
