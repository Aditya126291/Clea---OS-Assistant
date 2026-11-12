"""Ensure TypeScript protocol types stay in sync with src/clea/core/protocol.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_generated_ts_types_have_no_drift() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_ts_types.py"), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"TS protocol drift detected:\n{proc.stdout}\n{proc.stderr}"
