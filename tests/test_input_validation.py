"""Tests for entrypoint.sh input validation logic."""

import json

from conftest import run_entrypoint


def _error(stdout):
    """Parse the JSON error response from stdout."""
    return json.loads(stdout)


class TestRequiredFields:
    def test_no_arguments(self):
        """Script with no args should exit with usage error."""
        import subprocess, os
        from conftest import ENTRYPOINT

        result = subprocess.run(
            [str(ENTRYPOINT)],
            capture_output=True, text=True, timeout=10,
        )
        out = _error(result.stdout)
        assert result.returncode == 1
        assert out["success"] is False
        assert "Usage" in out["error"]

    def test_invalid_json(self):
        stdout, _, rc = run_entrypoint("not json")
        out = _error(stdout)
        assert rc == 1
        assert "Invalid JSON" in out["error"]

    def test_missing_action(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "action" in out["error"]

    def test_missing_db_url(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list", "db_user": "u", "db_pass": "p",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "db_url" in out["error"]

    def test_missing_db_user(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d", "db_pass": "p",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "db_user" in out["error"]

    def test_missing_db_pass(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d", "db_user": "u",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "db_pass" in out["error"]


class TestActionValidation:
    def test_invalid_action(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "bogus",
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "Invalid action" in out["error"]

    def test_valid_actions(self, mock_bin):
        """All valid actions should pass validation and reach the JAR."""
        bin_dir, _ = mock_bin
        for action in ("create", "list", "get", "update", "delete"):
            _, _, rc = run_entrypoint(
                json.dumps({
                    "action": action,
                    "db_url": "jdbc:postgresql://h:5432/d",
                    "db_user": "u", "db_pass": "p",
                }),
                bin_dir=bin_dir,
            )
            assert rc == 0, f"action={action} should succeed"


class TestCorrelationIdValidation:
    def test_valid_uuid(self, mock_bin):
        bin_dir, _ = mock_bin
        _, _, rc = run_entrypoint(
            json.dumps({
                "action": "list",
                "db_url": "jdbc:postgresql://h:5432/d",
                "db_user": "u", "db_pass": "p",
                "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
                "result_table": "api_key_results",
            }),
            bin_dir=bin_dir,
        )
        assert rc == 0

    def test_invalid_uuid(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
            "correlation_id": "not-a-uuid",
            "result_table": "api_key_results",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "correlation_id" in out["error"]

    def test_uppercase_uuid_rejected(self):
        """UUIDs with uppercase hex digits should be rejected."""
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
            "correlation_id": "550E8400-E29B-41D4-A716-446655440000",
            "result_table": "api_key_results",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "correlation_id" in out["error"]


class TestResultTableValidation:
    def test_valid_table_name(self, mock_bin):
        bin_dir, _ = mock_bin
        _, _, rc = run_entrypoint(
            json.dumps({
                "action": "list",
                "db_url": "jdbc:postgresql://h:5432/d",
                "db_user": "u", "db_pass": "p",
                "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
                "result_table": "api_key_results",
            }),
            bin_dir=bin_dir,
        )
        assert rc == 0

    def test_table_name_with_uppercase(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
            "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
            "result_table": "BadTable",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "result_table" in out["error"]

    def test_table_name_with_sql_injection(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
            "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
            "result_table": "x; DROP TABLE users",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "result_table" in out["error"]

    def test_table_name_starting_with_number(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
            "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
            "result_table": "123table",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "result_table" in out["error"]


class TestPairValidation:
    def test_correlation_id_without_result_table(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
            "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "without" in out["error"]

    def test_result_table_without_correlation_id(self):
        stdout, _, rc = run_entrypoint(json.dumps({
            "action": "list",
            "db_url": "jdbc:postgresql://h:5432/d",
            "db_user": "u", "db_pass": "p",
            "result_table": "api_key_results",
        }))
        out = _error(stdout)
        assert rc == 1
        assert "without" in out["error"]

    def test_neither_field_is_fine(self, mock_bin):
        """Omitting both fields should work (backwards compat)."""
        bin_dir, _ = mock_bin
        _, _, rc = run_entrypoint(
            json.dumps({
                "action": "list",
                "db_url": "jdbc:postgresql://h:5432/d",
                "db_user": "u", "db_pass": "p",
            }),
            bin_dir=bin_dir,
        )
        assert rc == 0
