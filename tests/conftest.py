"""Shared fixtures for entrypoint.sh tests."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"


@pytest.fixture()
def mock_bin(tmp_path):
    """Create a directory with mock java/psql binaries and add it to PATH.

    Returns a helper to create mock executables that echo canned output.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def _make_mock(name, *, stdout="", stderr="", exit_code=0):
        script = bin_dir / name
        lines = ["#!/usr/bin/env bash"]
        if stdout:
            lines.append(f"printf '%s' {_shell_quote(stdout)}")
        if stderr:
            lines.append(f"printf '%s' {_shell_quote(stderr)} >&2")
        # Consume any stdin so the process doesn't hang
        lines.append("cat > /dev/null")
        lines.append(f"exit {exit_code}")
        script.write_text("\n".join(lines) + "\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    # Default mocks — java exits 0 with JSON output, psql exits 0
    _make_mock("java", stdout="[]")
    _make_mock("psql")

    return bin_dir, _make_mock


def _shell_quote(s):
    """Single-quote a string for safe bash embedding."""
    return "'" + s.replace("'", "'\\''") + "'"


def run_entrypoint(json_input, *, env_override=None, bin_dir=None):
    """Run entrypoint.sh with the given JSON input.

    Returns (stdout, stderr, returncode).
    """
    env = os.environ.copy()
    if bin_dir:
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
    if env_override:
        env.update(env_override)

    result = subprocess.run(
        [str(ENTRYPOINT), json_input],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return result.stdout, result.stderr, result.returncode
