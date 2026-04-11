"""Tests for JDBC URL parsing into psql connection params."""

import json
import stat

from conftest import run_entrypoint


def _make_mocks(bin_dir, psql_log_path):
    """Create mock java (exits 0, outputs JSON) and psql (logs its flags)."""
    java = bin_dir / "java"
    java.write_text("#!/usr/bin/env bash\nprintf '%s' '[]'\n")
    java.chmod(java.stat().st_mode | stat.S_IEXEC)

    # Mock psql that parses the connection URI to extract connection params
    psql = bin_dir / "psql"
    psql.write_text(
        "#!/usr/bin/env bash\n"
        "# Extract connection params from postgresql:// URI (first non-flag arg)\n"
        "for arg in \"$@\"; do\n"
        "  case $arg in\n"
        "    postgresql://*)\n"
        "      uri=\"$arg\"\n"
        "      # Strip scheme\n"
        "      rest=\"${uri#postgresql://}\"\n"
        "      # user:pass@host:port/db?params\n"
        "      userpass=\"${rest%%@*}\"\n"
        "      hostpart=\"${rest#*@}\"\n"
        "      hostport=\"${hostpart%%/*}\"\n"
        "      dbparams=\"${hostpart#*/}\"\n"
        "      db=\"${dbparams%%\\?*}\"\n"
        "      host=\"${hostport%%:*}\"\n"
        "      port=\"${hostport#*:}\"\n"
        f"      echo \"HOST=$host\" >> '{psql_log_path}'\n"
        f"      echo \"PORT=$port\" >> '{psql_log_path}'\n"
        f"      echo \"DB=$db\" >> '{psql_log_path}'\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
        "cat > /dev/null\n"
    )
    psql.chmod(psql.stat().st_mode | stat.S_IEXEC)


def _run_with_result_table(db_url, bin_dir):
    """Run entrypoint with correlation_id and result_table, return psql log."""
    psql_log = bin_dir / "psql_calls.log"
    _make_mocks(bin_dir, psql_log)

    stdout, stderr, rc = run_entrypoint(
        json.dumps({
            "action": "list",
            "db_url": db_url,
            "db_user": "myuser",
            "db_pass": "mypass",
            "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
            "result_table": "api_key_results",
        }),
        bin_dir=bin_dir,
    )
    log = psql_log.read_text() if psql_log.exists() else ""
    return log, stdout, stderr, rc


class TestJdbcUrlParsing:
    def test_standard_url(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log, _, _, rc = _run_with_result_table(
            "jdbc:postgresql://dbhost:5432/mydb", bin_dir
        )
        assert rc == 0
        assert "HOST=dbhost" in log
        assert "PORT=5432" in log
        assert "DB=mydb" in log

    def test_non_standard_port(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log, _, _, rc = _run_with_result_table(
            "jdbc:postgresql://dbhost:15432/mydb", bin_dir
        )
        assert rc == 0
        assert "PORT=15432" in log

    def test_url_without_port(self, tmp_path):
        """JDBC URLs without explicit port should default to 5432."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log, _, _, rc = _run_with_result_table(
            "jdbc:postgresql://dbhost/mydb", bin_dir
        )
        assert rc == 0
        assert "HOST=dbhost" in log
        assert "PORT=5432" in log
        assert "DB=mydb" in log

    def test_url_with_query_params(self, tmp_path):
        """Query params like ?sslmode=require should be stripped from dbname."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log, _, _, rc = _run_with_result_table(
            "jdbc:postgresql://dbhost:5432/mydb?sslmode=require", bin_dir
        )
        assert rc == 0
        assert "DB=mydb\n" in log

    def test_url_with_multiple_query_params(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log, _, _, rc = _run_with_result_table(
            "jdbc:postgresql://dbhost:5432/mydb?sslmode=require&connect_timeout=10",
            bin_dir,
        )
        assert rc == 0
        assert "DB=mydb\n" in log
