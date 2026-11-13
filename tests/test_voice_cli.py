"""Unit tests for voice CLI commands (voice, voice doctor, voice test)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clea.surfaces.cli.main import build_parser, main
from clea.surfaces.cli.voice import run_voice_doctor, run_voice_service, run_voice_test
from clea.surfaces.voice.lock import VoiceAlreadyRunningError
from clea.surfaces.voice.wake_word import WakeWordModelNotFoundError


def test_voice_cli_parser_subcommands() -> None:
    parser = build_parser()
    args_doctor = parser.parse_args(["voice", "doctor"])
    assert args_doctor.command == "voice"
    assert args_doctor.voice_subcommand == "doctor"
    assert args_doctor.download_models is False

    args_doctor_dl = parser.parse_args(["voice", "doctor", "--download-models"])
    assert args_doctor_dl.download_models is True

    args_test = parser.parse_args(["voice", "test"])
    assert args_test.command == "voice"
    assert args_test.voice_subcommand == "test"

    args_default = parser.parse_args(["voice"])
    assert args_default.command == "voice"
    assert args_default.voice_subcommand is None


def test_voice_doctor_does_not_download_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))

    # Run doctor without --download-models
    run_voice_doctor(download_models=False)
    captured = capsys.readouterr().out

    assert "Clea Voice Doctor" in captured
    assert "Missing" in captured
    assert "clea voice doctor --download-models" in captured
    assert "never downloads models silently" in captured

    # Verify no model files were downloaded
    models_dir = tmp_path / "models"
    assert not (models_dir / "hey_jarvis_v0.1.onnx").exists()


def test_voice_doctor_with_download_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))

    with patch("clea.surfaces.cli.voice.download_wake_word_models") as mock_dl:
        run_voice_doctor(download_models=True)
        assert mock_dl.called

    captured = capsys.readouterr().out
    assert "Downloading missing models" in captured


def test_voice_test_runs_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("clea.surfaces.cli.voice.AudioManager") as mock_audio_cls:
        mock_audio = MagicMock()
        mock_audio_cls.return_value = mock_audio
        run_voice_test()

    captured = capsys.readouterr().out
    assert "Clea Voice Test" in captured
    assert "Testing audio output" in captured


def test_voice_service_catches_already_running(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("clea.surfaces.cli.voice.VoiceRunner") as mock_runner_cls:
        mock_runner = MagicMock()
        mock_runner.run_forever.side_effect = VoiceAlreadyRunningError("already running")
        mock_runner_cls.return_value = mock_runner

        with pytest.raises(SystemExit) as exc:
            run_voice_service()
        assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "already running" in err


def test_voice_service_catches_missing_models(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("clea.surfaces.cli.voice.VoiceRunner") as mock_runner_cls:
        mock_runner_cls.side_effect = WakeWordModelNotFoundError("missing models")

        with pytest.raises(SystemExit) as exc:
            run_voice_service()
        assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "missing models" in err


def test_main_dispatches_voice_subcommands() -> None:
    with patch("clea.surfaces.cli.voice.run_voice_doctor") as mock_doc:
        main(["voice", "doctor", "--download-models"])
        assert mock_doc.called
        assert mock_doc.call_args.kwargs == {"download_models": True}

    with patch("clea.surfaces.cli.voice.run_voice_test") as mock_test:
        main(["voice", "test"])
        assert mock_test.called

    with patch("clea.surfaces.cli.voice.run_voice_service") as mock_srv:
        main(["voice"])
        assert mock_srv.called
