"""Integration tests for the full API key CRUD lifecycle."""

from .helpers import make_input, run_and_parse, run_service


def test_full_lifecycle():
    """Create -> list -> get -> update -> delete an API key."""
    key_value = None

    try:
        result = run_and_parse(
            "create", name="Test Key", email="test@example.com", company="TestCorp",
        )
        assert result["success"] is True
        key_value = result["key"]
        assert key_value
        assert result["contactName"] == "Test Key"
        assert result["contactEmail"] == "test@example.com"
        assert result["contactCompany"] == "TestCorp"

        result = run_and_parse("list")
        assert result["total"] >= 1
        assert key_value in result["keys"]

        result = run_and_parse("get", key=key_value)
        assert result["key"] == key_value
        assert result["contactName"] == "Test Key"
        assert result["contactEmail"] == "test@example.com"
        assert result["contactCompany"] == "TestCorp"

        result = run_and_parse("update", key=key_value, name="Updated Name")
        assert result["success"] is True

        result = run_and_parse("get", key=key_value)
        assert result["contactName"] == "Updated Name"

        result = run_and_parse("delete", key=key_value)
        assert result["success"] is True

        result = run_and_parse("list")
        assert key_value not in result["keys"]

        key_value = None

    finally:
        if key_value is not None:
            run_service(make_input("delete", key=key_value))
