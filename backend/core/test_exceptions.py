"""Unit tests of the global exception handlers against a minimal test app.

Checked here: that every business exception lands on the right HTTP status, that the
DatabaseError handler leaks no internal details, and that **every** error response
carries the same key `message` — the two FastAPI raises itself included.
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .exceptions import (
    BusinessLogicError,
    DatabaseError,
    DuplicateKeyError,
    NotFoundError,
    business_logic_handler,
    database_error_handler,
    duplicate_key_handler,
    http_exception_handler,
    not_found_handler,
    validation_error_handler,
)


class _Login(BaseModel):
    """Minimal schema with two required fields, to trigger a 422."""
    email: str
    password: str


class _Line(BaseModel):
    """Nested schema — exercises the field path across several levels."""
    quantity: int


class _Document(BaseModel):
    lines: list[_Line]


@pytest.fixture
def client():
    app = FastAPI()
    app.add_exception_handler(NotFoundError, not_found_handler)
    app.add_exception_handler(DuplicateKeyError, duplicate_key_handler)
    app.add_exception_handler(BusinessLogicError, business_logic_handler)
    app.add_exception_handler(DatabaseError, database_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)

    @app.get("/not-found")
    async def raise_not_found():
        raise NotFoundError("Product does not exist")

    @app.get("/duplicate-key")
    async def raise_duplicate_key():
        raise DuplicateKeyError("Product number already taken")

    @app.get("/business-logic")
    async def raise_business_logic():
        raise BusinessLogicError("Quantity must be greater than 0")

    @app.get("/database-error")
    async def raise_database_error():
        raise DatabaseError("Connection refused at neo4j://internal-host:7687")

    @app.post("/login")
    async def login(data: _Login):
        return {"ok": True}

    @app.post("/document")
    async def document(data: _Document):
        return {"ok": True}

    @app.get("/not-signed-in")
    async def raise_unauthorized():
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/missing-role")
    async def raise_forbidden():
        raise HTTPException(status_code=403, detail="Role 'BackOffice' required.")

    return TestClient(app, raise_server_exceptions=False)


def test_not_found_handler_returns_404(client):
    response = client.get("/not-found")

    assert response.status_code == 404
    assert response.json() == {"message": "Product does not exist"}


def test_duplicate_key_handler_returns_409(client):
    response = client.get("/duplicate-key")

    assert response.status_code == 409
    assert response.json() == {"message": "Product number already taken"}


def test_business_logic_handler_returns_400(client):
    response = client.get("/business-logic")

    assert response.status_code == 400
    assert response.json() == {"message": "Quantity must be greater than 0"}


def test_database_error_handler_returns_500_with_generic_message(client):
    response = client.get("/database-error")

    assert response.status_code == 500
    assert response.json() == {"message": "An internal database error occurred."}


def test_database_error_handler_does_not_leak_internal_details(client):
    response = client.get("/database-error")

    assert "neo4j://internal-host:7687" not in response.text
    assert "Connection refused" not in response.text


# --- Validation errors (422) -------------------------------------------------

def test_validation_handler_answers_with_message_instead_of_a_detail_list(client):
    """FastAPI returns `detail` as a list of objects here — that would be a second shape."""
    response = client.post("/login", json={})

    assert response.status_code == 422
    assert "detail" not in response.json()
    assert isinstance(response.json()["message"], str)


def test_validation_handler_names_the_missing_fields(client):
    response = client.post("/login", json={})

    message = response.json()["message"]
    assert "email" in message
    assert "password" in message


def test_validation_handler_does_not_return_the_input(client):
    """The reason this handler exists.

    Pydantic puts the offending value into every error object as `input`. On a login
    without an e-mail address that value is the submitted password in plaintext — and it
    would end up in the response, in the browser log and in every tool that records error
    responses.
    """
    response = client.post("/login", json={"password": "MySecretPassword"})

    assert response.status_code == 422
    assert "MySecretPassword" not in response.text
    assert "email" in response.json()["message"]


def test_validation_handler_renders_nested_field_paths_readably(client):
    """`("body", "lines", 0, "quantity")` becomes `lines.0.quantity`."""
    response = client.post("/document", json={"lines": [{"quantity": "not a number"}]})

    assert response.status_code == 422
    assert "lines.0.quantity" in response.json()["message"]


def test_validation_handler_drops_the_transport_prefix(client):
    """`body` names the transport layer, not the field the user filled in."""
    response = client.post("/login", json={})

    assert "body." not in response.json()["message"]


# --- Errors raised by FastAPI itself (401, 403) ------------------------------

def test_http_exception_handler_answers_with_message(client):
    response = client.get("/missing-role")

    assert response.status_code == 403
    assert response.json() == {"message": "Role 'BackOffice' required."}


def test_http_exception_handler_preserves_the_headers(client):
    """A 401 without `WWW-Authenticate` is no longer protocol compliant."""
    response = client.get("/not-signed-in")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"message": "Missing or invalid token."}


def test_every_error_response_carries_the_same_key(client):
    """The actual point: a client only ever has to know `message`."""
    responses = [
        client.get("/not-found"),
        client.get("/duplicate-key"),
        client.get("/business-logic"),
        client.get("/database-error"),
        client.get("/missing-role"),
        client.get("/not-signed-in"),
        client.post("/login", json={}),
    ]

    for response in responses:
        assert "message" in response.json(), response.text
        assert "detail" not in response.json(), response.text
