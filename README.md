# api-key-service

Docker wrapper for the [OneBusAway API Key CLI](https://github.com/OneBusAway/onebusaway-application-modules/tree/master/onebusaway-api-key-cli) JAR, designed to run as [Render one-off jobs](https://render.com/docs/one-off-jobs) for managing API keys in a PostgreSQL-backed OneBusAway deployment.

Supports single-key CRUD operations and bulk CSV imports against an existing OneBusAway schema.

## Prerequisites

- Docker

## Building

```bash
docker build -t api-key-service .
```

To use a different JAR version:

```bash
docker build --build-arg JAR_VERSION=2.7.1 -t api-key-service .
```

## Usage

The entrypoint (`/app/entrypoint.sh`, a Python 3 script — the `.sh` name is preserved so Render's `startCommand` and the Dockerfile `ENTRYPOINT` stay stable) accepts a single argument containing the action, database credentials, and any command-specific fields. The argument can be either:

- **Base64-encoded JSON** (preferred, especially for Render) — avoids shell tokenization issues when fields contain spaces. Whitespace-wrapped base64 (e.g. `base64`'s default 76-column output) is accepted.
- **Raw JSON object** — supported for backwards compatibility

### JSON input

| Field | Required | Description |
|-------|----------|-------------|
| `action` | Yes | One of: `create`, `list`, `get`, `update`, `delete`, `bulk_create` |
| `db_url` | Yes | JDBC PostgreSQL URL (e.g. `jdbc:postgresql://host:5432/dbname`) |
| `db_user` | Yes | Database username |
| `db_pass` | Yes | Database password |
| `key` | No | API key value (auto-generated UUID if omitted on create) |
| `name` | No | Contact name |
| `email` | No | Contact email |
| `company` | No | Contact company |
| `details` | No | Contact details |
| `minApiReqInt` | No | Minimum API request interval in ms (default: 100) |
| `correlation_id` | No | UUID used to key result-table rows (must be paired with `result_table`) |
| `result_table` | No | Name of a table where the action's result JSON is recorded (auto-created on first use). Must be a valid lowercase identifier and paired with `correlation_id`. |
| `csv_url` | Required for `bulk_create` | HTTPS URL to a CSV of keys to import (see `bulk_create` below) |
| `jar_timeout_secs` | No | Per-row JAR invocation timeout for `bulk_create` (default: `300`) |

### Examples

**List all keys:**

```bash
docker run api-key-service '{"action":"list","db_url":"jdbc:postgresql://host:5432/oba","db_user":"admin","db_pass":"secret"}'
```

**Create a key:**

```bash
docker run api-key-service '{"action":"create","db_url":"jdbc:postgresql://host:5432/oba","db_user":"admin","db_pass":"secret","key":"my-api-key","email":"user@example.com","name":"Jane Doe","company":"Transit Co"}'
```

**Get, update, delete** follow the same shape with `"action": "get" | "update" | "delete"` and `"key": "..."`.

### `bulk_create`: batch import from CSV

`bulk_create` downloads a CSV from `csv_url` and invokes the JAR's `create` path once per row. The CSV must have a header row with columns `name, email, company, api_key, notes` (any order).

Constraints and behavior:

- **Download cap:** 10 MB body, 60s timeout. Enforced via `Content-Length` when present and via a streaming byte counter otherwise. The `csv_url` is never logged on error — signed URLs may carry credentials.
- **Per-row JAR timeout:** 300s by default (override with `jar_timeout_secs`). A hung row fails just that row, not the whole import.
- **Partial failures do not fail the job.** If any row succeeds, exit code is `0` and the result JSON has per-row detail. If *every* row fails (or the CSV has rows but all are rejected), exit code is `2` — callers can retry without re-running the successful rows.
- **Encoding:** CSV is parsed as UTF-8 with optional BOM; malformed UTF-8 or structural CSV errors are reported as a `ValidationError` and exit `1`.

**Example payload:**

```json
{
  "action": "bulk_create",
  "db_url": "jdbc:postgresql://host:5432/oba",
  "db_user": "admin",
  "db_pass": "secret",
  "csv_url": "https://storage.example.com/imports/batch.csv?signature=..."
}
```

**Summary output** (written to stdout, and to `result_data` if `correlation_id`/`result_table` are set):

```json
{
  "total": 3,
  "succeeded": 2,
  "failed": 1,
  "errors": [
    {"row": 2, "key": "dup_key_abc", "error": "duplicate key value violates unique constraint..."}
  ]
}
```

### Recording results (`correlation_id` + `result_table`)

When both `correlation_id` (a UUID) and `result_table` (a valid lowercase identifier) are set, the entrypoint writes a row to `result_table` recording the outcome:

| Column | Meaning |
|--------|---------|
| `correlation_id` | Unique key provided by the caller |
| `status` | `succeeded` or `failed` |
| `result_data` | The JAR's JSON output (or the `bulk_create` summary) as JSONB |
| `error_message` | Error text when `status = failed` (includes the psql diagnostic when a write itself fails) |
| `created_at` | Row timestamp; rows older than 24 h are purged on the next run |

The table is auto-created on first use. If the action itself fails validation (bad JSON, missing fields, malformed CSV, etc.) the row is still written with `status = failed` so callers can key off `correlation_id` without having to parse stdout.

## Testing

**Unit tests** mock `java` and `psql` as executables on `PATH`; no Docker or database required:

```bash
uvx pytest
```

**Integration tests** run the service container against a real PostgreSQL via Docker Compose:

```bash
docker compose -f docker-compose.test.yml build
docker compose -f docker-compose.test.yml up -d --wait postgres
uvx --with psycopg2-binary --with pytest pytest tests/integration/ -v
docker compose -f docker-compose.test.yml down -v
```

The `bulk_create` integration tests serve a CSV from the test host using `host.docker.internal`. `docker-compose.test.yml` sets `extra_hosts: host.docker.internal:host-gateway` so this works on Linux CI as well as Docker Desktop.

## Render deployment

This service is deployed as a **worker** on Render (no HTTP endpoint). API key operations are triggered via the [Render one-off job API](https://api-docs.render.com/reference/create-job).

### Step 1: Deploy the service

Click **New > Blueprint** in the Render dashboard and connect this repo. The `render.yaml` will configure the worker automatically.

Alternatively, create a **Worker** service manually, set the runtime to **Docker**, and point it at this repo.

### Step 2: Get your PostgreSQL connection details

In the Render dashboard, go to your PostgreSQL database and find the **Connection** section. You'll need three values:

| Render field | JSON field | Example |
|-------------|------------|---------|
| Internal Database URL | `db_url` | The hostname portion, e.g. `dpg-abc123` |
| Username | `db_user` | `myuser` |
| Password | `db_pass` | `mypass` |

**Convert the Render URL to JDBC format:**

Render gives you: `postgres://myuser:mypass@dpg-abc123.oregon-postgres.render.com:5432/mydb`

Strip the credentials and change the scheme to get the `db_url`:
- **Internal** (same region, faster, no egress): `jdbc:postgresql://dpg-abc123:5432/mydb`
- **External** (different region or outside Render): `jdbc:postgresql://dpg-abc123.oregon-postgres.render.com:5432/mydb`

Use the internal hostname when the worker and database are in the same Render region.

### Step 3: Get your Render API key and service ID

1. Create an API key at **Account Settings > API Keys** in the Render dashboard
2. Find your worker's **Service ID** in the worker's settings page (starts with `srv-`)

### Step 4: Trigger a one-off job

```bash
JSON='{"action":"create","db_url":"jdbc:postgresql://dpg-abc123:5432/mydb","db_user":"myuser","db_pass":"mypass","name":"Jane Doe"}'
B64=$(printf '%s' "$JSON" | base64)
curl -X POST "https://api.render.com/v1/services/srv-YOUR_SERVICE_ID/jobs" \
  -H "Authorization: Bearer rnd_YOUR_RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"startCommand\":\"/app/entrypoint.sh $B64\"}"
```

Base64 encoding avoids shell tokenization issues when any field contains spaces (e.g. a contact name like `Jane Doe`); Render splits `startCommand` on whitespace, so raw JSON breaks whenever a value has a space. Raw JSON is still accepted for payloads that have no spaces.

You can check the job's output in the Render dashboard under your worker's **Logs** tab, or query the `result_table` row directly if you supplied a `correlation_id`.

## Security

Database credentials are passed per-invocation in the JSON blob and never stored in environment variables or on disk beyond the brief life of a temporary `data-sources.xml`. The entrypoint:

- Creates a temporary directory with `700` permissions via `tempfile.mkdtemp`
- Writes `data-sources.xml` with `600` permissions, using XML attribute escaping so passwords containing `"` or `&` don't break the file
- Removes the temp directory in a `finally` block; logs a warning to stderr if removal fails (never silently leaks)
- Never echoes `csv_url` to stdout, stderr, or any recorded error message
- Uses random-tag dollar-quoting (`$dq<12-hex>$...$dq<12-hex>$`) when interpolating user-controlled data into psql SQL bodies

## Architecture

The fat JAR (`onebusaway-api-key-cli-2.7.1-withAllDependencies.jar`) bundles a MySQL driver but not PostgreSQL. The Dockerfile downloads the PostgreSQL JDBC driver separately, and the entrypoint invokes the JAR via `-cp` (classpath) rather than `-jar` to include both JARs.

The entrypoint shells out to the `psql` binary (bundled in the image) rather than using `psycopg2` so the unit-test suite can mock `java` and `psql` as simple executables on `PATH`. All JAR output is captured as JSON via the `-j` flag. For `bulk_create`, the entrypoint drives the CSV loop itself and invokes the JAR's `create` path once per row — no JAR modifications are required.
