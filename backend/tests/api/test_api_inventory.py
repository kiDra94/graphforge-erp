"""API tests of the inventory routes.

The interesting part of this domain at the HTTP level is the role check per movement type:
it is not a fixed role for the endpoint but a decision per request body. That check sits in
the router, so this is the level that can prove it.
"""

from unittest.mock import AsyncMock

import pytest

from core.exceptions import BusinessLogicError, NotFoundError
from domains.inventory.schemas_inventory import (
    Location,
    Stock,
    StockMovementResponse,
)

LOCATION_SERVICE = "domains.inventory.router_inventory.LocationService"
STOCK_SERVICE = "domains.inventory.router_inventory.StockService"
MOVEMENT_SERVICE = "domains.inventory.router_inventory.StockMovementService"


def _movement(**overrides) -> dict:
    data: dict = {
        "productNumber": "ACME-2001", "locationId": "1",
        "type": "Receipt", "quantity": 10.0,
    }
    data.update(overrides)
    return data


# ==========================================
# Reading
# ==========================================

@pytest.mark.asyncio
async def test_the_location_list_is_open_to_every_signed_in_user(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{LOCATION_SERVICE}.get_locations",
        AsyncMock(return_value=[Location(id="1", name="Central Warehouse", type="Warehouse")]),
    )

    response = await async_client.get("/api/locations", headers=auth("Sales"))

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_stock_list_passes_the_filter_on(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{STOCK_SERVICE}.get_stock_list", service)

    await async_client.get("/api/stock?belowMinStock=true", headers=auth("Warehouse"))

    assert service.await_args is not None
    assert service.await_args.args[1] is True


@pytest.mark.asyncio
async def test_the_stock_of_one_product_answers_404_when_unknown(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{STOCK_SERVICE}.get_stock", AsyncMock(side_effect=NotFoundError("no such product"))
    )

    response = await async_client.get("/api/stock/NOPE", headers=auth("Warehouse"))

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_stock_answer_carries_the_available_quantity(async_client, auth, monkeypatch):
    """`available` is the figure the caller actually plans against — stock minus what is
    already reserved."""
    monkeypatch.setattr(
        f"{STOCK_SERVICE}.get_stock",
        AsyncMock(return_value=Stock(
            productNumber="ACME-2001", totalStock=100.0, reserved=40.0, available=60.0
        )),
    )

    response = await async_client.get("/api/stock/ACME-2001", headers=auth("Warehouse"))

    assert response.json()["available"] == 60.0


# ==========================================
# The role check per movement type
# ==========================================

@pytest.mark.asyncio
async def test_warehouse_may_book_a_transfer(async_client, auth, monkeypatch):
    """A transfer between locations is a single internal operation — whoever carries the
    box may book it."""
    monkeypatch.setattr(
        f"{MOVEMENT_SERVICE}.post_movement",
        AsyncMock(return_value=StockMovementResponse(
            movementId="mov-1", newQuantity=10.0, reserved=0.0
        )),
    )

    response = await async_client.post(
        "/api/stock-movements",
        json=_movement(type="Transfer", targetLocationId="2"),
        headers=auth("Warehouse"),
    )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_warehouse_may_not_book_a_receipt(async_client, auth):
    """Not a restriction of the endpoint but of the type: a receipt runs through the goods
    receipt of a purchase order, where it is checked against what was ordered."""
    response = await async_client.post(
        "/api/stock-movements", json=_movement(type="Receipt"), headers=auth("Warehouse")
    )

    assert response.status_code == 403
    assert "Receipt" in response.json()["message"]


@pytest.mark.asyncio
async def test_backoffice_may_book_every_type(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{MOVEMENT_SERVICE}.post_movement",
        AsyncMock(return_value=StockMovementResponse(
            movementId="mov-1", newQuantity=10.0, reserved=0.0
        )),
    )

    for movement_type in ("Receipt", "Issue", "Reservation", "Correction"):
        response = await async_client.post(
            "/api/stock-movements", json=_movement(type=movement_type), headers=auth("BackOffice")
        )
        assert response.status_code == 201


@pytest.mark.asyncio
async def test_a_caller_without_either_role_answers_403(async_client, auth):
    response = await async_client.post(
        "/api/stock-movements", json=_movement(), headers=auth("Sales")
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_movement_type_answers_422(async_client, auth):
    """The Literal rejects it before the role check — otherwise an unknown type would get as
    far as the query and silently book nothing."""
    response = await async_client.post(
        "/api/stock-movements", json=_movement(type="Teleport"), headers=auth("BackOffice")
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_stock_going_negative_answers_400(async_client, auth, monkeypatch):
    """The rule sits on the booking itself, so it holds for this endpoint as well as for
    every document that books through it."""
    monkeypatch.setattr(
        f"{MOVEMENT_SERVICE}.post_movement",
        AsyncMock(side_effect=BusinessLogicError("stock would go negative")),
    )

    response = await async_client.post(
        "/api/stock-movements",
        json=_movement(type="Issue", quantity=999.0),
        headers=auth("BackOffice"),
    )

    assert response.status_code == 400


# ==========================================
# The movement history
# ==========================================

@pytest.mark.asyncio
async def test_the_history_passes_every_filter_on(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{MOVEMENT_SERVICE}.get_movements", service)

    await async_client.get(
        "/api/stock-movements?productNumber=ACME-2001&locationId=1&type=Receipt"
        "&fromDate=2026-01-01&toDate=2026-12-31",
        headers=auth("BackOffice"),
    )

    assert service.await_args is not None
    kwargs = service.await_args.kwargs
    # The query parameters are camel case (the API contract), the service arguments snake
    # case (the Python contract). The router translates between the two, and a mix-up here
    # would deliver an unfiltered list.
    assert kwargs["product_number"] == "ACME-2001"
    assert kwargs["location_id"] == "1"
    assert kwargs["type"] == "Receipt"
    assert str(kwargs["from_date"]) == "2026-01-01"
    assert str(kwargs["to_date"]) == "2026-12-31"


@pytest.mark.asyncio
async def test_a_malformed_date_answers_422(async_client, auth):
    """`date` in the signature parses it — a free-text filter would reach the query and
    match nothing."""
    response = await async_client.get(
        "/api/stock-movements?fromDate=yesterday", headers=auth("BackOffice")
    )

    assert response.status_code == 422
