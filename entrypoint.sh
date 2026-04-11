#!/usr/bin/env bash
set -euo pipefail

SECURE_TMPDIR=""

cleanup() {
  if [ -n "$SECURE_TMPDIR" ]; then
    rm -rf "$SECURE_TMPDIR"
  fi
}
trap cleanup EXIT

# --- psql helper functions (defined early so error_exit can use write_result) ---

run_psql() {
  PGPASSWORD="${db_pass:-}" psql -h "${pg_host:-}" -p "${pg_port:-}" -U "${db_user:-}" -d "${pg_dbname:-}" -q "$@"
}

ensure_result_table() {
  run_psql <<EOSQL
    CREATE TABLE IF NOT EXISTS ${result_table} (
      id              BIGSERIAL PRIMARY KEY,
      correlation_id  UUID NOT NULL UNIQUE,
      status          VARCHAR(20) NOT NULL DEFAULT 'succeeded',
      result_data     JSONB,
      error_message   TEXT,
      created_at      TIMESTAMP NOT NULL DEFAULT NOW()
    );
EOSQL
}

write_result() {
  local status="$1"
  local result_data="$2"
  local error_message="$3"

  # Guard: skip if psql connection params aren't set yet
  if [ -z "${pg_host:-}" ] || [ -z "${result_table:-}" ]; then
    return 0
  fi

  local rd_sql="NULL"
  if [ -n "$result_data" ]; then
    if echo "$result_data" | jq empty 2>/dev/null; then
      rd_sql="\$rd\$${result_data}\$rd\$::jsonb"
    else
      # JAR produced non-JSON output; store as error instead of silently dropping
      if [ -z "$error_message" ]; then
        error_message="Non-JSON output: $result_data"
      fi
    fi
  fi

  local em_sql="NULL"
  if [ -n "$error_message" ]; then
    em_sql="\$err\$${error_message}\$err\$"
  fi

  run_psql <<EOSQL
    INSERT INTO ${result_table} (correlation_id, status, result_data, error_message)
    VALUES ('${correlation_id}', '${status}', ${rd_sql}, ${em_sql})
    ON CONFLICT (correlation_id) DO UPDATE SET
      status = EXCLUDED.status,
      result_data = EXCLUDED.result_data,
      error_message = EXCLUDED.error_message;
EOSQL
}

error_exit() {
  local msg="$1"
  if [ -n "${correlation_id:-}" ] && [ -n "${result_table:-}" ]; then
    write_result "failed" "" "$msg" 2>/dev/null || true
  fi
  jq -n --arg msg "$msg" '{"success":false,"error":$msg}'
  exit 1
}

# --- Parse input ---

if [ $# -lt 1 ]; then
  error_exit "Usage: entrypoint.sh '<json_blob>'"
fi

JSON_INPUT="$1"

if ! echo "$JSON_INPUT" | jq empty 2>/dev/null; then
  error_exit "Invalid JSON input"
fi

action=$(echo "$JSON_INPUT" | jq -r '.action // ""')
db_url=$(echo "$JSON_INPUT" | jq -r '.db_url // ""')
db_user=$(echo "$JSON_INPUT" | jq -r '.db_user // ""')
db_pass=$(echo "$JSON_INPUT" | jq -r '.db_pass // ""')

for field in action db_url db_user db_pass; do
  if [ -z "${!field}" ]; then
    error_exit "Missing required field: $field"
  fi
done

case "$action" in
  create|list|get|update|delete) ;;
  *) error_exit "Invalid action: $action. Must be one of: create, list, get, update, delete" ;;
esac

correlation_id=$(echo "$JSON_INPUT" | jq -r '.correlation_id // ""')
result_table=$(echo "$JSON_INPUT" | jq -r '.result_table // ""')

if [ -n "$result_table" ] && ! echo "$result_table" | grep -qE '^[a-z_][a-z0-9_]*$'; then
  error_exit "Invalid result_table name: $result_table"
fi

if [ -n "$correlation_id" ] && ! echo "$correlation_id" | grep -qE '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
  error_exit "Invalid correlation_id: $correlation_id"
fi

