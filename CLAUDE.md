# api-key-service

Docker wrapper for the OneBusAway API Key CLI JAR, designed to run as Render one-off jobs. Supports single-key CRUD and bulk CSV imports.

## Architecture

- `entrypoint.sh` is a Python 3 script (filename kept so Dockerfile `ENTRYPOINT` and Render `startCommand` don't change). It parses the JSON input, generates a temporary `data-sources.xml` with PostgreSQL JDBC credentials, invokes the JAR, and cleans up on exit.
- The entrypoint deliberately shells out to the `psql` and `java` binaries (not psycopg2 / JNI) so the unit test suite can mock both as executables on `PATH`. Do not replace with psycopg2 without rewriting the psql-mocking tests.
- For `bulk_create` the entrypoint drives the CSV loop itself and calls the JAR's `create` path once per row. **Do not modify the JAR** — everything `bulk_create` needs is already exposed via `create -k -n -e -o -d`.
- The fat JAR (`onebusaway-api-key-cli-2.7.1-withAllDependencies.jar`) bundles MySQL but not PostgreSQL, so we download `postgresql-42.7.5.jar` separately and invoke via `-cp`.
- Main class: `org.onebusaway.cli.apikey.ApiKeyCliMain`

## Key implementation details (don't break these)

- **Result recording:** when both `correlation_id` and `result_table` are set, `_record_result` writes a row with status / `result_data` JSONB / `error_message`. `ValidationError` paths construct the `PgClient` best-effort *before* `validate()` runs so validation failures still land in the table on first run.
- **Dollar-quoted psql SQL:** user-controlled bodies are wrapped in `$dq<12-hex>$...$dq<12-hex>$` tags. `correlation_id` and `status` are interpolated directly but are regex-validated (`UUID_RE`, fixed allowlist) before reaching SQL.
- **Download safety:** `csv_url` is never echoed to stdout, stderr, or `error_message`. `download_csv` logs `type(e).__name__: str(e)` (not the URL) on failure so operators can distinguish DNS/TLS/refused/404.
- **Exit semantics:** `bulk_create` exits `0` on full success or partial success, `2` when `total > 0 and succeeded == 0` (so callers don't retry already-successful rows). Other actions pass through the JAR's exit code.
- **Per-row JAR timeout:** 300s default, overridable via `jar_timeout_secs`. A hung row fails just that row.

## Build

```bash
docker build -t api-key-service .
```

## Test

Unit tests (mock java/psql, no Docker needed):

```bash
uvx pytest
```

Integration tests (real PostgreSQL via docker compose). The `bulk_create` integration tests serve a CSV from the host using `host.docker.internal`; `docker-compose.test.yml` sets `extra_hosts: host.docker.internal:host-gateway` so this also works on Linux CI.

```bash
docker compose -f docker-compose.test.yml build
docker compose -f docker-compose.test.yml up -d --wait postgres
uvx --with psycopg2-binary --with pytest pytest tests/integration/ -v
docker compose -f docker-compose.test.yml down -v
```

The unit-test harness at `tests/conftest.py` exposes a `mock_bin` fixture and `run_entrypoint` helper; the integration harness at `tests/integration/conftest.py` provides a session-scoped `db_conn` (autocommit psycopg2) and compose lifecycle.

## JSON input fields

- **Required:** `action` (`create`|`list`|`get`|`update`|`delete`|`bulk_create`), `db_url`, `db_user`, `db_pass`
- **Optional single-action:** `key`, `name`, `email`, `company`, `details`, `minApiReqInt`
- **Optional result recording:** `correlation_id` (UUID), `result_table` (lowercase identifier) — must be paired
- **Required for `bulk_create`:** `csv_url` — HTTPS URL of a CSV with columns `name, email, company, api_key, notes` in any order
- **Optional for `bulk_create`:** `jar_timeout_secs` (default 300)

`bulk_create` downloads the CSV (10 MB cap, 60s timeout), iterates rows, and outputs a `{total, succeeded, failed, errors}` summary to stdout (and to `result_data` if recording is enabled).

## Render deployment

Triggered via the Render one-off job API. The `startCommand` should pass a base64-encoded JSON blob to avoid argv tokenization issues when fields contain spaces (Render splits `startCommand` on whitespace):

```
/app/entrypoint.sh <base64-encoded-json>
```

Whitespace-wrapped base64 (GNU/BSD `base64` default 76-column output) is accepted. Raw JSON is still accepted for backwards compatibility:

```
/app/entrypoint.sh '<json_blob>'
```
