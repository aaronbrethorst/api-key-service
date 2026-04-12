# api-key-service

Docker wrapper for the OneBusAway API Key CLI JAR, designed to run as Render one-off jobs.

## Architecture

- `entrypoint.sh` is a Python 3 script (the filename is kept so the Dockerfile `ENTRYPOINT` and Render `startCommand` don't change). It receives a flat JSON blob, generates a temporary `data-sources.xml` with PostgreSQL JDBC credentials, invokes the JAR, and cleans up on exit. For `bulk_create` it downloads a CSV and invokes the JAR's `create` action once per row.
- The fat JAR (`onebusaway-api-key-cli-2.7.1-withAllDependencies.jar`) bundles MySQL but not PostgreSQL, so we download `postgresql-42.7.5.jar` separately and use `-cp` invocation.
- Main class: `org.onebusaway.cli.apikey.ApiKeyCliMain`

## Build

```bash
docker build -t api-key-service .
```

## Test

Unit tests (mock java/psql, no Docker needed):

```bash
uvx pytest
```

Integration tests (real PostgreSQL via docker compose):

```bash
docker compose -f docker-compose.test.yml build
docker compose -f docker-compose.test.yml up -d --wait postgres
uvx --with psycopg2-binary --with pytest pytest tests/integration/ -v
docker compose -f docker-compose.test.yml down -v
```

## JSON input fields

- **Required:** `action` (create|list|get|update|delete|bulk_create), `db_url`, `db_user`, `db_pass`
- **Optional:** `key`, `name`, `email`, `company`, `details`, `minApiReqInt`, `correlation_id`, `result_table`
- **Required for `bulk_create`:** `csv_url` — HTTPS URL of a CSV with columns `name, email, company, api_key, notes`. The service downloads the CSV (10 MB cap, 60s timeout), iterates rows, and reports a `{total, succeeded, failed, errors}` summary. Partial-row failures do not fail the job.

## Render deployment

Triggered via the Render one-off job API. The `startCommand` should pass a
base64-encoded JSON blob to avoid argv tokenization issues when fields contain
spaces (Render splits `startCommand` on whitespace):
```
/app/entrypoint.sh <base64-encoded-json>
```

Raw JSON is still accepted for backwards compatibility:
```
/app/entrypoint.sh '<json_blob>'
```