key=$(echo "$JSON_INPUT" | jq -r '.key // ""')
name=$(echo "$JSON_INPUT" | jq -r '.name // ""')
email=$(echo "$JSON_INPUT" | jq -r '.email // ""')
company=$(echo "$JSON_INPUT" | jq -r '.company // ""')
details=$(echo "$JSON_INPUT" | jq -r '.details // ""')
minApiReqInt=$(echo "$JSON_INPUT" | jq -r '.minApiReqInt // ""')

# --- Parse JDBC URL for psql connection ---

if [ -n "$correlation_id" ] && [ -n "$result_table" ]; then
  pg_host=$(echo "$db_url" | sed -E 's|jdbc:postgresql://([^:]+):.*|\1|')
  pg_port=$(echo "$db_url" | sed -E 's|jdbc:postgresql://[^:]+:([0-9]+)/.*|\1|')
  pg_dbname=$(echo "$db_url" | sed -E 's|jdbc:postgresql://[^/]+/(.+)|\1|')
fi

# --- Build data-sources.xml ---

xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  s="${s//\"/&quot;}"
  s="${s//\'/&apos;}"
  echo "$s"
}

SECURE_TMPDIR=$(mktemp -d)
chmod 700 "$SECURE_TMPDIR"

escaped_url=$(xml_escape "$db_url")
escaped_user=$(xml_escape "$db_user")
escaped_pass=$(xml_escape "$db_pass")

cat > "$SECURE_TMPDIR/data-sources.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<beans xmlns="http://www.springframework.org/schema/beans"
       xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
       xsi:schemaLocation="http://www.springframework.org/schema/beans
         http://www.springframework.org/schema/beans/spring-beans.xsd">
  <bean id="dataSource" class="org.springframework.jdbc.datasource.DriverManagerDataSource">
    <property name="driverClassName" value="org.postgresql.Driver"/>
    <property name="url" value="${escaped_url}"/>
    <property name="username" value="${escaped_user}"/>
    <property name="password" value="${escaped_pass}"/>
  </bean>
</beans>
EOF
chmod 600 "$SECURE_TMPDIR/data-sources.xml"

# --- Build JAR arguments ---

ARGS=("$action")

[ -n "$key" ] && ARGS+=(-k "$key")
[ -n "$name" ] && ARGS+=(-n "$name")
[ -n "$email" ] && ARGS+=(-e "$email")
[ -n "$company" ] && ARGS+=(-o "$company")
[ -n "$details" ] && ARGS+=(-d "$details")
[ -n "$minApiReqInt" ] && ARGS+=(-m "$minApiReqInt")

ARGS+=(-c "$SECURE_TMPDIR/data-sources.xml" -j)

# --- Run JAR and capture output ---

# shellcheck disable=SC2206
JAVA_OPTS_ARRAY=(${JAVA_OPTS:-})

jar_stderr_file=$(mktemp)
jar_exit_code=0
jar_output=$(java "${JAVA_OPTS_ARRAY[@]}" \
  -cp "/app/api-key-cli.jar:/app/postgresql.jar" \
  org.onebusaway.cli.apikey.ApiKeyCliMain \
  "${ARGS[@]}" 2>"$jar_stderr_file") || jar_exit_code=$?
jar_stderr=$(cat "$jar_stderr_file")
rm -f "$jar_stderr_file"

# --- Write result to table if correlation_id is set ---

if [ -n "$correlation_id" ] && [ -n "$result_table" ]; then
  ensure_result_table

  # Clean up orphaned rows older than 24 hours
  run_psql -c "DELETE FROM ${result_table} WHERE created_at < NOW() - INTERVAL '24 hours';" 2>/dev/null || true

  if [ "$jar_exit_code" -eq 0 ]; then
    write_result "succeeded" "$jar_output" ""
  else
    write_result "failed" "" "JAR exited with code $jar_exit_code: $jar_stderr"
  fi
fi

# Always print to stdout/stderr for Render dashboard visibility
echo "$jar_output"
if [ -n "$jar_stderr" ]; then
  echo "$jar_stderr" >&2
fi
exit "$jar_exit_code"
