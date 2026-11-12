"""Unit tests for auth helpers and CLI token handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from clea.core import auth
from clea.core.daemon import build_daemon
from clea.core.protocol import ApprovalRequestedData, ApprovalRequestedEvent
from clea.memory import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk
from clea.surfaces.cli.main import _load_token_or_exit, _prompt_approval, _send_and_stream
from clea.tools import AuditLogger, RiskLevel, ToolDefinition, ToolRegistry


def test_token_roundtrip_and_comparison(tmp_path: Path) -> None:
    path = tmp_path / "session_token"
    token = auth.generate_token()
    auth.write_token(token, path)

    assert auth.read_token(path) == token
    assert auth.token_matches(token, token)
    assert not auth.token_matches(token, "wrong")
    assert not auth.token_matches(token, None)

    header = ", ".join(auth.client_subprotocols(token))
    assert auth.extract_token(header) == token
    assert auth.extract_token("clea.v1") is None

    auth.remove_token(path, "different-token")
    assert path.exists()
    auth.remove_token(path, token)
    assert not path.exists()


@pytest.mark.parametrize("bad_origin", ["*", "http://*", "null", "NULL", "localhost:5173"])
def test_normalize_origins_rejects_unsafe_values(bad_origin: str) -> None:
    with pytest.raises(ValueError):
        auth.normalize_origins([bad_origin])


def test_host_header_is_local() -> None:
    assert auth.host_header_is_local("127.0.0.1:8765")
    assert auth.host_header_is_local("localhost:8765")
    assert auth.host_header_is_local("[::1]:8765")
    assert not auth.host_header_is_local("evil.example:8765")
    assert not auth.host_header_is_local("user@127.0.0.1:8765")
    assert not auth.host_header_is_local("127.0.0.1:8765 ")
    assert not auth.host_header_is_local(None)


def test_cli_exits_clearly_when_token_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "session_token"
    with pytest.raises(SystemExit) as exc:
        _load_token_or_exit(missing)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "clea daemon" in err


async def test_cli_prompt_approval_requires_second_confirmation_for_destructive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    destructive_evt = ApprovalRequestedEvent(
        data=ApprovalRequestedData(
            approval_id="a1",
            tool="wipe",
            arguments={"target": "db"},
            risk_level="destructive",
            timeout_seconds=30,
        )
    )
    assert await _prompt_approval(destructive_evt) == "deny"

    answers_ok = iter(["y", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers_ok))
    assert await _prompt_approval(destructive_evt) == "allow"

    reversible_evt = ApprovalRequestedEvent(
        data=ApprovalRequestedData(
            approval_id="a2",
            tool="rename",
            arguments={"target": "file"},
            risk_level="reversible",
            timeout_seconds=30,
        )
    )
    answers_rev = iter(["y"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers_rev))
    assert await _prompt_approval(reversible_evt) == "allow"


class _CliEchoProvider(LLMProvider):
    provider_name = "cli-fake"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ):
        yield StreamChunk(type="text_delta", text="CLI hello")
        yield StreamChunk(type="done")


async def test_cli_send_and_stream_against_live_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    registry.register(
        name="noop",
        description="Noop",
        risk_level=RiskLevel.READ_ONLY,
        handler=lambda: "ok",
    )
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=_CliEchoProvider(),
        memory=MemoryStore(base_dir=tmp_path),
        registry=registry,
    )
    await daemon.start()
    try:
        prompts = iter(["hi there", "/exit"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(prompts))
        await _send_and_stream(f"ws://127.0.0.1:{daemon.bound_port}", token=daemon.token)
        out = capsys.readouterr().out
        assert "CLI hello" in out
    finally:
        await daemon.stop()
