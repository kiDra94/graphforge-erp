"""WebSocket endpoint for notifying the clients in real time."""

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from core.security import decode_token
from core.websocket import manager

router = APIRouter(tags=["Realtime"])

# 4008 = Policy Violation, the standard WebSocket close code for an authentication
# failure.
#
# It only reaches a client that got as far as an open connection. Closing BEFORE
# `accept()` — which is what this endpoint does — makes Starlette deny the handshake with
# an HTTP 403 instead, and neither the code nor the reason travels along. That is the
# deliberate trade: a caller with a bad token never gets an open socket, and it learns no
# more than "refused".
_INVALID_TOKEN = 4008


@router.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    token: str = Query(..., description="JWT token for authentication"),
) -> None:
    """Takes incoming WebSocket connections and starts the communication.

    The client has to hand over a valid JWT as the query parameter `?token=...`. Browsers
    cannot send an Authorization header on a WebSocket connection — the query parameter is
    the standard-conforming alternative.

    A missing token is rejected by FastAPI itself, an invalid one by this function. In
    both cases the handshake never completes and the client sees an HTTP 403 rather than
    an open socket that closes again (see `_INVALID_TOKEN`).

    Args:
        ws (WebSocket): The incoming WebSocket session.
        token (str): JWT token from the query parameter `?token=...`.
    """
    # Check the token BEFORE the connection is accepted. Accepting first and closing
    # afterwards would register a client in the manager that never had a right to be
    # there, and the next broadcast would reach it.
    #
    # `decode_token` raises an HTTPException on an invalid or expired token. Caught
    # broadly on purpose: there is no HTTP response to turn it into here, and every
    # failure has the same answer — refuse the connection. A distinction between expired
    # and tampered would tell an unauthenticated caller more than it needs to know.
    try:
        user = decode_token(token)
    except Exception:
        await ws.close(code=_INVALID_TOKEN, reason="Invalid or expired token.")
        return

    await manager.connect(ws, user)
    try:
        # The client sends nothing any more — the server is the single source of events.
        # The loop only waits for the disconnect. A frame arriving from an older client
        # is deliberately discarded rather than passed on: a client that could push into
        # this socket would be an unauthenticated write path past every router.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
