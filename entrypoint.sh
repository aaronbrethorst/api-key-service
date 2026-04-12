#!/usr/bin/env python3
"""Filename is .sh (not .py) because the Dockerfile ENTRYPOINT and Render
startCommand depend on it; the shebang drives execution."""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Optional
from xml.sax.saxutils import quoteattr

CSV_MAX_BYTES = 10 * 1024 * 1024
CSV_TIMEOUT_SECS = 60
DEFAULT_ROW_JAR_TIMEOUT_SECS = 300

VALID_ACTIONS = {"create", "list", "get", "update", "delete", "bulk_create"}
REQUIRED_FIELDS = ("action", "db_url", "db_user", "db_pass")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TABLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

JAR_PATH = "/app/api-key-cli.jar"
PG_DRIVER_PATH = "/app/postgresql.jar"
MAIN_CLASS = "org.onebusaway.cli.apikey.ApiKeyCliMain"


class ValidationError(Exception):
    pass


def _is_json_object(text: str) -> Optional[dict]:
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_input(raw: str) -> dict:
    obj = _is_json_object(raw)
    if obj is not None:
        return obj
    cleaned = "".join(raw.split())
    try:
        decoded = base64.b64decode(cleaned, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ValidationError(
            "Invalid JSON input: not a JSON object and not valid base64-encoded JSON"
        )
    obj = _is_json_object(decoded)
    if obj is None:
        raise ValidationError(
            "Invalid JSON input: base64 decoded successfully but payload is not a JSON object"
        )
    return obj


def validate(payload: dict) -> dict:
    for fname in REQUIRED_FIELDS:
        if not payload.get(fname):
            raise ValidationError(f"Missing required field: {fname}")

    action = payload["action"]
    if action not in VALID_ACTIONS:
        raise ValidationError(
            f"Invalid action: {action}. Must be one of: "
            + ", ".join(sorted(VALID_ACTIONS))
        )

    correlation_id = payload.get("correlation_id") or ""
    result_table = payload.get("result_table") or ""

    if result_table and not TABLE_NAME_RE.match(result_table):
        raise ValidationError(f"Invalid result_table name: {result_table}")
    if correlation_id and not UUID_RE.match(correlation_id):
        raise ValidationError(f"Invalid correlation_id: {correlation_id}")
    if correlation_id and not result_table:
        raise ValidationError("correlation_id provided without result_table")
    if result_table and not correlation_id:
        raise ValidationError("result_table provided without correlation_id")

    if action == "bulk_create" and not payload.get("csv_url"):
        raise ValidationError("bulk_create requires csv_url")

    return payload


@dataclass(frozen=True)
class PgConnInfo:
    host: str
    port: str
    dbname: str
    sslmode: str


def parse_jdbc_url(db_url: str) -> PgConnInfo:
    if not db_url.startswith("jdbc:postgresql://"):
        raise ValidationError(f"Failed to parse JDBC URL for psql connection: {db_url}")
    parsed = urllib.parse.urlparse(db_url[len("jdbc:"):])
    host = parsed.hostname or ""
    dbname = parsed.path.lstrip("/")
    if not host or not dbname:
        raise ValidationError(f"Failed to parse JDBC URL for psql connection: {db_url}")
    try:
        port = str(parsed.port) if parsed.port is not None else "5432"
    except ValueError:
        raise ValidationError(f"Failed to extract valid port from JDBC URL: {db_url}")
    sslmode = urllib.parse.parse_qs(parsed.query).get("sslmode", ["require"])[0]
    return PgConnInfo(host=host, port=port, dbname=dbname, sslmode=sslmode)


DATA_SOURCES_XML_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<beans xmlns="http://www.springframework.org/schema/beans"
       xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
       xsi:schemaLocation="http://www.springframework.org/schema/beans
         http://www.springframework.org/schema/beans/spring-beans.xsd">
  <bean id="dataSource" class="org.springframework.jdbc.datasource.DriverManagerDataSource">
    <property name="driverClassName" value="org.postgresql.Driver"/>
    <property name="url" value={url}/>
    <property name="username" value={user}/>
    <property name="password" value={pw}/>
  </bean>
</beans>
"""


def write_data_sources_xml(tmpdir: str, db_url: str, db_user: str, db_pass: str) -> str:
    path = os.path.join(tmpdir, "data-sources.xml")
    body = DATA_SOURCES_XML_TMPL.format(
        url=quoteattr(db_url),
        user=quoteattr(db_user),
        pw=quoteattr(db_pass),
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o600)
    return path


@dataclass(frozen=True)
class PgClient:
    """All user-controlled SQL is dollar-quoted with a random tag to prevent
    body breakout."""

    host: str
    port: str
    dbname: str
    sslmode: str
    user: str
    password: str

    @classmethod
    def from_jdbc(cls, db_url: str, user: str, password: str) -> "PgClient":
        info = parse_jdbc_url(db_url)
        return cls(
            host=info.host, port=info.port, dbname=info.dbname, sslmode=info.sslmode,
            user=user, password=password,
        )

    @property
    def _conn_str(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}"
            f"/{self.dbname}?sslmode={self.sslmode}"
        )

    def _run(self, *args: str, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["psql", self._conn_str, "-q", *args],
            input=stdin, text=True, capture_output=True, check=False,
        )

    def ensure_result_table(self, result_table: str) -> tuple:
        ddl = f"""    CREATE TABLE IF NOT EXISTS "{result_table}" (
      id              BIGSERIAL PRIMARY KEY,
      correlation_id  UUID NOT NULL UNIQUE,
      status          VARCHAR(20) NOT NULL DEFAULT 'succeeded',
      result_data     JSONB,
      error_message   TEXT,
      created_at      TIMESTAMP NOT NULL DEFAULT NOW()
    );
"""
        r = self._run(stdin=ddl)
        return (r.returncode == 0, r.stderr)

    def purge_old_rows(self, result_table: str) -> tuple:
        r = self._run(
            "-c",
            f'DELETE FROM "{result_table}" WHERE created_at < NOW() - INTERVAL \'24 hours\';',
        )
        return (r.returncode == 0, r.stderr)

    def write_result(self, result_table: str, correlation_id: str,
                     status: str, result_data: str, error_message: str) -> tuple:
        rd_sql = "NULL"
        em = error_message
        if result_data:
            try:
                json.loads(result_data)
                rd_sql = _dollar_quote(result_data, "dq") + "::jsonb"
            except ValueError:
                if not em:
                    em = f"Non-JSON output: {result_data}"
        em_sql = _dollar_quote(em, "em") if em else "NULL"

        sql = (
            f'    INSERT INTO "{result_table}" (correlation_id, status, result_data, error_message)\n'
            f"    VALUES ('{correlation_id}', '{status}', {rd_sql}, {em_sql})\n"
            f"    ON CONFLICT (correlation_id) DO UPDATE SET\n"
            f"      status = EXCLUDED.status,\n"
            f"      result_data = EXCLUDED.result_data,\n"
            f"      error_message = EXCLUDED.error_message;\n"
        )
        r = self._run(stdin=sql)
        return (r.returncode == 0, r.stderr)


def _dollar_quote(body: str, prefix: str) -> str:
    tag = prefix + os.urandom(6).hex()
    return f"${tag}${body}${tag}$"


def _java_opts() -> list:
    return shlex.split(os.environ.get("JAVA_OPTS", ""))


def run_jar(args: list, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    cmd = [
        "java", *_java_opts(),
        "-cp", f"{JAR_PATH}:{PG_DRIVER_PATH}",
        MAIN_CLASS,
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)


def download_csv(url: str, dest_path: str) -> None:
    """Never includes `url` in raised error messages — signed URLs may carry
    credentials."""
    req = urllib.request.Request(url, headers={"User-Agent": "api-key-service"})
    try:
        resp = urllib.request.urlopen(req, timeout=CSV_TIMEOUT_SECS)
    except urllib.error.HTTPError as e:
        print(f"CSV download failed: HTTPError {e.code}", file=sys.stderr)
        raise ValidationError(f"Failed to download CSV: HTTP {e.code}")
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        # Log the exception class + message to stderr (not the URL) so
        # operators can distinguish DNS, TLS, connection-refused, etc.
        print(f"CSV download failed: {type(e).__name__}: {e}", file=sys.stderr)
        raise ValidationError("Failed to download CSV")

    with resp:
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > CSV_MAX_BYTES:
            raise ValidationError(f"CSV exceeds maximum size of {CSV_MAX_BYTES} bytes")
        total = 0
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > CSV_MAX_BYTES:
                    raise ValidationError(
                        f"CSV exceeds maximum size of {CSV_MAX_BYTES} bytes"
                    )
                f.write(chunk)
    os.chmod(dest_path, 0o600)


SINGLE_ACTION_FIELDS = (
    ("key", "-k"),
    ("name", "-n"),
    ("email", "-e"),
    ("company", "-o"),
    ("details", "-d"),
    ("minApiReqInt", "-m"),
)

BULK_ROW_FIELDS = (
    ("name", "-n"),
    ("email", "-e"),
    ("company", "-o"),
    ("notes", "-d"),
)


def build_args(action: str, source: dict, fields, ds_xml_path: str,
               initial: tuple = ()) -> list:
    args = [action, *initial]
    for fname, flag in fields:
        val = (source.get(fname) or "").strip()
        if val:
            args += [flag, val]
    args += ["-c", ds_xml_path, "-j"]
    return args


@dataclass
class BulkSummary:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list = field(default_factory=list)


def _row_error(proc: subprocess.CompletedProcess) -> str:
    stderr = (proc.stderr or "").strip()
    if stderr:
        return stderr[:500]
    stdout = (proc.stdout or "").strip()
    if stdout:
        return f"JAR exited with code {proc.returncode}. stdout tail: {stdout[-500:]}"
    return f"JAR exited with code {proc.returncode}"


def run_bulk_create(csv_path: str, ds_xml_path: str,
                    row_timeout_secs: float) -> BulkSummary:
    summary = BulkSummary()
    try:
        fh = open(csv_path, "r", encoding="utf-8-sig", newline="")
    except (UnicodeDecodeError, OSError) as e:
        raise ValidationError(f"Failed to read CSV: {type(e).__name__}")
    with fh as f:
        try:
            reader = csv.DictReader(f)
            rows = enumerate(reader, start=1)
            while True:
                try:
                    row_num, row = next(rows)
                except StopIteration:
                    break
                except (csv.Error, UnicodeDecodeError) as e:
                    raise ValidationError(f"Malformed CSV: {type(e).__name__}: {e}")

                summary.total += 1
                api_key = (row.get("api_key") or "").strip()
                if not api_key:
                    summary.failed += 1
                    summary.errors.append({"row": row_num, "error": "missing api_key"})
                    continue
                args = build_args("create", row, BULK_ROW_FIELDS, ds_xml_path,
                                  initial=("-k", api_key))
                try:
                    proc = run_jar(args, timeout=row_timeout_secs)
                except subprocess.TimeoutExpired:
                    summary.failed += 1
                    summary.errors.append({
                        "row": row_num,
                        "key": api_key,
                        "error": f"JAR timeout after {row_timeout_secs}s",
                    })
                    continue
                if proc.returncode == 0:
                    summary.succeeded += 1
                else:
                    summary.failed += 1
                    summary.errors.append({
                        "row": row_num,
                        "key": api_key,
                        "error": _row_error(proc),
                    })
        except ValidationError:
            raise
    return summary


def _emit_error_json(msg: str) -> None:
    print(json.dumps({"success": False, "error": msg}))


_JSON_LINE_START = re.compile(r"^\s*[\[{]")


def extract_jar_json(output: str) -> str:
    lines = output.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _JSON_LINE_START.match(line):
            return "".join(lines[i:])
    return output


def _record_result(pg: Optional[PgClient], result_table: str, correlation_id: str,
                   status: str, result_data: str, error_message: str) -> None:
    if pg is None or not result_table or not correlation_id:
        return
    ok, stderr = pg.ensure_result_table(result_table)
    if not ok:
        print(
            f"ERROR: Failed to create/verify result table '{result_table}': {stderr.strip()}",
            file=sys.stderr,
        )
        return
    purge_ok, purge_stderr = pg.purge_old_rows(result_table)
    if not purge_ok:
        print(
            f"WARNING: Orphan row cleanup failed for '{result_table}': {purge_stderr.strip()}",
            file=sys.stderr,
        )
    write_ok, write_stderr = pg.write_result(
        result_table, correlation_id, status, result_data, error_message,
    )
    if not write_ok:
        print(
            f"ERROR: Failed to write result to {result_table} "
            f"for correlation_id={correlation_id}: {write_stderr.strip()}",
            file=sys.stderr,
        )


def main(argv: list) -> int:
    if len(argv) < 2:
        _emit_error_json("Usage: entrypoint.sh '<json_blob>' | <base64-encoded-json>")
        return 1

    tmpdir = None
    pg: Optional[PgClient] = None
    correlation_id = ""
    result_table = ""
    try:
        payload = parse_input(argv[1])

        # Best-effort PgClient construction up front so that a ValidationError
        # raised by validate() can still be recorded against the caller's
        # correlation_id. Silently leaves pg=None if the payload is too
        # malformed to build a client.
        correlation_id = (payload.get("correlation_id") or "") if isinstance(payload, dict) else ""
        result_table = (payload.get("result_table") or "") if isinstance(payload, dict) else ""
        if (correlation_id and result_table
                and UUID_RE.match(correlation_id)
                and TABLE_NAME_RE.match(result_table)
                and payload.get("db_url") and payload.get("db_user") and payload.get("db_pass")):
            try:
                pg = PgClient.from_jdbc(
                    payload["db_url"], payload["db_user"], payload["db_pass"],
                )
            except ValidationError:
                pg = None

        validate(payload)

        db_url = payload["db_url"]
        db_user = payload["db_user"]
        db_pass = payload["db_pass"]

        tmpdir = tempfile.mkdtemp()
        os.chmod(tmpdir, 0o700)
        ds_xml = write_data_sources_xml(tmpdir, db_url, db_user, db_pass)

        if payload["action"] == "bulk_create":
            csv_path = os.path.join(tmpdir, "import.csv")
            download_csv(payload["csv_url"], csv_path)
            row_timeout = float(payload.get("jar_timeout_secs") or DEFAULT_ROW_JAR_TIMEOUT_SECS)
            summary = run_bulk_create(csv_path, ds_xml, row_timeout)
            jar_output = json.dumps(asdict(summary)) + "\n"
            jar_stderr = ""
            # Non-zero exit when total work with zero successes is a failure.
            # A partial failure (any success) stays exit 0 so callers don't
            # retry the successful rows.
            if summary.total > 0 and summary.succeeded == 0:
                jar_exit_code = 2
            else:
                jar_exit_code = 0
        else:
            args = build_args(payload["action"], payload, SINGLE_ACTION_FIELDS, ds_xml)
            proc = run_jar(args)
            jar_output = proc.stdout
            jar_stderr = proc.stderr
            jar_exit_code = proc.returncode

        status = "succeeded" if jar_exit_code == 0 else "failed"
        if jar_exit_code == 0:
            _record_result(pg, result_table, correlation_id,
                           status, extract_jar_json(jar_output), "")
        else:
            _record_result(pg, result_table, correlation_id,
                           status, extract_jar_json(jar_output),
                           f"JAR exited with code {jar_exit_code}: {jar_stderr}")

        if jar_output:
            sys.stdout.write(jar_output)
            if not jar_output.endswith("\n"):
                sys.stdout.write("\n")
        if jar_stderr:
            sys.stderr.write(jar_stderr)
            if not jar_stderr.endswith("\n"):
                sys.stderr.write("\n")
        return jar_exit_code

    except ValidationError as e:
        msg = str(e)
        _record_result(pg, result_table, correlation_id, "failed", "", msg)
        _emit_error_json(msg)
        return 1
    finally:
        if tmpdir:
            try:
                shutil.rmtree(tmpdir)
            except OSError as e:
                print(
                    f"WARNING: Failed to remove tempdir containing data-sources.xml: {e}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
