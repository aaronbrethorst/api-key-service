# api-key-service

Docker wrapper for the OneBusAway API Key CLI JAR, designed to run as Render one-off jobs.

## Architecture

- `entrypoint.sh` receives a flat JSON blob, generates a temporary `data-sources.xml` with PostgreSQL JDBC credentials, invokes the JAR, and cleans up on exit.
- The fat JAR (`onebusaway-api-key-cli-2.7.1-withAllDependencies.jar`) bundles MySQL but not PostgreSQL, so we download `postgresql-42.7.5.jar` separately and use `-cp` invocation.
- Main class: `org.onebusaway.cli.apikey.ApiKeyCliMain`

## Build

```bash
docker build -t api-key-service .
```

## Test

Unit tests use pytest (via uv) with mock java/psql binaries — no Docker or database needed:

```bash
uvx pytest tests/ -v
```

Integration test via Docker (no database — expects a connection error from the JAR):

```bash
docker run api-key-service '{"action":"list","db_url":"jdbc:postgresql://host:5432/db","db_user":"user","db_pass":"pass"}'
```

## JSON input fields

- **Required:** `action` (create|list|get|update|delete), `db_url`, `db_user`, `db_pass`
- **Optional:** `key`, `name`, `email`, `company`, `details`, `minApiReqInt`

## Render deployment

Triggered via the Render one-off job API. The `startCommand` is:
```
/app/entrypoint.sh '<json_blob>'
```
