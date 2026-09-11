"""API tests of the WebSocket endpoint — through the real ASGI app.

The unit tests in `domains/realtime/` speak to the endpoint function with a stand-in socket.
What they cannot show is how the refusal looks from outside: because the socket is closed
before `accept()`, the handshake never completes and the client sees an HTTP 403 rather than
a close code. That difference only appears once a real client speaks the protocol, which is
what this file does.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.websocket import manager
from main import app


@pytest.fixture
def client(monkeypatch):
    """A synchronous test client — the WebSocket support of httpx has no counterpart here.

    `TestClient` runs the app in a thread of its own, so the connection really goes through
    the protocol handshake instead of calling the endpoint function directly.

    Entering it runs the lifespan, and that one verifies the Neo4j connection. These tests
    need no database, so the driver is replaced with one whose connectivity check does
    nothing — the alternative would be a second app object, and then the test would no
    longer prove that the route is registered on the REAL app.
    """
    class SilentDriver:
        async def verify_connectivity(self) -> None:
            return None

    monkeypatch.setattr("main.Neo4jDatabase.get_driver", staticmethod(lambda: SilentDriver()))
    monkeypatch.setattr("main.close_db_session", AsyncMock())

    with TestClient(app) as test_client:
        yield test_client
    manager.active.clear()


@pytest.fixture
def token(role_token) -> str:
    return role_token("Sales")


def test_a_connection_without_a_token_is_refused(client):
    """FastAPI rejects the missing query parameter itself — before the endpoint function
    runs."""
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_a_forged_token_is_refused(client):
    """The handshake does not complete, so the client never holds an open socket. The close
    code 4008 the endpoint asks for therefore never reaches it — that is the price of
    checking before `accept()`, and it is the safer half of the trade."""
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=not.a.real.token"):
            pass


def test_a_valid_token_gets_a_connection(client, token):
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert len(manager.active) == 1
        assert ws is not None


def test_the_connection_is_registered_with_its_user(client, token):
    """The manager ties every connection to its authenticated user — the log line and any
    future per-user filtering hang off that."""
    with client.websocket_connect(f"/ws?token={token}"):
        user = next(iter(manager.active.values()))

        assert user["email"] == "test@acme.example"
        assert user["roles"] == ["Sales"]


def test_an_event_reaches_the_connected_client(client, token):
    """End to end over a real socket: what a service sends lands in the browser as JSON.

    `client.portal.call` is the bridge across the thread boundary: the app runs in the
    client's own event loop, so the broadcast has to be scheduled there rather than awaited
    from this synchronous test.
    """
    with client.websocket_connect(f"/ws?token={token}") as ws:
        client.portal.call(  # type: ignore[attr-defined]
            manager.send_event,
            {
                "type": "event", "entity": "document", "trigger": "document_created",
                "reference": "QU-2026-0001", "ids": ["QU-2026-0001"], "scope": "list",
            },
        )

        message = ws.receive_json()

    assert message["trigger"] == "document_created"
    assert message["ids"] == ["QU-2026-0001"]


def test_a_frame_from_the_client_is_discarded(client, token):
    """The server is the single source of events. A client able to push into this socket
    would be an unauthenticated write path past every router."""
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text('{"type": "event", "trigger": "smuggled"}')

        client.portal.call(  # type: ignore[attr-defined]
            manager.send_event, {"type": "event", "entity": "document", "trigger": "real"}
        )
        message = ws.receive_json()

    # The only thing that arrives is what the server sent — the frame from the client was
    # neither echoed nor passed on.
    assert message["trigger"] == "real"


def test_the_client_is_removed_after_the_disconnect(client, token):
    """A dead socket left in the dictionary would make every subsequent broadcast pay for it
    again."""
    with client.websocket_connect(f"/ws?token={token}"):
        assert len(manager.active) == 1

    assert manager.active == {}
