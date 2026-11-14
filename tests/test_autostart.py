"""Tests for Windows Task Scheduler ``clea autostart`` and background logging."""

from __future__ import annotations

import logging
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from clea.core.daemon import configure_daemon_logging, daemon_log_path
from clea.surfaces.cli import autostart
from clea.surfaces.cli.main import main


def test_build_task_xml_uses_interactive_session_pythonw_and_restarts(tmp_path: Path) -> None:
    pythonw = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    xml_text = autostart.build_task_xml(
        pythonw_path=pythonw,
        working_dir=tmp_path,
        user_id=r"HOST\Alice & Bob",
    )

    root = ET.fromstring(xml_text.replace('encoding="UTF-16"', 'encoding="UTF-8"'))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    logon_user = root.find("./t:Triggers/t:LogonTrigger/t:UserId", ns)
    assert logon_user is not None and logon_user.text == r"HOST\Alice & Bob"

    principal = root.find("./t:Principals/t:Principal", ns)
    assert principal is not None
    assert principal.findtext("t:LogonType", namespaces=ns) == "InteractiveToken"
    assert principal.findtext("t:RunLevel", namespaces=ns) == "LeastPrivilege"

    settings = root.find("./t:Settings", ns)
    assert settings is not None
    assert settings.findtext("t:Hidden", namespaces=ns) == "true"
    assert settings.findtext("t:ExecutionTimeLimit", namespaces=ns) == "PT0S"
    assert settings.findtext("t:DisallowStartIfOnBatteries", namespaces=ns) == "false"
    assert settings.findtext("t:StopIfGoingOnBatteries", namespaces=ns) == "false"
    assert settings.findtext("t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    assert settings.findtext("t:RestartOnFailure/t:Count", namespaces=ns) == "3"
    assert settings.findtext("t:RestartOnFailure/t:Interval", namespaces=ns) == "PT1M"

    exec_node = root.find("./t:Actions/t:Exec", ns)
    assert exec_node is not None
    assert exec_node.findtext("t:Command", namespaces=ns) == str(pythonw)
    assert exec_node.findtext("t:Arguments", namespaces=ns) == "-m clea daemon --background"
    assert exec_node.findtext("t:WorkingDirectory", namespaces=ns) == str(tmp_path)


def test_resolve_background_python_prefers_pythonw(tmp_path: Path) -> None:
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    py = scripts / "python.exe"
    pyw = scripts / "pythonw.exe"
    py.write_text("", encoding="utf-8")
    pyw.write_text("", encoding="utf-8")

    assert autostart.resolve_background_python(py) == pyw


def test_autostart_install_status_uninstall_with_mock_schtasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setenv("USERDOMAIN", "LOCALHOST")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='clea'\n", encoding="utf-8")
    pyw = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    pyw.parent.mkdir(parents=True)
    pyw.write_text("", encoding="utf-8")

    recorded: list[list[str]] = []
    captured_xml: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded.append(cmd)
        if "/Create" in cmd:
            xml_idx = cmd.index("/XML") + 1
            captured_xml.append(Path(cmd[xml_idx]).read_text(encoding="utf-16"))
            return subprocess.CompletedProcess(cmd, 0, stdout="SUCCESS: created\n", stderr="")
        if "/Query" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    "TaskName:                             \\CleaDaemon\n"
                    "Status:                               Ready\n"
                    f"Task To Run:                          {pyw} -m clea daemon --background\n"
                    f"Start In:                             {tmp_path}\n"
                ),
                stderr="",
            )
        if "/Delete" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="SUCCESS: deleted\n", stderr="")
        raise AssertionError(f"Unexpected schtasks command: {cmd}")

    installed = autostart.install_autostart(
        python_exe=pyw,
        working_dir=tmp_path,
        runner=fake_run,
    )
    assert installed.installed is True
    assert "-m clea daemon --background" in (installed.command or "")
    assert "InteractiveToken" in captured_xml[0]

    status = autostart.status_autostart(runner=fake_run)
    assert status.installed is True
    assert status.state == "Ready"
    assert status.working_dir == str(tmp_path)

    assert autostart.uninstall_autostart(runner=fake_run) is True
    assert [cmd[1] for cmd in recorded] == ["/Create", "/Query", "/Delete"]


def test_cli_autostart_status_reports_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "clea.surfaces.cli.autostart.status_autostart",
        lambda: autostart.AutostartStatus(installed=False, task_name="CleaDaemon"),
    )
    main(["autostart", "status"])
    assert "not installed" in capsys.readouterr().out


def test_configure_daemon_logging_writes_rotating_log_in_background(tmp_path: Path) -> None:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    try:
        log_file = configure_daemon_logging(background=True, data_dir=tmp_path)
        assert log_file == daemon_log_path(tmp_path)
        logging.getLogger("clea.test").info("background log smoke test")
        for h in root.handlers:
            h.flush()
        assert log_file.is_file()
        assert "background log smoke test" in log_file.read_text(encoding="utf-8")
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
    finally:
        for h in root.handlers:
            h.close()
        root.handlers = old_handlers


def test_python_module_entrypoint_runs_cli() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "clea", "daemon", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "--background" in proc.stdout


