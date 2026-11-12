"""Unit tests for auth helpers and CLI token handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from clea.core import auth
from clea.surfaces.cli.main import _load_token_or_exit


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
