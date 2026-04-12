import base64
import http.server
import json
import shlex
import socketserver
import stat
import threading
from pathlib import Path

import pytest

from conftest import run_entrypoint


class _CSVHandler(http.server.BaseHTTPRequestHandler):
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
        if server.send_content_length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args, **kwargs):
        return


class _CSVServer(socketserver.TCPServer):
    allow_reuse_address = True


@pytest.fixture()
def csv_server():
    server = _CSVServer(("127.0.0.1", 0), _CSVHandler)
    server.body_bytes = b""
    server.force_status = None
    server.send_content_length = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def set_body(body: bytes, status: int = 200, send_content_length: bool = True):
        server.body_bytes = body
        server.force_status = status
        server.send_content_length = send_content_length

    url_for = lambda path="/import.csv": f"http://127.0.0.1:{port}{path}"
    yield set_body, url_for
    server.shutdown()
    server.server_close()


def _install_logging_java(
    bin_dir: Path, *, per_row_exit_codes=None, stderr_per_row=None,
    stdout_per_row=None, sleep_secs: float = 0.0,
):
    """Mock java that logs each invocation's argv as a JSON line, with per-call
    exit codes / stderr / stdout, and an optional fixed sleep."""
    log_path = bin_dir / "java_calls.log"
    counter_path = bin_dir / "java_counter"
    counter_path.write_text("0")
    exits = per_row_exit_codes or []
    stderrs = stderr_per_row or []
    stdouts = stdout_per_row or []

    exits_bash = " ".join(str(c) for c in exits) or "0"
    stderrs_bash = " ".join(shlex.quote(s) for s in stderrs) or "''"
    stdouts_bash = " ".join(shlex.quote(s) for s in stdouts) or "''"

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
stdouts=({stdouts_bash})

