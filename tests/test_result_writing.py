"""Tests for result-writing behavior (with mocked java/psql)."""

import json
import stat

from conftest import run_entrypoint


def _setup_mocks(bin_dir, *, java_stdout="[]", java_stderr="", java_exit=0,
                 psql_exit=0):
    """Create mock java and psql in bin_dir. Returns path to psql SQL log."""
    java = bin_dir / "java"
    lines = ["#!/usr/bin/env bash"]
    if java_stdout:
        lines.append(f"printf '%s' '{java_stdout}'")
    if java_stderr:
        lines.append(f"printf '%s' '{java_stderr}' >&2")
    lines.append(f"exit {java_exit}")
    java.write_text("\n".join(lines) + "\n")
    java.chmod(java.stat().st_mode | stat.S_IEXEC)

    psql_sql_log = bin_dir / "psql_sql.log"
    psql = bin_dir / "psql"
    # Capture SQL from both -c flag and stdin
    psql.write_text(
        "#!/usr/bin/env bash\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  case $1 in\n"
        f"    -c) echo \"$2\" >> '{psql_sql_log}'; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        f"cat >> '{psql_sql_log}'\n"
        f"exit {psql_exit}\n"
    )
    psql.chmod(psql.stat().st_mode | stat.S_IEXEC)
    return psql_sql_log


def _base_input(**overrides):
    data = {
        "action": "list",
        "db_url": "jdbc:postgresql://h:5432/d",
        "db_user": "u",
        "db_pass": "p",
        "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
        "result_table": "api_key_results",
    }
    data.update(overrides)
    return json.dumps(data)


class TestResultWriting:
    def test_success_writes_succeeded(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        psql_log = _setup_mocks(bin_dir, java_stdout='{"keys":[]}')

        stdout, stderr, rc = run_entrypoint(_base_input(), bin_dir=bin_dir)
        assert rc == 0

        sql = psql_log.read_text()
        assert "INSERT INTO" in sql
        assert "'succeeded'" in sql
        assert "550e8400-e29b-41d4-a716-446655440000" in sql

    def test_jar_failure_writes_failed(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        psql_log = _setup_mocks(bin_dir, java_exit=1, java_stderr="DB error")

        stdout, stderr, rc = run_entrypoint(_base_input(), bin_dir=bin_dir)
        assert rc == 1

        sql = psql_log.read_text()
        assert "'failed'" in sql
        assert "DB error" in sql

    def test_stdout_always_printed(self, tmp_path):
        """JAR output should appear on stdout even when writing to table."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _setup_mocks(bin_dir, java_stdout='{"keys":["abc"]}')

        stdout, _, rc = run_entrypoint(_base_input(), bin_dir=bin_dir)
        assert rc == 0
        assert '{"keys":["abc"]}' in stdout

    def test_no_table_write_without_correlation_id(self, tmp_path):
        """Without correlation_id, no psql calls should happen."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        psql_log = _setup_mocks(bin_dir, java_stdout='[]')

        input_data = {
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
        }
        stdout, _, rc = run_entrypoint(json.dumps(input_data), bin_dir=bin_dir)
        assert rc == 0
        # psql should not have been called at all
        assert not psql_log.exists()

    def test_create_table_ddl_sent(self, tmp_path):
        """ensure_result_table should send CREATE TABLE IF NOT EXISTS."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        psql_log = _setup_mocks(bin_dir, java_stdout='[]')

        run_entrypoint(_base_input(), bin_dir=bin_dir)
        sql = psql_log.read_text()
        assert "CREATE TABLE IF NOT EXISTS" in sql
        assert "api_key_results" in sql

    def test_cleanup_delete_sent(self, tmp_path):
        """Old rows should be cleaned up."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        psql_log = _setup_mocks(bin_dir, java_stdout='[]')

        run_entrypoint(_base_input(), bin_dir=bin_dir)
        sql = psql_log.read_text()
        assert "DELETE FROM" in sql
        assert "24 hours" in sql


class TestPsqlFailureResilience:
    def test_psql_failure_does_not_kill_script(self, tmp_path):
        """If psql fails, the script should still exit with the JAR's exit code."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _setup_mocks(bin_dir, java_stdout='{"keys":[]}', psql_exit=1)

        stdout, stderr, rc = run_entrypoint(_base_input(), bin_dir=bin_dir)
        # Should exit 0 (JAR succeeded) despite psql failure
        assert rc == 0
        # JAR output should still be printed
        assert '{"keys":[]}' in stdout
        # Should warn about the psql failure
        assert "ERROR" in stderr or "Failed" in stderr
