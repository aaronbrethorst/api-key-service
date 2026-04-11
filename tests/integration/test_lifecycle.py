"""Integration tests for the full API key CRUD lifecycle."""

from .helpers import make_input, parse_json_output, run_service


def test_full_lifecycle():
    """Create -> list -> get -> update -> delete an API key."""
    key_value = None

    try:
        # --- Create ---
        stdout, stderr, rc = run_service(make_input(
            "create",
            name="Test Key",
            email="test@example.com",
            company="TestCorp",
        ))
        assert rc == 0, f"create failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"
        result = parse_json_output(stdout)
        assert result["success"] is True
        key_value = result["key"]
        assert key_value
        assert result["contactName"] == "Test Key"
        assert result["contactEmail"] == "test@example.com"
        assert result["contactCompany"] == "TestCorp"

        # --- List ---
        stdout, stderr, rc = run_service(make_input("list"))
        assert rc == 0, f"list failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"
        result = parse_json_output(stdout)
        assert result["total"] >= 1
        assert key_value in result["keys"]

        # --- Get ---
        stdout, stderr, rc = run_service(make_input("get", key=key_value))
        assert rc == 0, f"get failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"
        result = parse_json_output(stdout)
        assert result["key"] == key_value
        assert result["contactName"] == "Test Key"
        assert result["contactEmail"] == "test@example.com"
        assert result["contactCompany"] == "TestCorp"

        # --- Update ---
        stdout, stderr, rc = run_service(make_input(
            "update",
            key=key_value,
            name="Updated Name",
        ))
        assert rc == 0, f"update failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"
        result = parse_json_output(stdout)
        assert result["success"] is True

        # --- Verify update ---
        stdout, stderr, rc = run_service(make_input("get", key=key_value))
        assert rc == 0, f"get after update failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"
        result = parse_json_output(stdout)
        assert result["contactName"] == "Updated Name"

        # --- Delete ---
        stdout, stderr, rc = run_service(make_input("delete", key=key_value))
        assert rc == 0, f"delete failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"
        result = parse_json_output(stdout)
        assert result["success"] is True

        # --- Verify deletion ---
        stdout, stderr, rc = run_service(make_input("list"))
        assert rc == 0, f"list after delete failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"
        result = parse_json_output(stdout)
        assert key_value not in result["keys"]

        key_value = None  # Cleanup not needed — delete succeeded

    finally:
        # Clean up the key if a step after create failed
        if key_value is not None:
            run_service(make_input("delete", key=key_value))