def test_build_voice_task_xml_includes_delay_interactive_and_pythonw(tmp_path: Path) -> None:
    pythonw = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    xml_text = autostart.build_task_xml(
        pythonw_path=pythonw,
        working_dir=tmp_path,
        user_id=r"HOST\Alice",
        arguments=autostart.VOICE_BG_ARGS,
        description=autostart.VOICE_DESCRIPTION,
        delay=autostart.VOICE_LOGON_DELAY,
    )

    root = ET.fromstring(xml_text.replace('encoding="UTF-16"', 'encoding="UTF-8"'))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    assert root.findtext("./t:RegistrationInfo/t:Description", namespaces=ns) == (
        autostart.VOICE_DESCRIPTION
    )
    assert root.findtext("./t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns) == r"HOST\Alice"
    assert root.findtext("./t:Triggers/t:LogonTrigger/t:Delay", namespaces=ns) == "PT5S"
    assert root.findtext("./t:Principals/t:Principal/t:LogonType", namespaces=ns) == (
        "InteractiveToken"
    )

    exec_node = root.find("./t:Actions/t:Exec", ns)
    assert exec_node is not None
    assert exec_node.findtext("t:Command", namespaces=ns) == str(pythonw)
    assert exec_node.findtext("t:Arguments", namespaces=ns) == "-m clea.surfaces.cli.main voice"
    assert exec_node.findtext("t:WorkingDirectory", namespaces=ns) == str(tmp_path)


def test_voice_autostart_install_status_uninstall_with_mock_schtasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setenv("USERDOMAIN", "LOCALHOST")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='clea'\n", encoding="utf-8")
    pyw = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    pyw.parent.mkdir(parents=True)
    pyw.write_text("", encoding="utf-8")

    recorded: list[list[str]] = []
    captured_xml: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded.append(cmd)
        if "/Create" in cmd:
            xml_idx = cmd.index("/XML") + 1
            captured_xml.append(Path(cmd[xml_idx]).read_text(encoding="utf-16"))
            return subprocess.CompletedProcess(cmd, 0, stdout="SUCCESS: created\n", stderr="")
        if "/Query" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    "TaskName:                             \\CleaVoice\n"
                    "Status:                               Ready\n"
                    f"Task To Run:                          {pyw} -m clea.surfaces.cli.main voice\n"
                    f"Start In:                             {tmp_path}\n"
                ),
                stderr="",
            )
        if "/Delete" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="SUCCESS: deleted\n", stderr="")
        raise AssertionError(f"Unexpected schtasks command: {cmd}")

    installed = autostart.install_voice_autostart(
        python_exe=pyw,
        working_dir=tmp_path,
        runner=fake_run,
    )
    assert installed.installed is True
    assert installed.task_name == "CleaVoice"
    assert "-m clea.surfaces.cli.main voice" in (installed.command or "")
    assert "InteractiveToken" in captured_xml[0]
    assert "<Delay>PT5S</Delay>" in captured_xml[0]

    status = autostart.status_voice_autostart(runner=fake_run)
    assert status.installed is True
    assert status.task_name == "CleaVoice"
    assert status.state == "Ready"

    assert autostart.uninstall_voice_autostart(runner=fake_run) is True
    assert [cmd[2] for cmd in recorded] == ["/TN", "/TN", "/TN"]
    assert [cmd[3] for cmd in recorded] == ["CleaVoice", "CleaVoice", "CleaVoice"]


def test_cli_autostart_voice_and_all_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "clea.surfaces.cli.autostart.install_autostart",
        lambda: (
            calls.append(("install", "daemon"))
            or autostart.AutostartStatus(installed=True, task_name="CleaDaemon", command="d")
        ),
    )
    monkeypatch.setattr(
        "clea.surfaces.cli.autostart.install_voice_autostart",
        lambda: (
            calls.append(("install", "voice"))
            or autostart.AutostartStatus(installed=True, task_name="CleaVoice", command="v")
        ),
    )
    monkeypatch.setattr(
        "clea.surfaces.cli.autostart.status_autostart",
        lambda: (
            calls.append(("status", "daemon"))
            or autostart.AutostartStatus(installed=True, task_name="CleaDaemon", state="Running")
        ),
    )
    monkeypatch.setattr(
        "clea.surfaces.cli.autostart.status_voice_autostart",
        lambda: (
            calls.append(("status", "voice"))
            or autostart.AutostartStatus(installed=True, task_name="CleaVoice", state="Ready")
        ),
    )
    monkeypatch.setattr(
        "clea.surfaces.cli.autostart.uninstall_voice_autostart",
        lambda: calls.append(("uninstall", "voice")) or True,
    )

    main(["autostart", "install", "--target", "voice"])
    main(["autostart", "status", "--target", "all"])
    main(["autostart", "uninstall", "--target", "voice"])

    out = capsys.readouterr().out
    assert "Installed Windows logon task 'CleaVoice'." in out
    assert "Task 'CleaDaemon': installed (Running)" in out
    assert "Task 'CleaVoice': installed (Ready)" in out
    assert "Removed Windows logon task 'CleaVoice'." in out
    assert calls == [
        ("install", "voice"),
        ("status", "daemon"),
        ("status", "voice"),
        ("uninstall", "voice"),
    ]
