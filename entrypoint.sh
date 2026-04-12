#!/usr/bin/env python3
"""Entrypoint for the OneBusAway API Key service container.

Accepts a JSON blob (raw or base64-encoded) describing a single action against
the api-key-cli JAR, or a bulk_create action that iterates a CSV file.

Named `entrypoint.sh` rather than `entrypoint.py` so the Dockerfile ENTRYPOINT
and Render startCommand keep working without change. The shebang drives it.
"""

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
from typing import Optional
from xml.sax.saxutils import quoteattr

CSV_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
CSV_TIMEOUT_SECS = 60

VALID_ACTIONS = {"create", "list", "get", "update", "delete", "bulk_create"}
REQUIRED_FIELDS = ("action", "db_url", "db_user", "db_pass")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TABLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

JAR_PATH = "/app/api-key-cli.jar"
PG_DRIVER_PATH = "/app/postgresql.jar"
MAIN_CLASS = "org.onebusaway.cli.apikey.ApiKeyCliMain"


class ValidationError(Exception):
    pass


# --- input parsing ---------------------------------------------------------

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
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except Exception:
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
    for field in REQUIRED_FIELDS:
        if not payload.get(field):
            raise ValidationError(f"Missing required field: {field}")

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


# --- JDBC parsing ----------------------------------------------------------

def parse_jdbc_url(db_url: str) -> dict:
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
    return {"host": host, "port": port, "dbname": dbname, "sslmode": sslmode}


# --- data-sources.xml ------------------------------------------------------

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


# --- psql client -----------------------------------------------------------

class PgClient:
    """Shells out to psql so the existing test suite (which mocks psql as a
    binary on PATH) keeps working. All SQL that interpolates user-controlled
    data is dollar-quoted with a random tag to prevent body breakout."""

    def __init__(self, host, port, dbname, sslmode, user, password):
        self.host = host
        self.port = port
        self.dbname = dbname
        self.sslmode = sslmode
        self.user = user
        self.password = password

    @classmethod
    def from_jdbc(cls, db_url: str, user: str, password: str) -> "PgClient":
        pg = parse_jdbc_url(db_url)
        return cls(pg["host"], pg["port"], pg["dbname"], pg["sslmode"], user, password)

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

    def ensure_result_table(self, result_table: str) -> bool:
        ddl = f"""    CREATE TABLE IF NOT EXISTS "{result_table}" (
      id              BIGSERIAL PRIMARY KEY,
      correlation_id  UUID NOT NULL UNIQUE,
      status          VARCHAR(20) NOT NULL DEFAULT 'succeeded',
      result_data     JSONB,
      error_message   TEXT,
      created_at      TIMESTAMP NOT NULL DEFAULT NOW()
    );
"""
        return self._run(stdin=ddl).returncode == 0

    def purge_old_rows(self, result_table: str) -> None:
        self._run(
            "-c",
            f'DELETE FROM "{result_table}" WHERE created_at < NOW() - INTERVAL \'24 hours\';',
        )

    def write_result(self, result_table: str, correlation_id: str,
                     status: str, result_data: str, error_message: str) -> bool:
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
        return self._run(stdin=sql).returncode == 0


def _dollar_quote(body: str, prefix: str) -> str:
    tag = prefix + os.urandom(6).hex()
    return f"${tag}${body}${tag}$"


# --- JAR invocation --------------------------------------------------------

def _java_opts() -> list:
    return shlex.split(os.environ.get("JAVA_OPTS", ""))

def run_jar(args: list) -> subprocess.CompletedProcess:
    cmd = [
        "java", *_java_opts(),
        "-cp", f"{JAR_PATH}:{PG_DRIVER_PATH}",
        MAIN_CLASS,
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def download_csv(url: str, dest_path: str) -> None:
    """Download CSV at `url` to `dest_path` with a hard size cap and timeout.

    Never includes `url` in raised error messages — signed URLs may carry
    credentials.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "api-key-service"})
    try:
        resp = urllib.request.urlopen(req, timeout=CSV_TIMEOUT_SECS)
    except urllib.error.HTTPError as e:
        raise ValidationError(f"Failed to download CSV: HTTP {e.code}")
    except (urllib.error.URLError, socket.timeout, OSError):
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
    for field, flag in fields:
        val = (source.get(field) or "").strip()
        if val:
            args += [flag, val]
    args += ["-c", ds_xml_path, "-j"]
    return args


def run_bulk_create(csv_path: str, ds_xml_path: str) -> dict:
    total = succeeded = failed = 0
    errors: list = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for row_num, row in enumerate(csv.DictReader(f), start=1):
            total += 1
            api_key = (row.get("api_key") or "").strip()
            if not api_key:
                failed += 1
                errors.append({"row": row_num, "error": "missing api_key"})
                continue
            args = build_args("create", row, BULK_ROW_FIELDS, ds_xml_path,
                              initial=("-k", api_key))
            proc = run_jar(args)
            if proc.returncode == 0:
                succeeded += 1
            else:
                failed += 1
                err_text = (proc.stderr or "").strip()[:500] or (
                    f"JAR exited with code {proc.returncode}"
                )
                errors.append({"row": row_num, "key": api_key, "error": err_text})
    return {"total": total, "succeeded": succeeded, "failed": failed, "errors": errors}


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
    if not pg.ensure_result_table(result_table):
        print(
            f"ERROR: Failed to create/verify result table '{result_table}'. "
            "Result will not be written.",
            file=sys.stderr,
        )
        return
    pg.purge_old_rows(result_table)
    if not pg.write_result(result_table, correlation_id, status, result_data, error_message):
        print(
            f"ERROR: Failed to write result to {result_table} "
            f"for correlation_id={correlation_id}",
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
        validate(payload)

        correlation_id = payload.get("correlation_id") or ""
        result_table = payload.get("result_table") or ""
        db_url = payload["db_url"]
        db_user = payload["db_user"]
        db_pass = payload["db_pass"]

        if correlation_id and result_table:
            pg = PgClient.from_jdbc(db_url, db_user, db_pass)

        tmpdir = tempfile.mkdtemp()
        os.chmod(tmpdir, 0o700)
        ds_xml = write_data_sources_xml(tmpdir, db_url, db_user, db_pass)

        if payload["action"] == "bulk_create":
            csv_path = os.path.join(tmpdir, "import.csv")
            download_csv(payload["csv_url"], csv_path)
            summary = run_bulk_create(csv_path, ds_xml)
            jar_output = json.dumps(summary) + "\n"
            jar_stderr = ""
            jar_exit_code = 0
        else:
            args = build_args(payload["action"], payload, SINGLE_ACTION_FIELDS, ds_xml)
            proc = run_jar(args)
            jar_output = proc.stdout
            jar_stderr = proc.stderr
            jar_exit_code = proc.returncode

        if jar_exit_code == 0:
            _record_result(
                pg, result_table, correlation_id,
                "succeeded", extract_jar_json(jar_output), "",
            )
        else:
            _record_result(
                pg, result_table, correlation_id,
                "failed", "", f"JAR exited with code {jar_exit_code}: {jar_stderr}",
            )

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
        if pg is not None:
            try:
                pg.write_result(result_table, correlation_id, "failed", "", msg)
            except Exception:
                print("WARNING: Failed to write error result to database", file=sys.stderr)
        _emit_error_json(msg)
        return 1
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
