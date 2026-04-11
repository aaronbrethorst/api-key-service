"""Helper functions for integration tests."""

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = str(REPO_ROOT / "docker-compose.test.yml")

DB_HOST = "localhost"
DB_PORT = 15432
DB_USER = "testuser"
DB_PASS = "testpass"
DB_NAME = "oba_test"

# JDBC URL as seen from inside the compose network
JDBC_URL = "jdbc:postgresql://postgres:5432/oba_test"


def run_service(input_dict):
    """Run the api-key-service container with the given JSON input.

    Returns (stdout, stderr, returncode).
    """
    result = subprocess.run(
        [
            "docker", "compose", "-f", COMPOSE_FILE,
            "run", "--rm", "api-key-service",
            json.dumps(input_dict),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    return result.stdout, result.stderr, result.returncode


def parse_json_output(stdout):
    """Extract and parse JSON from stdout, ignoring WARNING/log lines."""
    # The JAR prints WARNING lines to stdout before the JSON.
    # Find the first { or [ and parse from there.
    match = re.search(r'[\[{]', stdout)
    if not match:
        raise ValueError(f"No JSON found in output: {stdout!r}")
    try:
        return json.loads(stdout[match.start():])
    except json.JSONDecodeError as e:
        raise ValueError(
            f"JSON parse failed: {e}\nRaw stdout: {stdout!r}"
        ) from e


def make_input(action, **kwargs):
    """Build a standard JSON input dict for the service."""
    data = {
        "action": action,
        "db_url": JDBC_URL,
        "db_user": DB_USER,
        "db_pass": DB_PASS,
    }
    data.update(kwargs)
    return data
