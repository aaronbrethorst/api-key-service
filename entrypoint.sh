#!/usr/bin/env bash
set -euo pipefail

SECURE_TMPDIR=""

cleanup() {
  if [ -n "$SECURE_TMPDIR" ]; then
    rm -rf "$SECURE_TMPDIR"
  fi
}
trap cleanup EXIT

# --- psql helper functions (must precede error_exit, which calls write_result) ---

run_psql() {
  psql "postgresql://${db_user:-}:${db_pass:-}@${pg_host:-}:${pg_port:-}/${pg_dbname:-}?sslmode=${pg_sslmode:-require}" -q "$@"
}

ensure_result_table() {
  run_psql <<EOSQL
    CREATE TABLE IF NOT EXISTS "${result_table}" (
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

  # No-op if result reporting was not requested or connection params are not yet parsed
  if [ -z "${pg_host:-}" ] || [ -z "${result_table:-}" ]; then
    return 0
  fi

  # Use a per-call randomized dollar-quote tag to prevent breakout from data content
  local dq_tag
  dq_tag="dq$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"

  local rd_sql="NULL"
  if [ -n "$result_data" ]; then
    if echo "$result_data" | jq empty 2>/dev/null; then
      rd_sql="\$${dq_tag}\$${result_data}\$${dq_tag}\$::jsonb"
    else
      # JAR produced non-JSON output; store as error since result_data is JSONB-typed
      if [ -z "$error_message" ]; then
        error_message="Non-JSON output: $result_data"
      fi
    fi
  fi

  local em_dq_tag
  em_dq_tag="em$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"

  local em_sql="NULL"
  if [ -n "$error_message" ]; then
    em_sql="\$${em_dq_tag}\$${error_message}\$${em_dq_tag}\$"
  fi

  run_psql <<EOSQL
    INSERT INTO "${result_table}" (correlation_id, status, result_data, error_message)
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
    write_result "failed" "" "$msg" 2>&1 || echo "WARNING: Failed to write error result to database" >&2
  fi
  jq -n --arg msg "$msg" '{"success":false,"error":$msg}'
  exit 1
}

# --- Parse input ---

if [ $# -lt 1 ]; then
  error_exit "Usage: entrypoint.sh '<json_blob>' | <base64-encoded-json>"
fi

# Accept either a base64-encoded JSON object or a raw JSON object for backwards
# compatibility. Base64 encoding avoids shell tokenization issues when the JSON
# contains spaces (e.g., in name/details fields), since Render passes startCommand
# as argv.
#
# Require an object (not a bare scalar) so a base64 string that happens to parse
# as a JSON scalar doesn't mis-route to the raw branch.
is_json_object() {
  printf '%s' "$1" | jq -e 'type=="object"' >/dev/null 2>&1
}

RAW_INPUT="$1"

if is_json_object "$RAW_INPUT"; then
  JSON_INPUT="$RAW_INPUT"
else
  if ! decoded=$(printf '%s' "$RAW_INPUT" | base64 -d 2>/dev/null) || [ -z "$decoded" ]; then
    error_exit "Invalid JSON input: not a JSON object and not valid base64-encoded JSON"
  fi
  if ! is_json_object "$decoded"; then
    error_exit "Invalid JSON input: base64 decoded successfully but payload is not a JSON object"
  fi
  JSON_INPUT="$decoded"
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

# Require both fields together — providing one without the other is a caller bug
if [ -n "$correlation_id" ] && [ -z "$result_table" ]; then
  error_exit "correlation_id provided without result_table"
fi
if [ -n "$result_table" ] && [ -z "$correlation_id" ]; then
  error_exit "result_table provided without correlation_id"
fi

key=$(echo "$JSON_INPUT" | jq -r '.key // ""')
name=$(echo "$JSON_INPUT" | jq -r '.name // ""')
email=$(echo "$JSON_INPUT" | jq -r '.email // ""')
company=$(echo "$JSON_INPUT" | jq -r '.company // ""')
details=$(echo "$JSON_INPUT" | jq -r '.details // ""')
minApiReqInt=$(echo "$JSON_INPUT" | jq -r '.minApiReqInt // ""')

# --- Parse JDBC URL for psql connection ---

if [ -n "$correlation_id" ] && [ -n "$result_table" ]; then
  if echo "$db_url" | grep -qE 'jdbc:postgresql://[^:/]+/'; then
    # No port specified — use default
    pg_host=$(echo "$db_url" | sed -E 's|jdbc:postgresql://([^/]+)/.*|\1|')
    pg_port="5432"
  else
    pg_host=$(echo "$db_url" | sed -E 's|jdbc:postgresql://([^:]+):.*|\1|')
    pg_port=$(echo "$db_url" | sed -E 's|jdbc:postgresql://[^:]+:([0-9]+)/.*|\1|')
  fi
  pg_dbname=$(echo "$db_url" | sed -E 's|jdbc:postgresql://[^/]+/([^?]+).*|\1|')
  pg_sslmode=$(echo "$db_url" | sed -nE 's|.*[?&]sslmode=([^&]+).*|\1|p')

  if [ -z "$pg_host" ] || [ -z "$pg_port" ] || [ -z "$pg_dbname" ]; then
    error_exit "Failed to parse JDBC URL for psql connection: $db_url"
  fi
  if ! echo "$pg_port" | grep -qE '^[0-9]+$'; then
    error_exit "Failed to extract valid port from JDBC URL: $db_url"
  fi
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
# Do not use exec — the EXIT trap must fire to clean up data-sources.xml

# shellcheck disable=SC2206
JAVA_OPTS_ARRAY=(${JAVA_OPTS:-})

jar_stderr_file=$(mktemp)
jar_exit_code=0
jar_output=$(java ${JAVA_OPTS_ARRAY[@]+"${JAVA_OPTS_ARRAY[@]}"} \
  -cp "/app/api-key-cli.jar:/app/postgresql.jar" \
  org.onebusaway.cli.apikey.ApiKeyCliMain \
  "${ARGS[@]}" 2>"$jar_stderr_file") || jar_exit_code=$?
jar_stderr=$(cat "$jar_stderr_file")
rm -f "$jar_stderr_file"

# --- Write result to table if correlation_id is set ---
# Reporting failures must not kill the script after the JAR has already run

if [ -n "$correlation_id" ] && [ -n "$result_table" ]; then
  if ! ensure_result_table; then
    echo "ERROR: Failed to create/verify result table '${result_table}'. Result will not be written." >&2
  else
    # Purge result rows older than 24 hours to prevent unbounded table growth
    run_psql -c "DELETE FROM \"${result_table}\" WHERE created_at < NOW() - INTERVAL '24 hours';" 2>&1 || echo "WARNING: Orphan row cleanup failed" >&2

    if [ "$jar_exit_code" -eq 0 ]; then
      # The JAR may print non-JSON warning lines to stdout before the JSON payload.
      # Extract just the JSON object/array for storage.
      jar_json=$(echo "$jar_output" | sed -n '/^[[:space:]]*[{\[]/,$p')
      if [ -z "$jar_json" ]; then
        jar_json="$jar_output"
      fi
      if ! write_result "succeeded" "$jar_json" ""; then
        echo "ERROR: Failed to write result to ${result_table} for correlation_id=${correlation_id}" >&2
      fi
    else
      if ! write_result "failed" "" "JAR exited with code $jar_exit_code: $jar_stderr"; then
        echo "ERROR: Failed to write result to ${result_table} for correlation_id=${correlation_id}" >&2
      fi
    fi
  fi
fi

# Always echo to stdout/stderr for Render dashboard visibility (duplicates DB result intentionally)
echo "$jar_output"
if [ -n "$jar_stderr" ]; then
  echo "$jar_stderr" >&2
fi
exit "$jar_exit_code"
