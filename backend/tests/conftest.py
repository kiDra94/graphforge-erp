"""Central fixtures for the API tests.

pytest finds this file by itself — it is never imported. Everything defined here is
available to the tests in `tests/api/` as a parameter under its function name.

Deliberately NOT here: a stand-in for the Neo4j session of the unit tests. Those tests live
next to the logic they cover (`core/`, `domains/*/`) and never see this file — and their
one-line `AsyncMock()` reads better at the place of the test than a fixture in another file
would.

**What the API tests are for.** They send a real HTTP request through the real app:
middleware, routing, Pydantic validation, the role dependencies and the global exception
handlers all run. Only the service layer is mocked away, and the database with it. What they
check is therefore the layer the unit tests cannot reach — that a route is registered under
the path the specification names, that a missing role ends in a 403 rather than a 500, and
that every error leaves the building in the same shape.

**What they are not for.** No Cypher runs here, so nothing about the correctness of a query
can be proven. That belongs to a test against a real database.
"""

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx2 import ASGITransport, AsyncClient

from core.database import get_db_session
from core.security import create_access_token
from main import app


@pytest.fixture
def mock_db_session() -> AsyncMock:
    """Stand-in for the Neo4j session in the API tests.

    Put into `app.dependency_overrides` by `async_client`, where it replaces the real
    database connection. It is deliberately an empty placeholder: API tests mock the
    service layer and never touch the session. Its only job is to let
    `Depends(get_db_session)` return something without opening a driver.

    Returns:
        AsyncMock: An inert replacement for the AsyncSession.
    """
    return AsyncMock()


@pytest.fixture
def role_token() -> Callable[..., str]:
    """Factory for JWTs with freely chosen roles, for tests of the role checks.

    Replaces `admin_token()`/`sales_token()`-style helpers that would otherwise be rebuilt
    in every domain: `role_token("Sales", "Admin")` produces a token with both roles,
    `role_token()` one without any role.

    Returns:
        Callable[..., str]: Callable with any number of roles as positional arguments and
            optional `sub`/`email`/`name` overrides.
    """
    def _create(
        *roles: str,
        sub: str = "1",
        email: str = "test@acme.example",
        name: str = "Test User",
    ) -> str:
        return create_access_token({
            "sub": sub,
            "email": email,
            "name": name,
            "roles": list(roles),
        })

    return _create


@pytest.fixture
def auth(role_token) -> Callable[..., dict[str, str]]:
    """Builds the Authorization header for a token with the given roles.

    Saves the `{"Authorization": f"Bearer {role_token(...)}"}` line in every single test.

    Returns:
        Callable[..., dict[str, str]]: Callable with any number of roles, returning the
            ready-made header dictionary.
    """
    def _header(*roles: str, **overrides) -> dict[str, str]:
        return {"Authorization": f"Bearer {role_token(*roles, **overrides)}"}

    return _header


@pytest_asyncio.fixture
async def async_client(mock_db_session: AsyncMock):
    """HTTP client against the FastAPI app, without a network and without a database.

    `ASGITransport` speaks to the app object directly. The request passes fully through
    middleware, routing, Pydantic validation and the global exception handlers — only
    in-process, without a running server.

    The database access is cut off through `app.dependency_overrides`. That entry MUST
    disappear after the test: `dependency_overrides` hangs off the global app object, and an
    entry left behind would leak into the next test and cause a failure whose cause lies
    elsewhere. The cleanup therefore stands behind the `yield` — where it also runs when the
    test aborts with an exception.

    Only the own key is removed rather than `.clear()`, so a test that additionally
    overrides another dependency stays untouched by this.

    Yields:
        AsyncClient: The ready-to-use test client.
    """
    app.dependency_overrides[get_db_session] = lambda: mock_db_session

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    app.dependency_overrides.pop(get_db_session, None)
