"""Integration tests for bulk_create.

Serves a CSV from the pytest host and passes a `host.docker.internal` URL to
the service container. Requires Docker Desktop (macOS/Windows), which is how
the rest of the integration suite already runs.
"""

import http.server
import json
import socketserver
import threading

import pytest

from .helpers import run_service, make_input, parse_json_output


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.body_bytes
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        return


@pytest.fixture()
def csv_server():
    server = socketserver.TCPServer(("0.0.0.0", 0), _Handler)
    server.body_bytes = b""
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]
    try:
        yield server, port
    finally:
        server.shutdown()
        server.server_close()


def _existing_keys(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("SELECT value FROM oba_user_indices WHERE type='api'")
        return {row[0] for row in cur.fetchall()}


def _cleanup_keys(db_conn, keys):
    if not keys:
        return
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM oba_user_indices WHERE type='api' AND value = ANY(%s)",
            (list(keys),),
        )


HOST_FROM_CONTAINER = "host.docker.internal"


class TestBulkCreateHappyPath:
    def test_three_rows_land_in_db(self, csv_server, db_conn):
        server, port = csv_server
        keys = [f"bulk_ok_{i}" for i in range(3)]
        server.body_bytes = (
            b"name,email,company,api_key,notes\n"
            + f"Alice,alice@ex.com,Acme,{keys[0]},n1\n".encode()
            + f"Bob,bob@ex.com,Bobco,{keys[1]},n2\n".encode()
            + f"Carol,,Carol Inc,{keys[2]},\n".encode()
        )
        csv_url = f"http://{HOST_FROM_CONTAINER}:{port}/import.csv"

        try:
            stdout, stderr, rc = run_service(make_input("bulk_create", csv_url=csv_url))
            assert rc == 0, f"rc={rc} stderr={stderr}"
            summary = parse_json_output(stdout)
            assert summary == {"total": 3, "succeeded": 3, "failed": 0, "errors": []}

            found = _existing_keys(db_conn)
            assert set(keys).issubset(found)
        finally:
            _cleanup_keys(db_conn, keys)


class TestBulkCreatePartialFailure:
    def test_one_preexisting_key_fails_gracefully(self, csv_server, db_conn):
        server, port = csv_server
        keys = [f"bulk_pf_{i}" for i in range(3)]
        stdout, stderr, rc = run_service(
            make_input("create", key=keys[1], name="Pre", email="p@x.y", company="PreCo"),
        )
        assert rc == 0, f"pre-create failed: {stderr}"

        server.body_bytes = (
            b"name,email,company,api_key,notes\n"
            + f"A,a@x.y,X,{keys[0]},\n".encode()
            + f"B,b@x.y,X,{keys[1]},\n".encode()
            + f"C,c@x.y,X,{keys[2]},\n".encode()
        )
        csv_url = f"http://{HOST_FROM_CONTAINER}:{port}/import.csv"

        try:
            stdout, stderr, rc = run_service(make_input("bulk_create", csv_url=csv_url))
            assert rc == 0, f"rc={rc} stderr={stderr}"
            summary = parse_json_output(stdout)
            assert summary["total"] == 3
            assert summary["succeeded"] == 2
            assert summary["failed"] == 1
            assert len(summary["errors"]) == 1
            assert summary["errors"][0]["row"] == 2

            assert set(keys).issubset(_existing_keys(db_conn))
        finally:
            _cleanup_keys(db_conn, keys)


class TestBulkCreateDownloadFailure:
    def test_404_exits_nonzero_without_leaking_url(self):
        csv_url = f"http://{HOST_FROM_CONTAINER}:1/does-not-exist-token-xyz"
        stdout, stderr, rc = run_service(make_input("bulk_create", csv_url=csv_url))
        assert rc == 1
        err = json.loads(stdout.strip().splitlines()[-1])
        assert "does-not-exist-token-xyz" not in err["error"]
        assert "csv" in err["error"].lower() or "download" in err["error"].lower()
