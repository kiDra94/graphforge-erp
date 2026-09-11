"""Fixtures for the integration tests: a real Neo4j in a throwaway container.

The unit and API tests never touch a database, so they cannot prove that a Cypher query
returns the right rows. The tests below this folder can: every one of them runs against a
Neo4j started by Testcontainers, with the same constraints and indexes as the demo data,
and an empty graph at the start of each test.

The container starts once per test run and is removed afterwards. Without Docker the tests
are skipped locally — nobody should need a container for a one-line change — but fail in
CI, where a skipped suite would look like a passing one.
"""

import os
from pathlib import Path

import pytest
import pytest_asyncio
from neo4j import AsyncGraphDatabase
from testcontainers.community.neo4j import Neo4jContainer

# The same image the compose stack runs, so the tests see the same Cypher dialect.
NEO4J_IMAGE = "neo4j:5-community"

# Deliberately not taken from NEO4J_PASSWORD: that variable configures the application,
# and Neo4jContainer would otherwise pick it up as the container's password as well.
TEST_DB_PASSWORD = "integration-tests"

# Constraints and indexes come from the file that also builds the demo database, so the
# tests run against the real schema. The data part of the same file stays out — every test
# builds exactly the graph it needs.
SEED_FILE = Path(__file__).resolve().parents[3] / "seed.cypher"


def _database_is_required() -> bool:
    """Decides whether a missing Docker is an error rather than a skip.

    `CI` is set by GitHub Actions on its own; `REQUIRE_TEST_DB` allows reproducing the
    behaviour locally.

    Returns:
        bool: True when the integration tests must not be skipped.
    """
    truthy = ("1", "true", "yes")
    return (
        os.getenv("CI", "").lower() in truthy
        or os.getenv("REQUIRE_TEST_DB", "").lower() in truthy
    )


def _schema_statements() -> list[str]:
    """Reads the constraint and index statements out of `seed.cypher`.

    Comment lines are dropped and the rest is split at the semicolons; only the statements
    that create a constraint or an index are kept. All of them carry `IF NOT EXISTS`.

    Returns:
        list[str]: The individually executable Cypher statements.
    """
    lines = [
        line
        for line in SEED_FILE.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("//")
    ]
    statements = " ".join(lines).split(";")
    return [
        statement.strip()
        for statement in statements
        if statement.strip().upper().startswith(("CREATE CONSTRAINT", "CREATE INDEX"))
    ]


def pytest_collection_modifyitems(config, items):
    """Marks everything below `tests/integration/` as an integration test.

    Saves the marker on every single test function, and keeps a forgotten marker from
    slipping a database test into a run that is meant to need no database.
    """
    for item in items:
        if "tests/integration/" in item.nodeid:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def neo4j_container():
    """Starts the Neo4j container once per test run and applies the schema.

    Deliberately synchronous: a session-scoped async fixture would need an event loop of its
    own, and the async driver must not outlive the loop it was created in. The synchronous
    driver sidesteps that — for a handful of schema statements the speed does not matter.

    Yields:
        Neo4jContainer: The running container, schema applied.
    """
    container = Neo4jContainer(NEO4J_IMAGE, password=TEST_DB_PASSWORD)
    try:
        container.start()
    except Exception as error:  # Docker missing, daemon not running, image not pullable
        message = f"Neo4j test container could not be started ({type(error).__name__}: {error})"
        if _database_is_required():
            raise RuntimeError(f"{message} — skipping is not allowed under CI.") from error
        pytest.skip(message)

    try:
        with container.get_driver() as driver, driver.session() as session:
            for statement in _schema_statements():
                session.run(statement)  # type: ignore[arg-type]
        yield container
    finally:
        container.stop()


@pytest_asyncio.fixture
async def neo4j_session(neo4j_container: Neo4jContainer):
    """A real Neo4j session against the test container, on an empty graph.

    The graph is emptied before and after every test. Emptying it *before* is not
    overcautious: a run aborted in the middle of a test would otherwise leave nodes behind,
    and the next test would fail on a unique constraint in a way that looks like a bug.

    The driver is created per test because the async Neo4j driver binds to the event loop it
    was created in, and pytest-asyncio gives every test a loop of its own.

    Yields:
        AsyncSession: An open session against the empty test graph.
    """
    driver = AsyncGraphDatabase.driver(
        neo4j_container.get_connection_url(),
        auth=(neo4j_container.username, neo4j_container.password),
    )
    try:
        async with driver.session() as session:
            await session.run("MATCH (n) DETACH DELETE n")
            yield session
            await session.run("MATCH (n) DETACH DELETE n")
    finally:
        await driver.close()
