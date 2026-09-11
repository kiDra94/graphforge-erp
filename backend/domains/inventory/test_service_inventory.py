"""Unit tests of the inventory service layer — repository mocked, no Neo4j needed."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from core.exceptions import BusinessLogicError, NotFoundError
from domains.inventory.schemas_inventory import (
    Location,
    Stock,
    StockMovement,
    StockMovementCreate,
    StockMovementResponse,
)
from domains.inventory.service_inventory import (
    LocationService,
    StockMovementService,
    StockService,
)

SERVICE_MODULE = "domains.inventory.service_inventory"
MANAGER_MODULE = "core.websocket"


def _response(**overrides) -> StockMovementResponse:
    data = {"newQuantity": 15.0, "reserved": 0.0, "movementId": "mov-1"}
    data.update(overrides)
    return StockMovementResponse(**data)


def _create(**overrides) -> StockMovementCreate:
    data = {
        "productNumber": "ACME-2003",
        "quantity": 5.0,
        "type": "Receipt",
        "locationId": "1",
    }
    data.update(overrides)
    return StockMovementCreate(**data)


# ==========================================
# Schema validators — the three cross-field rules
# ==========================================

def test_a_transfer_without_a_destination_is_rejected():
    with pytest.raises(ValidationError):
        _create(type="Transfer", quantity=5.0)


def test_a_transfer_to_the_same_location_is_rejected():
    with pytest.raises(ValidationError):
        _create(type="Transfer", locationId="1", targetLocationId="1")


def test_a_destination_on_a_non_transfer_is_rejected():
    """A set targetLocationId would have no effect and only pretend that it had one."""
    with pytest.raises(ValidationError):
        _create(type="Receipt", targetLocationId="2")


def test_a_purchase_price_outside_a_receipt_is_rejected():
    """It would either be discarded silently or distort the moving average price."""
    with pytest.raises(ValidationError):
        _create(type="Issue", purchasePrice=Decimal("11.00"))


@pytest.mark.parametrize("movement_type", ["Correction", "Transfer"])
def test_a_customer_on_an_internal_operation_is_rejected(movement_type):
    """It would show a movement in the per-customer report that never happened."""
    extra = {"targetLocationId": "2"} if movement_type == "Transfer" else {}
    with pytest.raises(ValidationError):
        _create(type=movement_type, customerId="C-1001", **extra)


# ==========================================
# LocationService / StockService
# ==========================================

@pytest.mark.asyncio
async def test_get_locations_passes_the_result_through(monkeypatch):
    expected = [Location(id="1", name="Central Warehouse", type="Warehouse")]
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.LocationRepository.get_locations", AsyncMock(return_value=expected)
    )

    assert await LocationService.get_locations(AsyncMock()) is expected


@pytest.mark.asyncio
async def test_get_stock_translates_none_into_not_found(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockRepository.get_stock", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await StockService.get_stock("nope", AsyncMock())


@pytest.mark.asyncio
async def test_a_product_without_a_stock_record_is_not_an_error(monkeypatch):
    """Never stored is a stock of zero, not a 404 — that difference matters."""
    expected = Stock(
        productNumber="ACME-3001", label="Installation",
        totalStock=0.0, reserved=0.0, available=0.0, byLocation=[],
    )
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockRepository.get_stock", AsyncMock(return_value=expected)
    )

    result = await StockService.get_stock("ACME-3001", AsyncMock())

    assert result.totalStock == 0.0
    assert result.byLocation == []


@pytest.mark.asyncio
async def test_get_stock_list_passes_the_filter_through(monkeypatch):
    repo = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.StockRepository.get_stock_list", repo)
    session = AsyncMock()

    await StockService.get_stock_list(session, below_min_stock=True)

    repo.assert_awaited_once_with(session, True)


# ==========================================
# The sign rule — enforced before the database is touched
# ==========================================

@pytest.mark.parametrize("movement_type", ["Receipt", "Reservation", "Issue", "Transfer"])
@pytest.mark.parametrize("quantity", [0.0, -1.0])
@pytest.mark.asyncio
async def test_quantity_has_to_be_positive_except_on_a_correction(
    monkeypatch, movement_type, quantity
):
    repo = AsyncMock()
    monkeypatch.setattr(f"{SERVICE_MODULE}.StockMovementRepository.post_movement", repo)
    monkeypatch.setattr(f"{SERVICE_MODULE}.StockMovementRepository.post_transfer", repo)

    extra = {"targetLocationId": "2"} if movement_type == "Transfer" else {}
    with pytest.raises(BusinessLogicError):
        await StockMovementService.post_movement(
            _create(type=movement_type, quantity=quantity, **extra), AsyncMock()
        )

    repo.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_correction_may_be_negative(monkeypatch):
    """The one signed movement type — the emergency exit for wrong stock figures."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockMovementRepository.post_movement",
        AsyncMock(return_value=_response()),
    )
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    result = await StockMovementService.post_movement(
        _create(type="Correction", quantity=-5.0), AsyncMock()
    )

    assert result.movementId == "mov-1"


