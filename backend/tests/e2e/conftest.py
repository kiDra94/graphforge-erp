"""Fixtures for the end-to-end tests: the whole stack, driven through a real browser.

Nothing is mocked here. A Neo4j container is filled with the complete `seed.cypher` — the
same demo graph the compose stack and the live demo run on — and the backend runs as its
own uvicorn process against it. Playwright then opens the Swagger UI in Chromium and clicks
through it the way a visitor of the live demo would.

What this proves that the API tests cannot: that the Swagger UI's own JavaScript sends what
the backend expects (the *Authorize* dialog posting a form to `tokenUrl`, the token ending
up in the header of the next request), and that the demo accounts in the seed really sign
in.

Requirements: a running Docker, the Playwright browser (`uv run playwright install
chromium`), and network access, because the Swagger UI loads its scripts from a CDN.
Without Docker the tests are skipped locally and fail in CI, as the integration tests do.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from testcontainers.community.neo4j import Neo4jContainer

BACKEND_DIR = Path(__file__).resolve().parents[2]
SEED_FILE = BACKEND_DIR.parent / "seed.cypher"

# The same image the compose stack runs.
NEO4J_IMAGE = "neo4j:5-community"
TEST_DB_PASSWORD = "e2e-tests"

SERVER_START_TIMEOUT_S = 60


def _database_is_required() -> bool:
    """Whether a missing Docker is an error rather than a skip.

    The same rule as in `tests/integration/conftest.py`: `CI` is set by GitHub Actions,
    `REQUIRE_TEST_DB` reproduces it locally. Repeated rather than imported, because one
    conftest importing another couples two folders pytest treats as unrelated.
    """
    truthy = ("1", "true", "yes")
    return (
        os.getenv("CI", "").lower() in truthy
        or os.getenv("REQUIRE_TEST_DB", "").lower() in truthy
    )


def pytest_collection_modifyitems(config, items):
    """Marks everything below `tests/e2e/` as an end-to-end test."""
    for item in items:
        if "tests/e2e/" in item.nodeid:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def seeded_neo4j() -> Iterator[Neo4jContainer]:
    """A Neo4j container holding the full demo graph from `seed.cypher`.

    The file is loaded with `cypher-shell -f` inside the container — exactly the command the
    `seed` service of docker-compose.yml runs — rather than split into statements here, so
    the test sees the graph the demo sees.

    Yields:
        Neo4jContainer: The running, seeded container.
    """
    container = Neo4jContainer(NEO4J_IMAGE, password=TEST_DB_PASSWORD).with_volume_mapping(
        str(SEED_FILE), "/seed.cypher", "ro"
    )
    try:
        container.start()
    except Exception as error:  # Docker missing, daemon not running, image not pullable
        message = f"Neo4j test container could not be started ({type(error).__name__}: {error})"
        if _database_is_required():
            raise RuntimeError(f"{message} — skipping is not allowed under CI.") from error
        pytest.skip(message)

    try:
        exit_code, output = container.exec(
            ["cypher-shell", "-u", "neo4j", "-p", TEST_DB_PASSWORD, "-f", "/seed.cypher"]
        )
        if exit_code != 0:
            # Bytes for a plain exec; the declared type also allows a stream.
            text = output if isinstance(output, bytes) else b"".join(output)
            raise RuntimeError(f"Loading seed.cypher failed:\n{text.decode(errors='replace')}")
        yield container
    finally:
        container.stop()


def _free_port() -> int:
    """Asks the OS for a port nobody listens on, so parallel runs cannot collide."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def live_server(seeded_neo4j: Neo4jContainer, tmp_path_factory) -> Iterator[str]:
    """The backend as a real uvicorn process against the seeded container.

    A separate process instead of a thread: the settings are read from the environment once
    and cached, and the test process carries the throwaway values of the CI job. The child
    gets an environment of its own that points at the container.

    Its output goes to a log file, printed when the server does not come up, because a
    startup error is otherwise invisible.

    Yields:
        str: The base URL, e.g. `http://127.0.0.1:54321`.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("e2e") / "uvicorn.log"
    env = {
        **os.environ,
        "NEO4J_URI": seeded_neo4j.get_connection_url(),
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": TEST_DB_PASSWORD,
        "JWT_SECRET_KEY": "e2e-only-secret-long-enough-for-hs256-signing",
    }

    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=BACKEND_DIR,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while True:
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"The backend did not start:\n{log_path.read_text()}")
            try:
                # The root endpoint is the one without authentication — the health check.
                with urllib.request.urlopen(f"{base_url}/", timeout=1):
                    break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.25)
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="session")
def base_url(live_server: str) -> str:
    """Points Playwright's `page.goto("/...")` at the live server.

    Overrides the `base_url` fixture of pytest-base-url, which pytest-playwright reads.
    """
    return live_server
