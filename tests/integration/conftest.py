"""Integration test fixtures — manages docker compose stack and DB connection."""

import subprocess

import psycopg2
import pytest

from . import helpers


@pytest.fixture(scope="session", autouse=True)
def compose_up():
    """Start the docker compose stack before tests, tear down after."""
    # Build the service image
    subprocess.run(
        ["docker", "compose", "-f", helpers.COMPOSE_FILE, "build"],
        check=True,
        cwd=str(helpers.REPO_ROOT),
    )
    # Start only postgres (the service is run on-demand via docker compose run)
    subprocess.run(
        ["docker", "compose", "-f", helpers.COMPOSE_FILE,
         "up", "-d", "--wait", "postgres"],
        check=True,
        cwd=str(helpers.REPO_ROOT),
    )
    yield
    subprocess.run(
        ["docker", "compose", "-f", helpers.COMPOSE_FILE, "down", "-v"],
        cwd=str(helpers.REPO_ROOT),
    )


@pytest.fixture(scope="session")
def db_conn(compose_up):
    """psycopg2 connection to the test database."""
    conn = psycopg2.connect(
        host=helpers.DB_HOST,
        port=helpers.DB_PORT,
        user=helpers.DB_USER,
        password=helpers.DB_PASS,
        dbname=helpers.DB_NAME,
    )
    conn.autocommit = True
    yield conn
    conn.close()
