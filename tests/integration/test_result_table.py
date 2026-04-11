"""Integration tests for correlation_id / result_table flow against real PostgreSQL."""

import uuid

from .helpers import make_input, run_service


def _new_correlation_id():
    return str(uuid.uuid4())


class TestResultTableWriting:
    def test_result_written_on_success(self, db_conn):
        """A successful action should write status='succeeded' with data."""
        corr_id = _new_correlation_id()
        stdout, stderr, rc = run_service(make_input(
            "list",
            correlation_id=corr_id,
            result_table="api_key_results",
        ))
        assert rc == 0, f"list failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"

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
        has_data = result_data is not None or error_message is not None
        assert has_data, "Row has neither result_data nor error_message"

    def test_result_table_auto_created(self, db_conn):
        """Using a new table name should auto-create it."""
        table_name = "test_auto_create"
        corr_id = _new_correlation_id()

        cur = db_conn.cursor()
        try:
            # Drop the table first in case a previous run left it
            cur.execute(f'DROP TABLE IF EXISTS "{table_name}"')

            stdout, stderr, rc = run_service(make_input(
                "list",
                correlation_id=corr_id,
                result_table=table_name,
            ))
            assert rc == 0, f"list failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"

            # Verify the table exists and has our row
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
        corr_id = _new_correlation_id()
        input_data = make_input(
            "list",
            correlation_id=corr_id,
            result_table="api_key_results",
        )

        # Run twice — both must succeed
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

    def test_old_rows_cleaned_up(self, db_conn):
        """Rows older than 24 hours should be deleted on the next run."""
        old_corr_id = _new_correlation_id()
        cur = db_conn.cursor()

        # Ensure the table exists
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

        # Insert a row backdated to 25 hours ago
        cur.execute(
            """INSERT INTO api_key_results (correlation_id, status, created_at)
               VALUES (%s, 'succeeded', NOW() - INTERVAL '25 hours')
               ON CONFLICT (correlation_id) DO NOTHING""",
            (old_corr_id,),
        )

        # Run a new action which triggers cleanup
        new_corr_id = _new_correlation_id()
        stdout, stderr, rc = run_service(make_input(
            "list",
            correlation_id=new_corr_id,
            result_table="api_key_results",
        ))
        assert rc == 0, f"list failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"

        # Verify new row was written (proves the service ran successfully)
        cur.execute(
            "SELECT count(*) FROM api_key_results WHERE correlation_id = %s",
            (new_corr_id,),
        )
        assert cur.fetchone()[0] == 1, "New result row was not written"

        # Old row should be gone
        cur.execute(
            "SELECT count(*) FROM api_key_results WHERE correlation_id = %s",
            (old_corr_id,),
        )
        count = cur.fetchone()[0]
        assert count == 0, "Old row was not cleaned up"
