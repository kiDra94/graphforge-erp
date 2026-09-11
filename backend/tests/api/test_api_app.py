"""API tests of the application frame — everything that belongs to no single domain.

What is checked here is what `main.py` assembles and what therefore cannot be proven in any
domain: that every error leaves the building in the same shape, that the request id reaches
the client, and that the documentation sits under `/api` where the reverse proxy can reach
it.

The one answer shape is the point of the file. A client that has to know three of them
(`message`, `detail`, and a list of objects on a 422) ends up rendering raw JSON somewhere.
"""

import pytest

from core.exceptions import BusinessLogicError, DatabaseError, NotFoundError

SERVICE = "domains.catalog.router_catalog.ProductService"


# ==========================================
# Health and documentation
# ==========================================

@pytest.mark.asyncio
async def test_the_root_endpoint_needs_no_token(async_client):
    """The container health check calls it, and a health check must not need a token."""
    response = await async_client.get("/")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_the_documentation_sits_under_api(async_client):
    """Behind the reverse proxy only /api reaches the backend. Under the default paths the
    Swagger interface would land at the frontend."""
    for path in ("/api/docs", "/api/redoc", "/api/openapi.json"):
        assert (await async_client.get(path)).status_code == 200


@pytest.mark.asyncio
async def test_the_default_documentation_paths_are_gone(async_client):
    """Were they still served, two URLs would answer the same thing and only one of them
    would work behind the proxy."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await async_client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_the_openapi_document_describes_every_domain(async_client):
    """A router that was not registered in main.py is invisible — and a domain whose tests
    all pass can still be unreachable."""
    spec = (await async_client.get("/api/openapi.json")).json()
    tags = {tag for path in spec["paths"].values()
            for operation in path.values()
            for tag in operation.get("tags", [])}

    assert {
        "IAM", "Products", "Inventory", "Suppliers", "Procurement", "Customers",
        "Documents", "Contracts", "Pricing", "Reports", "Assets", "Notifications",
    } <= tags


# ==========================================
# The request id
# ==========================================

@pytest.mark.asyncio
async def test_every_answer_carries_a_request_id(async_client):
    """It ties a client-side report to the server log lines of exactly that request."""
    response = await async_client.get("/")

    assert len(response.headers["X-Request-ID"]) == 8


@pytest.mark.asyncio
async def test_two_requests_get_different_ids(async_client):
    first = await async_client.get("/")
    second = await async_client.get("/")

    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_an_error_answer_carries_the_id_as_well(async_client, auth):
    """Exactly the case where the id matters: the client reports a failure and the log line
    has to be findable."""
    response = await async_client.get("/api/products/NOPE", headers=auth("Engineering"))

    assert "X-Request-ID" in response.headers


# ==========================================
# One answer shape for every error
# ==========================================

@pytest.mark.asyncio
async def test_an_unknown_path_answers_with_message(async_client):
    """Starlette would answer `detail` here. The overridden handler brings it onto
    `message`, like every other error."""
    response = await async_client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert "message" in response.json()
    assert "detail" not in response.json()


@pytest.mark.asyncio
async def test_a_missing_token_answers_401_with_message(async_client):
    response = await async_client.get("/api/products")

    assert response.status_code == 401
    assert "message" in response.json()


@pytest.mark.asyncio
async def test_a_401_keeps_its_www_authenticate_header(async_client):
    """Without that header the answer is no longer protocol compliant — passing the
    exception's headers through is not a detail."""
    response = await async_client.get("/api/products")

    assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_a_wrong_method_answers_405_with_message(async_client):
    response = await async_client.delete("/")

    assert response.status_code == 405
    assert "message" in response.json()


@pytest.mark.asyncio
async def test_a_schema_violation_answers_422_with_a_readable_text(async_client, auth):
    """FastAPI would answer a list of objects here. A client rendering that raw puts JSON on
    the screen."""
    response = await async_client.post(
        "/api/products", json={"label": "No number"}, headers=auth("Engineering")
    )

    assert response.status_code == 422
    message = response.json()["message"]
    assert isinstance(message, str)
    # The text names every field that is missing, each with its reason — that is what makes
    # it usable in a form without the client parsing anything.
    assert "unit: Field required" in message
    assert "listPrice" in message


@pytest.mark.asyncio
async def test_a_422_names_the_field_but_never_the_value(async_client):
    """Pydantic puts the offending value into every error object. On a login that value is
    the submitted password — it must not end up in the response, in a browser log or in a
    tool that records error responses."""
    response = await async_client.post(
        "/api/auth/login", json={"identifier": "max@acme.example", "passwort": "s3cret!"}
    )

    message = response.json()["message"]
    assert response.status_code == 422
    assert "passwort" in message
    assert "s3cret!" not in message


@pytest.mark.asyncio
async def test_each_business_exception_gets_its_own_status(async_client, auth, monkeypatch):
    """The four exceptions are translated into a status code exactly once, in main.py — a
    router forming its own HTTPException would be the second place the same rule lives."""
    expected = {
        NotFoundError("nope"): 404,
        BusinessLogicError("not allowed"): 400,
        DatabaseError("query failed"): 500,
    }
    for error, status in expected.items():
        async def raising(number, session, _error=error):
            raise _error

        monkeypatch.setattr(f"{SERVICE}.get_product", raising)
        response = await async_client.get("/api/products/ACME-1000", headers=auth("Engineering"))

        assert response.status_code == status
        assert "message" in response.json()


@pytest.mark.asyncio
async def test_a_database_error_does_not_leak_the_query(async_client, auth, monkeypatch):
    """A 500 is the one case where the message is not passed on: it would carry the Cypher
    text and the parameter values to the client."""
    async def raising(number, session):
        raise DatabaseError("MATCH (p:Product {number: 'ACME-1000'}) RETURN p — syntax error")

    monkeypatch.setattr(f"{SERVICE}.get_product", raising)

    response = await async_client.get("/api/products/ACME-1000", headers=auth("Engineering"))

    assert response.status_code == 500
    assert "MATCH" not in response.json()["message"]
