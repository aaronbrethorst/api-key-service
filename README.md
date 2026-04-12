# api-key-service

Docker wrapper for the [OneBusAway API Key CLI](https://github.com/OneBusAway/onebusaway-application-modules/tree/master/onebusaway-api-key-cli) JAR, designed to run as [Render one-off jobs](https://render.com/docs/one-off-jobs) for managing API keys in a PostgreSQL-backed OneBusAway deployment.

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

The entrypoint accepts a single argument containing the action, database credentials, and any command-specific fields. The argument can be either:

- **Base64-encoded JSON** (preferred, especially for Render) — avoids shell tokenization issues when fields contain spaces
- **Raw JSON** — supported for backwards compatibility

### JSON Input

| Field | Required | Description |
|-------|----------|-------------|
| `action` | Yes | One of: `create`, `list`, `get`, `update`, `delete` |
| `db_url` | Yes | JDBC PostgreSQL URL (e.g. `jdbc:postgresql://host:5432/dbname`) |
| `db_user` | Yes | Database username |
| `db_pass` | Yes | Database password |
| `key` | No | API key value (auto-generated UUID if omitted on create) |
| `name` | No | Contact name |
| `email` | No | Contact email |
| `company` | No | Contact company |
| `details` | No | Contact details |
| `minApiReqInt` | No | Minimum API request interval in ms (default: 100) |

### Examples

**List all keys:**

```bash
docker run api-key-service '{"action":"list","db_url":"jdbc:postgresql://host:5432/oba","db_user":"admin","db_pass":"secret"}'
```

**Create a key:**

```bash
docker run api-key-service '{"action":"create","db_url":"jdbc:postgresql://host:5432/oba","db_user":"admin","db_pass":"secret","key":"my-api-key","email":"user@example.com","name":"Jane Doe","company":"Transit Co"}'
```

**Get a key:**

```bash
docker run api-key-service '{"action":"get","db_url":"jdbc:postgresql://host:5432/oba","db_user":"admin","db_pass":"secret","key":"my-api-key"}'
```

**Update a key:**

```bash
docker run api-key-service '{"action":"update","db_url":"jdbc:postgresql://host:5432/oba","db_user":"admin","db_pass":"secret","key":"my-api-key","email":"new@example.com"}'
```

**Delete a key:**

```bash
docker run api-key-service '{"action":"delete","db_url":"jdbc:postgresql://host:5432/oba","db_user":"admin","db_pass":"secret","key":"my-api-key"}'
```

## Testing

**Unit tests** mock `java` and `psql`, so no Docker or database is required:

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

## Render Deployment

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
curl -X POST "https://api.render.com/v1/services/srv-YOUR_SERVICE_ID/jobs" \
  -H "Authorization: Bearer rnd_YOUR_RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "startCommand": "/app/entrypoint.sh '\''{ \"action\": \"list\", \"db_url\": \"jdbc:postgresql://dpg-abc123:5432/mydb\", \"db_user\": \"myuser\", \"db_pass\": \"mypass\" }'\''"
  }'
```

**Recommended: base64-encode the JSON** to avoid shell tokenization issues when
any field contains spaces (e.g. a contact name like `Jane Doe`). Render splits
`startCommand` on whitespace, so raw JSON breaks whenever a value has a space:

```bash
JSON='{"action":"create","db_url":"jdbc:postgresql://dpg-abc123:5432/mydb","db_user":"myuser","db_pass":"mypass","name":"Jane Doe"}'
B64=$(printf '%s' "$JSON" | base64)
curl -X POST "https://api.render.com/v1/services/srv-YOUR_SERVICE_ID/jobs" \
  -H "Authorization: Bearer rnd_YOUR_RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"startCommand\":\"/app/entrypoint.sh $B64\"}"
```

You can check the job's output in the Render dashboard under your worker's **Logs** tab, or poll the job status via the API.

## Security

Database credentials are passed per-invocation in the JSON blob and never stored in environment variables or on disk. The entrypoint:

- Creates a temporary directory with `700` permissions
- Writes `data-sources.xml` with `600` permissions
- Removes the temp directory on exit via a `trap`, even on error

## Architecture

The fat JAR (`onebusaway-api-key-cli-2.7.1-withAllDependencies.jar`) bundles a MySQL driver but not PostgreSQL. The Dockerfile downloads the PostgreSQL JDBC driver separately, and the entrypoint invokes the JAR via `-cp` (classpath) rather than `-jar` to include both JARs.

All output from the JAR is in JSON format (via the `-j` flag).
