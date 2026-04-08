#!/usr/bin/env bash
set -euo pipefail

SECURE_TMPDIR=""

cleanup() {
  if [ -n "$SECURE_TMPDIR" ]; then
    rm -rf "$SECURE_TMPDIR"
  fi
}
trap cleanup EXIT

error_exit() {
  echo "{\"success\":false,\"error\":\"$1\"}"
  exit 1
}

# Validate input
if [ $# -lt 1 ]; then
  error_exit "Usage: entrypoint.sh '<json_blob>'"
fi

JSON_INPUT="$1"

# Validate JSON
if ! echo "$JSON_INPUT" | jq empty 2>/dev/null; then
  error_exit "Invalid JSON input"
fi

# Parse required fields
action=$(echo "$JSON_INPUT" | jq -r '.action // empty')
db_url=$(echo "$JSON_INPUT" | jq -r '.db_url // empty')
db_user=$(echo "$JSON_INPUT" | jq -r '.db_user // empty')
db_pass=$(echo "$JSON_INPUT" | jq -r '.db_pass // empty')

for field in action db_url db_user db_pass; do
  if [ -z "${!field}" ]; then
    error_exit "Missing required field: $field"
  fi
done

# Validate action
case "$action" in
  create|list|get|update|delete) ;;
  *) error_exit "Invalid action: $action. Must be one of: create, list, get, update, delete" ;;
esac

# Parse optional fields
key=$(echo "$JSON_INPUT" | jq -r '.key // empty')
name=$(echo "$JSON_INPUT" | jq -r '.name // empty')
email=$(echo "$JSON_INPUT" | jq -r '.email // empty')
company=$(echo "$JSON_INPUT" | jq -r '.company // empty')
details=$(echo "$JSON_INPUT" | jq -r '.details // empty')
minApiReqInt=$(echo "$JSON_INPUT" | jq -r '.minApiReqInt // empty')

# XML-escape a string
xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  s="${s//\"/&quot;}"
  s="${s//\'/&apos;}"
  echo "$s"
}

# Create secure temp directory
SECURE_TMPDIR=$(mktemp -d)
chmod 700 "$SECURE_TMPDIR"

# Generate data-sources.xml with escaped values
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

# Build CLI arguments
ARGS=("$action")

[ -n "$key" ] && ARGS+=(-k "$key")
[ -n "$name" ] && ARGS+=(-n "$name")
[ -n "$email" ] && ARGS+=(-e "$email")
[ -n "$company" ] && ARGS+=(-o "$company")
[ -n "$details" ] && ARGS+=(-d "$details")
[ -n "$minApiReqInt" ] && ARGS+=(-m "$minApiReqInt")

ARGS+=(-c "$SECURE_TMPDIR/data-sources.xml" -j)

# Execute the JAR with PostgreSQL driver on classpath
# shellcheck disable=SC2206
JAVA_OPTS_ARRAY=(${JAVA_OPTS:-})
exec java "${JAVA_OPTS_ARRAY[@]}" \
  -cp "/app/api-key-cli.jar:/app/postgresql.jar" \
  org.onebusaway.cli.apikey.ApiKeyCliMain \
  "${ARGS[@]}"
