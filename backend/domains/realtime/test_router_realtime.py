"""Unit tests of the WebSocket endpoint — against a stand-in socket, without a server.

The endpoint is short, and three of its four lines are a decision worth a test:

* **The token is checked before `accept()`.** The other way round a client without a right
  to be there would sit in the manager, and the next broadcast would reach it. That is the
  test this module exists for.
* **A refusal closes with 4008** and registers nothing. On the wire that surfaces as an
  HTTP 403 during the handshake, because the socket is closed before `accept()` — the
  close code is what the endpoint asks for, not what the client ends up seeing.
* **A disconnect removes the client**, so a broadcast does not pay for a dead socket again
  on every event.

Not here: whether a real browser can connect, and whether an event actually arrives over
the wire. Both need a running server (integration tests).
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, WebSocketDisconnect

from core.websocket import ConnectionManager
from domains.realtime import router_realtime

ROUTER_MODULE = "domains.realtime.router_realtime"

_USER = {"sub": "1", "email": "max.mustermann@acme.example", "roles": ["Sales"]}


class FakeWebSocket:
    """A WebSocket that records what happened to it.

    `incoming` is what `receive_text()` hands out one after another; once the list is
    exhausted it raises `WebSocketDisconnect`, the way the real socket does when the client
    goes away.
    """

    def __init__(self, incoming: list[str] | None = None):
        self.incoming = list(incoming or [])
        self.accepted = False
        self.closed_with: tuple[int, str] | None = None
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int, reason: str = "") -> None:
        self.closed_with = (code, reason)

    async def receive_text(self) -> str:
        if self.incoming:
            return self.incoming.pop(0)
        raise WebSocketDisconnect(1000)

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


@pytest.fixture
def manager(monkeypatch):
    """Gives the endpoint a manager of its own, so the tests do not share the global one."""
    fresh = ConnectionManager()
    monkeypatch.setattr(f"{ROUTER_MODULE}.manager", fresh)
    return fresh


@pytest.fixture
def valid_token(monkeypatch):
    """Lets `decode_token` pass and return the demo payload."""
    monkeypatch.setattr(f"{ROUTER_MODULE}.decode_token", lambda token: _USER)


@pytest.fixture
def invalid_token(monkeypatch):
    """Lets `decode_token` fail the way it does on an expired or tampered token."""
    def raising(token: str) -> dict:
        raise HTTPException(status_code=401, detail="Token has expired.")

    monkeypatch.setattr(f"{ROUTER_MODULE}.decode_token", raising)


# ==========================================
# Refusal
# ==========================================

@pytest.mark.asyncio
async def test_an_invalid_token_is_never_accepted(manager, invalid_token):
    """The check runs BEFORE `accept()`. Accepting first and closing afterwards would
    register a client that never had a right to be there."""
    ws = FakeWebSocket()

    await router_realtime.websocket_endpoint(ws, token="expired")  # type: ignore[arg-type]

    assert ws.accepted is False
    assert manager.active == {}


@pytest.mark.asyncio
async def test_an_invalid_token_closes_with_the_policy_violation_code(manager, invalid_token):
    """4008 is the standard code for an authentication failure.

    Closing before `accept()` means Starlette turns it into an HTTP 403 on the wire and the
    code never reaches the client — that is the price of never handing out an open socket,
    and it is the behaviour the endpoint deliberately picks.
    """
    ws = FakeWebSocket()

    await router_realtime.websocket_endpoint(ws, token="tampered")  # type: ignore[arg-type]

    assert ws.closed_with is not None
    assert ws.closed_with[0] == 4008


@pytest.mark.asyncio
async def test_the_refusal_does_not_distinguish_expired_from_tampered(manager, monkeypatch):
    """Both failures get the same answer. A distinction would tell an unauthenticated
    caller whether a token it holds was ever valid."""
    reasons = []
    for error in (HTTPException(401, "Token has expired."), ValueError("garbage")):
        monkeypatch.setattr(
            f"{ROUTER_MODULE}.decode_token",
            lambda token, e=error: (_ for _ in ()).throw(e),
        )
        ws = FakeWebSocket()
        await router_realtime.websocket_endpoint(ws, token="x")  # type: ignore[arg-type]
        assert ws.closed_with is not None
        reasons.append(ws.closed_with)

    assert reasons[0] == reasons[1]


# ==========================================
# Accepted connection
# ==========================================

@pytest.mark.asyncio
async def test_a_valid_token_accepts_and_registers(manager, valid_token):
    ws = FakeWebSocket()

    await router_realtime.websocket_endpoint(ws, token="valid")  # type: ignore[arg-type]

    assert ws.accepted is True
    assert ws.closed_with is None


@pytest.mark.asyncio
async def test_the_user_from_the_token_reaches_the_manager(manager, valid_token):
    """The manager ties every connection to its authenticated user — the log line and any
    future per-user filtering hang off it."""
    seen: dict = {}

    async def capturing_connect(ws, user):
        seen["user"] = user
        await ws.accept()

    manager.connect = capturing_connect  # type: ignore[method-assign]
    ws = FakeWebSocket()

    await router_realtime.websocket_endpoint(ws, token="valid")  # type: ignore[arg-type]

    assert seen["user"] == _USER


@pytest.mark.asyncio
async def test_a_disconnect_removes_the_client(manager, valid_token):
    """A dead socket left in the dictionary would make every subsequent broadcast pay for
    it again."""
    ws = FakeWebSocket()

    await router_realtime.websocket_endpoint(ws, token="valid")  # type: ignore[arg-type]

    assert manager.active == {}


@pytest.mark.asyncio
async def test_an_incoming_frame_is_discarded(manager, valid_token):
    """The server is the single source of events. A client that could push into this socket
    would be an unauthenticated write path past every router."""
    ws = FakeWebSocket(incoming=["{\"type\": \"event\", \"entity\": \"document\"}"])

    await router_realtime.websocket_endpoint(ws, token="valid")  # type: ignore[arg-type]

    # Nothing was echoed back and nothing was passed on — the loop only waits for the
    # disconnect, which the exhausted list then raises.
    assert ws.sent == []
    assert manager.active == {}


@pytest.mark.asyncio
async def test_the_connection_survives_several_incoming_frames(manager, valid_token):
    """The loop keeps running until the disconnect — a chatty legacy client must not tear
    the connection down."""
    ws = FakeWebSocket(incoming=["one", "two", "three"])

    await router_realtime.websocket_endpoint(ws, token="valid")  # type: ignore[arg-type]

    assert ws.incoming == []
    assert ws.closed_with is None


# ==========================================
# Broadcast over the registered connection
# ==========================================

@pytest.mark.asyncio
async def test_an_event_reaches_a_connected_client(manager):
    """End to end within the manager: whoever is registered gets every event as JSON."""
    ws = FakeWebSocket()
    await manager.connect(ws, _USER)  # type: ignore[arg-type]

    await manager.send_event({
        "type": "event", "entity": "document", "trigger": "document_created",
        "reference": "QU-2026-0001", "ids": ["QU-2026-0001"], "scope": "list",
    })

    assert len(ws.sent) == 1
    assert "document_created" in ws.sent[0]


@pytest.mark.asyncio
async def test_a_client_whose_socket_raises_is_dropped(manager):
    """Dropped rather than retried: the connection is gone, and keeping it would make
    every subsequent broadcast pay for it again."""
    healthy = FakeWebSocket()
    broken = AsyncMock()
    broken.send_text.side_effect = RuntimeError("socket closed")

    await manager.connect(healthy, _USER)  # type: ignore[arg-type]
    await manager.connect(broken, _USER)

    await manager.send_event({"type": "event", "entity": "document"})

    assert list(manager.active) == [healthy]
    assert len(healthy.sent) == 1
