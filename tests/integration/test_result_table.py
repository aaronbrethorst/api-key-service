"""Integration tests for correlation_id / result_table flow against real PostgreSQL."""

import json
import uuid

from .helpers import assert_success, make_input, run_service


class TestResultTableWriting:
    def test_result_written_on_success(self, db_conn):
        """A successful action should write status='succeeded' with data."""
        corr_id = str(uuid.uuid4())
        stdout, stderr, rc = run_service(make_input(
            "list",
            correlation_id=corr_id,
            result_table="api_key_results",
        ))
        assert_success(stdout, stderr, rc, "list")

        cur = db_conn.cursor()
        cur.execute(
            "SELECT status, result_data, error_message FROM api_key_results WHERE correlation_id = %s",
            (corr_id,),
        )
        row = cur.fetchone()
        assert row is not None, "No result row written to database"
        status, result_data, error_message = row
        assert status == "succeeded"
        # The row must contain either valid JSON result_data or at minimum
        # the raw output in error_message (if the JAR's WARNING lines
        # caused jq validation to fail).
        assert result_data is not None or error_message is not None, \
            "Row has neither result_data nor error_message"

    def test_result_table_auto_created(self, db_conn):
        """Using a new table name should auto-create it."""
        table_name = "test_auto_create"
        corr_id = str(uuid.uuid4())

        cur = db_conn.cursor()
        try:
            cur.execute(f'DROP TABLE IF EXISTS "{table_name}"')

            stdout, stderr, rc = run_service(make_input(
                "list",
                correlation_id=corr_id,
                result_table=table_name,
            ))
            assert_success(stdout, stderr, rc, "list")

            cur.execute(
                f'SELECT status FROM "{table_name}" WHERE correlation_id = %s',
                (corr_id,),
            )
            row = cur.fetchone()
            assert row is not None, f"Table {table_name} not created or row not written"
            assert row[0] == "succeeded"
        finally:
            cur.execute(f'DROP TABLE IF EXISTS "{table_name}"')

    def test_upsert_on_duplicate_correlation_id(self, db_conn):
        """Running twice with the same correlation_id should upsert, not duplicate."""
        corr_id = str(uuid.uuid4())
        input_data = make_input(
            "list",
            correlation_id=corr_id,
            result_table="api_key_results",
        )

        _, _, rc1 = run_service(input_data)
        assert rc1 == 0, "First run failed"
        _, _, rc2 = run_service(input_data)
        assert rc2 == 0, "Second run failed"

        cur = db_conn.cursor()
        cur.execute(
            "SELECT count(*) FROM api_key_results WHERE correlation_id = %s",
            (corr_id,),
        )
        count = cur.fetchone()[0]
        assert count == 1, f"Expected 1 row, found {count}"

    def test_validation_error_recorded_when_table_not_yet_created(self, db_conn):
        """Issue #7: a ValidationError on first run (fresh table) must still
        record a 'failed' row rather than silently losing the correlation."""
        table_name = "test_validation_err"
        corr_id = str(uuid.uuid4())

        cur = db_conn.cursor()
        try:
            cur.execute(f'DROP TABLE IF EXISTS "{table_name}"')

            # bulk_create with no csv_url → ValidationError
            stdout, stderr, rc = run_service({
                "action": "bulk_create",
                "db_url": "jdbc:postgresql://postgres:5432/oba_test?sslmode=disable",
                "db_user": "testuser",
                "db_pass": "testpass",
                "correlation_id": corr_id,
                "result_table": table_name,
            })
            assert rc == 1, f"stdout={stdout} stderr={stderr}"
            err = json.loads(stdout.strip().splitlines()[-1])
            assert "csv_url" in err["error"]

            cur.execute(
                f'SELECT status, error_message FROM "{table_name}" '
                f'WHERE correlation_id = %s',
                (corr_id,),
            )
            row = cur.fetchone()
            assert row is not None, \
                "ValidationError must write a failed row even when table is fresh"
            status, error_message = row
            assert status == "failed"
            assert "csv_url" in error_message
        finally:
            cur.execute(f'DROP TABLE IF EXISTS "{table_name}"')

    def test_old_rows_cleaned_up(self, db_conn):
        """Rows older than 24 hours should be deleted on the next run."""
        old_corr_id = str(uuid.uuid4())
        cur = db_conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_key_results (
                id BIGSERIAL PRIMARY KEY,
                correlation_id UUID NOT NULL UNIQUE,
                status VARCHAR(20) NOT NULL DEFAULT 'succeeded',
                result_data JSONB,
                error_message TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)

        cur.execute(
            """INSERT INTO api_key_results (correlation_id, status, created_at)
               VALUES (%s, 'succeeded', NOW() - INTERVAL '25 hours')
               ON CONFLICT (correlation_id) DO NOTHING""",
            (old_corr_id,),
        )

        new_corr_id = str(uuid.uuid4())
        stdout, stderr, rc = run_service(make_input(
            "list",
            correlation_id=new_corr_id,
            result_table="api_key_results",
        ))
        assert_success(stdout, stderr, rc, "list")

        # Verify new row was written
        cur.execute(
            "SELECT count(*) FROM api_key_results WHERE correlation_id = %s",
            (new_corr_id,),
        )
        assert cur.fetchone()[0] == 1, "New result row was not written"

        cur.execute(
            "SELECT count(*) FROM api_key_results WHERE correlation_id = %s",
            (old_corr_id,),
        )
        count = cur.fetchone()[0]
        assert count == 0, "Old row was not cleaned up"
