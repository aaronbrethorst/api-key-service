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

The entrypoint accepts a single JSON argument containing the action, database credentials, and any command-specific fields.

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

## Render Deployment

This service is deployed as a **worker** on Render (no HTTP endpoint). API key operations are triggered via the [Render one-off job API](https://api-docs.render.com/reference/create-job):

```bash
curl -X POST "https://api.render.com/v1/services/YOUR_SERVICE_ID/jobs" \
  -H "Authorization: Bearer YOUR_RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "startCommand": "/app/entrypoint.sh '\''{ \"action\": \"list\", \"db_url\": \"jdbc:postgresql://host:5432/oba\", \"db_user\": \"admin\", \"db_pass\": \"secret\" }'\''"
  }'
```

## Security

Database credentials are passed per-invocation in the JSON blob and never stored in environment variables or on disk. The entrypoint:

- Creates a temporary directory with `700` permissions
- Writes `data-sources.xml` with `600` permissions
- Removes the temp directory on exit via a `trap`, even on error

## Architecture

The fat JAR (`onebusaway-api-key-cli-2.7.1-withAllDependencies.jar`) bundles a MySQL driver but not PostgreSQL. The Dockerfile downloads the PostgreSQL JDBC driver separately, and the entrypoint invokes the JAR via `-cp` (classpath) rather than `-jar` to include both JARs.

All output from the JAR is in JSON format (via the `-j` flag).
