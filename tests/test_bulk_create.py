"""Unit tests for the bulk_create action.

Covers CSV download, row iteration, JAR invocation, and summary aggregation.
Uses a localhost HTTP server for CSV serving and a java mock on PATH that logs
its invocations so we can assert each row became a separate `create` call.
"""

import http.server
import json
import shlex
import socketserver
import stat
import threading
from pathlib import Path

import pytest

from conftest import run_entrypoint


# --- helpers ---------------------------------------------------------------

class _CSVHandler(http.server.BaseHTTPRequestHandler):
    """Serves whatever content the parent server was initialized with."""

    def do_GET(self):
        server = self.server
        if server.force_status and server.force_status != 200:
            self.send_response(server.force_status)
            self.end_headers()
            self.wfile.write(b"nope")
            return
        body = server.body_bytes
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        return  # silence


class _CSVServer(socketserver.TCPServer):
    allow_reuse_address = True


@pytest.fixture()
def csv_server():
    """Spin up a localhost HTTP server serving a configurable CSV body."""
    server = _CSVServer(("127.0.0.1", 0), _CSVHandler)
    server.body_bytes = b""
    server.force_status = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def set_body(body: bytes, status: int = 200):
        server.body_bytes = body
        server.force_status = status

    url_for = lambda path="/import.csv": f"http://127.0.0.1:{port}{path}"

    yield set_body, url_for
    server.shutdown()
    server.server_close()


def _install_logging_java(bin_dir: Path, *, per_row_exit_codes=None, stderr_per_row=None):
    """Mock java that logs each invocation's argv as a JSON line and can exit
    with per-call exit codes and per-call stderr strings."""
    log_path = bin_dir / "java_calls.log"
    counter_path = bin_dir / "java_counter"
    counter_path.write_text("0")
    exits = per_row_exit_codes or []
    stderrs = stderr_per_row or []

    exits_bash = " ".join(str(c) for c in exits) or "0"
    stderrs_bash = " ".join(shlex.quote(s) for s in stderrs) or "''"

    script = f"""#!/usr/bin/env bash
set -u
counter_file={shlex.quote(str(counter_path))}
log_file={shlex.quote(str(log_path))}
i=$(cat "$counter_file")
i=$((i+1))
echo "$i" > "$counter_file"

python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$log_file"

exits=({exits_bash})
stderrs=({stderrs_bash})

idx=$((i-1))
if [ $idx -lt ${{#exits[@]}} ]; then
  ec=${{exits[$idx]}}
else
  ec=0
fi
if [ $idx -lt ${{#stderrs[@]}} ]; then
  msg=${{stderrs[$idx]}}
  if [ -n "$msg" ]; then
    printf '%s' "$msg" >&2
  fi
fi

if [ "$ec" = "0" ]; then
  printf '%s' '{{"ok":true}}'
fi
exit $ec
"""
    java = bin_dir / "java"
    java.write_text(script)
    java.chmod(java.stat().st_mode | stat.S_IEXEC)
    psql = bin_dir / "psql"
    if not psql.exists():
        psql.write_text("#!/usr/bin/env bash\ncat > /dev/null\nexit 0\n")
        psql.chmod(psql.stat().st_mode | stat.S_IEXEC)
    return log_path


def _read_java_calls(log_path: Path):
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def _base_payload(**overrides):
    data = {
        "action": "bulk_create",
        "db_url": "jdbc:postgresql://h:5432/d",
        "db_user": "u",
        "db_pass": "p",
    }
    data.update(overrides)
    return data


# --- validation ------------------------------------------------------------

class TestBulkCreateValidation:
    def test_missing_csv_url(self):
        stdout, _, rc = run_entrypoint(json.dumps(_base_payload()))
        out = json.loads(stdout)
        assert rc == 1
        assert "csv_url" in out["error"]

    def test_action_is_allowed(self, tmp_path, csv_server):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(b"name,email,company,api_key,notes\nAlice,a@x.y,Acme,key1,n\n")

        _, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 0


# --- download --------------------------------------------------------------

class TestBulkCreateDownload:
    def test_404_fails_without_leaking_url(self, tmp_path, csv_server):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(b"", status=404)

        url = url_for("/secret-token-abc123")
        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url)),
            bin_dir=bin_dir,
        )
        out = json.loads(stdout)
        assert rc == 1
        assert "secret-token-abc123" not in out["error"]
        assert "download" in out["error"].lower() or "csv" in out["error"].lower()
        # JAR should not have been invoked
        assert _read_java_calls(log_path) == []


# --- row iteration ---------------------------------------------------------

HAPPY_CSV = (
    b"name,email,company,api_key,notes\n"
    b"Alice,alice@example.com,Acme,key_a,note1\n"
    b"Bob,bob@example.com,Bobco,key_b,note2\n"
    b"Carol,,Carolinc,key_c,\n"
)


def _find_flag(argv, flag):
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


class TestBulkCreateHappyPath:
    def test_three_rows_three_jar_calls(self, tmp_path, csv_server):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(HAPPY_CSV)

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 0

        calls = _read_java_calls(log_path)
        assert len(calls) == 3

        keys = [_find_flag(c, "-k") for c in calls]
        assert keys == ["key_a", "key_b", "key_c"]

        # row 1 has all fields
        first = calls[0]
        assert _find_flag(first, "-n") == "Alice"
        assert _find_flag(first, "-e") == "alice@example.com"
        assert _find_flag(first, "-o") == "Acme"
        assert _find_flag(first, "-d") == "note1"
        # create action present
        assert "create" in first

        # row 3 has blank email and notes — those flags should be omitted
        third = calls[2]
        assert _find_flag(third, "-e") is None
        assert _find_flag(third, "-d") is None

    def test_summary_json_on_stdout(self, tmp_path, csv_server):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(HAPPY_CSV)

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 0
        # Find JSON object in stdout (may have other lines)
        obj = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    break
                except ValueError:
                    continue
        assert obj is not None, f"no JSON summary in stdout: {stdout!r}"
        assert obj["total"] == 3
        assert obj["succeeded"] == 3
        assert obj["failed"] == 0
        assert obj["errors"] == []


class TestBulkCreatePartialFailure:
    def test_one_row_fails_overall_exit_still_zero(self, tmp_path, csv_server):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_path = _install_logging_java(
            bin_dir,
            per_row_exit_codes=[0, 1, 0],
            stderr_per_row=["", "duplicate key", ""],
        )
        set_body, url_for = csv_server
        set_body(HAPPY_CSV)

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 0
        calls = _read_java_calls(log_path)
        assert len(calls) == 3

        obj = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    break
                except ValueError:
                    continue
        assert obj is not None
        assert obj["total"] == 3
        assert obj["succeeded"] == 2
        assert obj["failed"] == 1
        assert len(obj["errors"]) == 1
        assert obj["errors"][0]["row"] == 2
        assert "duplicate" in obj["errors"][0]["error"].lower()


class TestBulkCreateOversize:
    def test_oversize_csv_rejected(self, tmp_path, csv_server):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        # 11 MB body — over the 10 MB cap
        header = b"name,email,company,api_key,notes\n"
        row = b"a,b,c,key,notes\n"  # 16 bytes
        n_rows = (11 * 1024 * 1024) // len(row) + 1
        body = header + (row * n_rows)
        assert len(body) > 10 * 1024 * 1024
        set_body(body)

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 1
        out = json.loads(stdout)
        assert "csv" in out["error"].lower() or "size" in out["error"].lower()
        # JAR must not have been invoked
        assert _read_java_calls(log_path) == []
