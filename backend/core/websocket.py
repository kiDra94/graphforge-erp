"""Management of the active WebSocket connections and dispatch of server events."""

import json

from fastapi import WebSocket
from loguru import logger


class ConnectionManager:
    """Manages active WebSocket connections and the dispatch of server events.

    Every connection is tied to an authenticated user. The server is the single source:
    a newly connecting client receives no state along with the handshake — it loads its
    view over REST, where the permission check applies anyway.

    Attributes:
        active: Mapping of WebSocket -> user dict (from the JWT payload).
    """

    def __init__(self):
        self.active: dict[WebSocket, dict] = {}

    async def connect(self, ws: WebSocket, user: dict) -> None:
        """Accepts a new WebSocket connection and registers it.

        Args:
            ws (WebSocket): The incoming client connection.
            user (dict): Decoded JWT payload (sub, email, name, roles).
        """
        await ws.accept()
        self.active[ws] = user
        logger.bind(request_id="-").info(
            f"WS connected | user={user.get('email')} | clients={len(self.active)}"
        )

    def disconnect(self, ws: WebSocket) -> None:
        """Removes a client from the list of active connections.

        Args:
            ws (WebSocket): The connection to drop.
        """
        user = self.active.pop(ws, {})
        logger.bind(request_id="-").info(
            f"WS disconnected | user={user.get('email')} | clients={len(self.active)}"
        )

    async def send_event(self, event: dict) -> None:
        """Sends a server event to every connected client.

        A client whose socket raises on send is dropped rather than retried: the
        connection is gone, and keeping it in the dictionary would make every subsequent
        broadcast pay for it again.

        Args:
            event (dict): The event contract, with the fields `type`, `entity`,
                `trigger`, `reference`, `ids` and `scope`.
        """
        message = json.dumps(event)
        dead = set()
        for client in self.active:
            try:
                await client.send_text(message)
            except Exception:
                dead.add(client)

        for client in dead:
            self.active.pop(client, None)

        if dead:
            logger.bind(request_id="-").warning(
                f"WS clients removed | dead={len(dead)} | active={len(self.active)}"
            )


# Global instance imported by the router
manager = ConnectionManager()

# Upper bound from the event contract: above it, an event no longer carries individual
# ids but only "scope: many" — the client then reloads its view wholesale instead of
# reconciling 200+ single hits.
_ID_LIMIT = 200


def ids_and_scope(ids: list[str]) -> dict:
    """Builds the `ids`/`scope` fields of an event according to the 200 limit.

    Args:
        ids (list[str]): The ids affected by one business operation.

    Returns:
        dict: To be unpacked with `**` into a `manager.send_event(...)` call.
    """
    if len(ids) > _ID_LIMIT:
        return {"scope": "many"}
    return {"ids": ids, "scope": "list"}