@pytest.mark.asyncio
async def test_a_correction_of_zero_is_rejected(monkeypatch):
    """It would be an audit log entry that says nothing."""
    repo = AsyncMock()
    monkeypatch.setattr(f"{SERVICE_MODULE}.StockMovementRepository.post_movement", repo)

    with pytest.raises(BusinessLogicError):
        await StockMovementService.post_movement(
            _create(type="Correction", quantity=0.0), AsyncMock()
        )

    repo.assert_not_awaited()


# ==========================================
# Routing between single booking and transfer
# ==========================================

@pytest.mark.asyncio
async def test_a_transfer_calls_post_transfer_not_post_movement(monkeypatch):
    single = AsyncMock()
    transfer = AsyncMock(return_value=_response(
        targetNewQuantity=5.0, targetMovementId="mov-2"
    ))
    monkeypatch.setattr(f"{SERVICE_MODULE}.StockMovementRepository.post_movement", single)
    monkeypatch.setattr(f"{SERVICE_MODULE}.StockMovementRepository.post_transfer", transfer)
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    result = await StockMovementService.post_movement(
        _create(type="Transfer", targetLocationId="2"), AsyncMock()
    )

    assert transfer.await_count == 1
    single.assert_not_awaited()
    assert result.targetMovementId == "mov-2"


@pytest.mark.asyncio
async def test_an_overdraft_is_passed_through_unchanged(monkeypatch):
    """The stock check belongs in the transaction — the service must not swallow it."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockMovementRepository.post_movement",
        AsyncMock(side_effect=BusinessLogicError("stock would go negative")),
    )

    with pytest.raises(BusinessLogicError):
        await StockMovementService.post_movement(_create(type="Issue"), AsyncMock())


@pytest.mark.asyncio
async def test_an_unknown_product_is_passed_through_unchanged(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockMovementRepository.post_movement",
        AsyncMock(side_effect=NotFoundError("does not exist")),
    )

    with pytest.raises(NotFoundError):
        await StockMovementService.post_movement(_create(), AsyncMock())


# ==========================================
# Real-time events
# ==========================================

@pytest.mark.asyncio
async def test_a_booking_sends_exactly_one_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockMovementRepository.post_movement",
        AsyncMock(return_value=_response()),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await StockMovementService.post_movement(
        _create(documentNumber="GR-2026-0001"), AsyncMock()
    )

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "stock", "trigger": "stock_movement",
        "reference": "GR-2026-0001", "ids": ["ACME-2003"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_a_transfer_sends_exactly_one_event(monkeypatch):
    """Two bookings, one business operation — therefore one event, not two."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockMovementRepository.post_transfer",
        AsyncMock(return_value=_response(targetNewQuantity=5.0, targetMovementId="mov-2")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await StockMovementService.post_movement(
        _create(type="Transfer", targetLocationId="2"), AsyncMock()
    )

    assert send_event.await_count == 1


@pytest.mark.asyncio
async def test_a_failed_booking_sends_no_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockMovementRepository.post_movement",
        AsyncMock(side_effect=BusinessLogicError("stock would go negative")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(BusinessLogicError):
        await StockMovementService.post_movement(_create(type="Issue"), AsyncMock())

    send_event.assert_not_awaited()


# ==========================================
# get_movements — pass-through of the filters
# ==========================================

@pytest.mark.asyncio
async def test_get_movements_passes_the_filters_through_unchanged(monkeypatch):
    from datetime import date

    repo = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.StockMovementRepository.get_movements", repo)
    session = AsyncMock()

    await StockMovementService.get_movements(
        session, product_number="ACME-2003", location_id="1", type="Receipt",
        from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
    )

    repo.assert_awaited_once_with(
        session, product_number="ACME-2003", location_id="1", type="Receipt",
        from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
    )


@pytest.mark.asyncio
async def test_get_movements_empty_result_is_not_an_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockMovementRepository.get_movements", AsyncMock(return_value=[])
    )

    assert await StockMovementService.get_movements(AsyncMock()) == []


@pytest.mark.asyncio
async def test_get_movements_returns_the_list(monkeypatch):
    expected = [StockMovement(id="mov-1", productNumber="ACME-2003", type="Receipt")]
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.StockMovementRepository.get_movements",
        AsyncMock(return_value=expected),
    )

    assert await StockMovementService.get_movements(AsyncMock()) is expected