idx=$((i-1))
if [ $idx -lt ${{#exits[@]}} ]; then
  ec=${{exits[$idx]}}
else
  ec=0
fi
if [ $idx -lt ${{#stderrs[@]}} ]; then
  msg=${{stderrs[$idx]}}
  [ -n "$msg" ] && printf '%s' "$msg" >&2
fi
if [ $idx -lt ${{#stdouts[@]}} ]; then
  out=${{stdouts[$idx]}}
  if [ -n "$out" ]; then
    printf '%s' "$out"
  elif [ "$ec" = "0" ]; then
    printf '%s' '{{"ok":true}}'
  fi
else
  [ "$ec" = "0" ] && printf '%s' '{{"ok":true}}'
fi

if [ "{sleep_secs}" != "0.0" ]; then
  sleep {sleep_secs}
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


def _find_summary(stdout):
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and '"total"' in line:
            return json.loads(line)
    raise AssertionError(f"no bulk summary JSON in stdout: {stdout!r}")


def _bin(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


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


class TestBulkCreateValidation:
    def test_missing_csv_url(self):
        stdout, _, rc = run_entrypoint(json.dumps(_base_payload()))
        out = json.loads(stdout)
        assert rc == 1
        assert "csv_url" in out["error"]

    def test_action_is_allowed(self, tmp_path, csv_server):
        bin_dir = _bin(tmp_path)
        _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(b"name,email,company,api_key,notes\nAlice,a@x.y,Acme,key1,n\n")

        _, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 0


class TestBulkCreateDownload:
    def test_404_fails_without_leaking_url(self, tmp_path, csv_server):
        bin_dir = _bin(tmp_path)
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(b"", status=404)

        url = url_for("/secret-token-abc123")
        stdout, stderr, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url)),
            bin_dir=bin_dir,
        )
        out = json.loads(stdout)
        assert rc == 1
        assert "secret-token-abc123" not in out["error"]
        assert "secret-token-abc123" not in stderr
        assert out["error"] == "Failed to download CSV: HTTP 404"
        assert _read_java_calls(log_path) == []

    def test_download_error_logs_exception_type(self, tmp_path):
        """Issue #6: Operators need exception class + message in stderr."""
        bin_dir = _bin(tmp_path)
        _install_logging_java(bin_dir)
        # Port 1 is privileged; connection should be refused.
        stdout, stderr, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url="http://127.0.0.1:1/nope")),
            bin_dir=bin_dir,
        )
        assert rc == 1
        assert "URLError" in stderr or "ConnectionRefused" in stderr or "refused" in stderr.lower()
        # URL must not appear in stderr
        assert "127.0.0.1:1" not in stderr or "nope" not in stderr


class TestBulkCreateHappyPath:
    def test_three_rows_three_jar_calls(self, tmp_path, csv_server):
        bin_dir = _bin(tmp_path)
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(HAPPY_CSV)

        _, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 0

        calls = _read_java_calls(log_path)
        assert len(calls) == 3

        keys = [_find_flag(c, "-k") for c in calls]
        assert keys == ["key_a", "key_b", "key_c"]

        first = calls[0]
        assert "create" in first
        assert _find_flag(first, "-n") == "Alice"
        assert _find_flag(first, "-e") == "alice@example.com"
        assert _find_flag(first, "-o") == "Acme"
        assert _find_flag(first, "-d") == "note1"

        third = calls[2]
        assert _find_flag(third, "-e") is None
        assert _find_flag(third, "-d") is None

    def test_summary_json_on_stdout(self, tmp_path, csv_server):
        bin_dir = _bin(tmp_path)
        _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(HAPPY_CSV)

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 0
        summary = _find_summary(stdout)
        assert summary == {"total": 3, "succeeded": 3, "failed": 0, "errors": []}


class TestBulkCreatePartialFailure:
    def test_one_row_fails_overall_exit_still_zero(self, tmp_path, csv_server):
        bin_dir = _bin(tmp_path)
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
        assert len(_read_java_calls(log_path)) == 3

        summary = _find_summary(stdout)
        assert summary["total"] == 3
        assert summary["succeeded"] == 2
        assert summary["failed"] == 1
        assert len(summary["errors"]) == 1
        assert summary["errors"][0]["row"] == 2
        assert "duplicate" in summary["errors"][0]["error"].lower()

    def test_all_rows_fail_exit_nonzero(self, tmp_path, csv_server):
        """Issue #1: 100%-failed bulk must not exit 0."""
        bin_dir = _bin(tmp_path)
        _install_logging_java(
            bin_dir,
            per_row_exit_codes=[1, 1, 1],
            stderr_per_row=["boom", "boom", "boom"],
        )
        set_body, url_for = csv_server
        set_body(HAPPY_CSV)

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc != 0
        summary = _find_summary(stdout)
        assert summary["total"] == 3
        assert summary["succeeded"] == 0
        assert summary["failed"] == 3

    def test_row_stderr_empty_includes_stdout(self, tmp_path, csv_server):
        """Issue #12b: when JAR stderr is empty, include stdout tail for ops."""
        bin_dir = _bin(tmp_path)
        _install_logging_java(
            bin_dir,
            per_row_exit_codes=[1],
            stderr_per_row=[""],
            stdout_per_row=["FATAL: schema mismatch"],
        )
        set_body, url_for = csv_server
        set_body(
            b"name,email,company,api_key,notes\n"
            b"A,a@x.y,X,key1,\n"
        )

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        summary = _find_summary(stdout)
        assert summary["failed"] == 1
        err = summary["errors"][0]["error"]
        assert "FATAL" in err or "schema mismatch" in err


class TestBulkCreateOversize:
    def test_oversize_via_content_length(self, tmp_path, csv_server):
        bin_dir = _bin(tmp_path)
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        header = b"name,email,company,api_key,notes\n"
        row = b"a,b,c,key,notes\n"
        body = header + (row * ((11 * 1024 * 1024) // len(row) + 1))
        set_body(body)

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 1
        out = json.loads(stdout)
        assert "exceeds maximum size" in out["error"]
        assert _read_java_calls(log_path) == []

    def test_oversize_via_streaming_no_content_length(self, tmp_path, csv_server):
        """Issue #9: cap must trip when server omits Content-Length."""
        bin_dir = _bin(tmp_path)
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        header = b"name,email,company,api_key,notes\n"
        row = b"a,b,c,key,notes\n"
        body = header + (row * ((11 * 1024 * 1024) // len(row) + 1))
        set_body(body, send_content_length=False)

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 1
        out = json.loads(stdout)
        assert "exceeds maximum size" in out["error"]
        assert _read_java_calls(log_path) == []


class TestBulkCreateBadCsv:
    def test_non_utf8_bytes(self, tmp_path, csv_server):
        """Issue #2: non-UTF-8 CSV must surface as ValidationError, not a crash."""
        bin_dir = _bin(tmp_path)
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(
            b"name,email,company,api_key,notes\n"
            b"\xff\xfe\xfd,a@x.y,X,key1,\n"
        )

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 1
        out = json.loads(stdout)
        assert "CSV" in out["error"] or "csv" in out["error"]
        # No uncaught traceback
        assert "success" in out
        assert _read_java_calls(log_path) == []

    def test_missing_api_key_column(self, tmp_path, csv_server):
        """Every row records 'missing api_key' when the column is absent."""
        bin_dir = _bin(tmp_path)
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(
            b"name,email,company,notes\n"
            b"A,a@x.y,X,n\n"
            b"B,b@x.y,Y,n\n"
        )

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc != 0
        summary = _find_summary(stdout)
        assert summary["total"] == 2
        assert summary["succeeded"] == 0
        assert summary["failed"] == 2
        assert all("missing api_key" in e["error"] for e in summary["errors"])
        assert _read_java_calls(log_path) == []

    def test_header_only_empty_csv(self, tmp_path, csv_server):
        bin_dir = _bin(tmp_path)
        log_path = _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(b"name,email,company,api_key,notes\n")

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(csv_url=url_for())),
            bin_dir=bin_dir,
        )
        assert rc == 0
        summary = _find_summary(stdout)
        assert summary == {"total": 0, "succeeded": 0, "failed": 0, "errors": []}
        assert _read_java_calls(log_path) == []


class TestBulkCreateResultTable:
    """Issue #8: exercise the correlation_id + result_table path for bulk_create."""

    def test_summary_written_to_result_table(self, tmp_path, csv_server):
        bin_dir = _bin(tmp_path)
        # Capture psql stdin by writing a psql mock that logs to a file
        psql_log = bin_dir / "psql_sql.log"
        psql = bin_dir / "psql"
        psql.write_text(
            "#!/usr/bin/env bash\n"
            "while [[ $# -gt 0 ]]; do\n"
            f"  case $1 in -c) echo \"$2\" >> '{psql_log}'; shift 2 ;; *) shift ;; esac\n"
            "done\n"
            f"cat >> '{psql_log}'\n"
            "exit 0\n"
        )
        psql.chmod(psql.stat().st_mode | stat.S_IEXEC)
        _install_logging_java(bin_dir)

        set_body, url_for = csv_server
        set_body(HAPPY_CSV)

        _, _, rc = run_entrypoint(
            json.dumps(_base_payload(
                csv_url=url_for(),
                correlation_id="550e8400-e29b-41d4-a716-446655440000",
                result_table="api_key_results",
            )),
            bin_dir=bin_dir,
        )
        assert rc == 0
        sql = psql_log.read_text()
        assert "INSERT INTO" in sql
        assert "'succeeded'" in sql
        # Summary JSON must be embedded in the dollar-quoted literal
        assert '"total": 3' in sql
        assert '"succeeded": 3' in sql


class TestBulkCreatePerRowTimeout:
    def test_hanging_row_killed(self, tmp_path, csv_server):
        """Issue #12: a slow JAR must not hang the whole job."""
        bin_dir = _bin(tmp_path)
        # 2 rows: first sleeps > per-row timeout, second succeeds
        _install_logging_java(bin_dir, sleep_secs=5.0)
        set_body, url_for = csv_server
        set_body(
            b"name,email,company,api_key,notes\n"
            b"A,a@x.y,X,key1,\n"
        )

        stdout, _, rc = run_entrypoint(
            json.dumps(_base_payload(
                csv_url=url_for(),
                jar_timeout_secs=1,
            )),
            bin_dir=bin_dir,
        )
        # Must not hang, must record a timeout
        summary = _find_summary(stdout)
        assert summary["failed"] == 1
        assert "timeout" in summary["errors"][0]["error"].lower()


class TestBulkCreateBase64Wrapping:
    def test_wrapped_base64_accepted(self, tmp_path, csv_server):
        """Issue #10: GNU/BSD `base64` wraps at col 76 by default."""
        bin_dir = _bin(tmp_path)
        _install_logging_java(bin_dir)
        set_body, url_for = csv_server
        set_body(b"name,email,company,api_key,notes\nAlice,a@x.y,Acme,key1,n\n")

        payload = json.dumps(_base_payload(csv_url=url_for()))
        # Inject CR/LF every 10 chars to simulate wrapping
        encoded = base64.b64encode(payload.encode()).decode()
        wrapped = "\n".join(encoded[i:i+10] for i in range(0, len(encoded), 10))

        _, _, rc = run_entrypoint(wrapped, bin_dir=bin_dir)
        assert rc == 0
