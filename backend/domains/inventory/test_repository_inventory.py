"""Unit tests of the inventory repository — without a database.

The centre of gravity is `_effect`: a pure function holding the truth table of all five
movement types. Everything the stock cache is worth depends on it, and it is the one part
of the booking that can be tested without a transaction.
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from core.exceptions import BusinessLogicError, DatabaseError
from domains.inventory.repository_inventory import (
    LocationRepository,
    StockMovementRepository,
    StockRepository,
    movement_params,
)

REPOSITORY_MODULE = "domains.inventory.repository_inventory"

_effect = StockMovementRepository._effect


# ==========================================
# _effect — the truth table of the five movement types
# ==========================================

def test_receipt_raises_the_stock_and_leaves_the_reservation():
    assert _effect("Receipt", 10, 5, 2) == (15, 2)


def test_issue_lowers_stock_and_reservation_together():
    """An issue releases the matching reservation in the same operation."""
    assert _effect("Issue", 3, 10, 3) == (7, 0)


def test_issue_without_a_reservation_does_not_drive_the_counter_negative():
    """A walk-in withdrawal is legitimate and must not produce a negative reservation."""
    assert _effect("Issue", 3, 10, 0) == (7, 0.0)


def test_issue_of_fully_reserved_stock_is_allowed():
    """The check runs against quantity, not against quantity - reserved.

    Otherwise the very issue the reservation was made for would fail against its own
    reservation, and a fully reserved line could never be shipped.
    """
    assert _effect("Issue", 5, 5, 5) == (0, 0.0)


def test_reservation_blocks_without_touching_the_stock():
    assert _effect("Reservation", 4, 10, 0) == (10, 4)


def test_reservation_is_capped_at_the_available_stock():
    """More than available may be requested; only what is there gets reserved.

    The order confirmation should still come into existence — otherwise it never shows up
    in the reorder analysis. The remainder stays on the line as an open quantity.
    """
    assert _effect("Reservation", 20, 10, 2) == (10, 10)


def test_reservation_against_nothing_available_is_rejected():
    with pytest.raises(BusinessLogicError):
        _effect("Reservation", 1, 10, 10)


def test_correction_carries_its_direction_in_the_sign():
    assert _effect("Correction", 5, 10, 0) == (15, 0)
    assert _effect("Correction", -5, 10, 0) == (5, 0)


def test_correction_against_reserved_leaves_the_stock_alone():
    """The counter-booking of a reservation must not change the stock.

    A reservation never touched `quantity`; an ordinary correction would be the wrong
    booking here, not merely an imprecise one.
    """
    assert _effect("Correction", -4, 10, 4, target_reserved=True) == (10, 0.0)


def test_correction_against_reserved_is_floored_at_zero():
    assert _effect("Correction", -10, 10, 4, target_reserved=True) == (10, 0.0)


def test_transfer_carries_its_direction_in_the_sign_and_ignores_the_reservation():
    assert _effect("Transfer", -3, 10, 2) == (7, 2)
    assert _effect("Transfer", 3, 10, 2) == (13, 2)


@pytest.mark.parametrize(
    ("movement_type", "quantity", "stock"),
    [("Issue", 11, 10), ("Correction", -11, 10), ("Transfer", -11, 10)],
)
def test_a_booking_driving_the_stock_negative_is_rejected(movement_type, quantity, stock):
    with pytest.raises(BusinessLogicError):
        _effect(movement_type, quantity, stock, 0)


# ==========================================
# movement_params — the single construction site for keys and cents
# ==========================================

def test_movement_params_builds_the_stock_key_from_product_and_location():
    """A second construction site would create a second stock node beside the existing one."""
    params = movement_params("ACME-2003", "1", "Receipt", 5)

    assert params["stockId"] == "ACME-2003_1"


def test_movement_params_converts_euro_to_integer_cents():
    """The driver rejects Decimal as a query parameter outright."""
    params = movement_params("ACME-2003", "1", "Receipt", 5, purchase_price=Decimal("11.00"))

    assert params["purchasePriceCent"] == 1100
    assert isinstance(params["purchasePriceCent"], int)


def test_movement_params_without_a_price_leaves_the_cent_field_empty():
    assert movement_params("ACME-2003", "1", "Issue", 5)["purchasePriceCent"] is None


def test_movement_params_gives_every_booking_its_own_id():
    a = movement_params("ACME-2003", "1", "Receipt", 5)
    b = movement_params("ACME-2003", "1", "Receipt", 5)

    assert a["movementId"] != b["movementId"]


# ==========================================
# _to_movement — cent/euro conversion and tolerance
# ==========================================

def _movement(**overrides) -> dict:
    props = {
        "id": "mov-1",
        "productNumber": "ACME-2003",
        "quantity": 50.0,
        "type": "Receipt",
        "locationName": "Central Warehouse",
        "documentNumber": "GR-2026-0001",
        "customerName": None,
        "purchasePriceCent": 1100,
        "createdAt": None,
    }
    props.update(overrides)
    return props


def test_to_movement_converts_cents_to_euro():
    assert StockMovementRepository._to_movement(_movement()).purchasePrice == Decimal("11.00")


def test_to_movement_without_a_cent_property_yields_none():
    props = _movement()
    del props["purchasePriceCent"]

    assert StockMovementRepository._to_movement(props).purchasePrice is None


def test_to_movement_keeps_a_recorded_price_of_zero():
    """0 cents is a recorded 0.00 EUR, not a missing price."""
    result = StockMovementRepository._to_movement(_movement(purchasePriceCent=0))

    assert result.purchasePrice == Decimal("0")


def test_to_movement_converts_a_neo4j_datetime():
    class _Stub:
        def to_native(self):
            return datetime(2026, 2, 12, 8, 0)

    result = StockMovementRepository._to_movement(_movement(createdAt=_Stub()))

    assert result.createdAt == datetime(2026, 2, 12, 8, 0)


def test_to_movement_accepts_a_thin_node():
    """Imported movements may be incomplete — only `id` is mandatory."""
    result = StockMovementRepository._to_movement({"id": "mov-old"})

    assert result.quantity is None
    assert result.type is None


def test_to_movement_rejects_an_unknown_type():
    with pytest.raises(DatabaseError):
        StockMovementRepository._to_movement(_movement(type="NoSuchType"))


def test_to_movement_reports_a_missing_id_as_a_database_error():
    with pytest.raises(DatabaseError):
        StockMovementRepository._to_movement({"quantity": 5.0})


# ==========================================
# get_movements — dynamically assembled WHERE clause
# ==========================================

@pytest.fixture
def captured_query(monkeypatch):
    captured: dict = {}

    async def fake_read_many(session, query, **params):
        captured["query"] = query
        captured["params"] = params
        return []

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_many", fake_read_many)
    return captured


@pytest.mark.asyncio
async def test_without_filters_no_parameters_are_bound(captured_query):
    await StockMovementRepository.get_movements(AsyncMock())

    assert captured_query["params"] == {}
    assert "WHERE" not in captured_query["query"]


@pytest.mark.asyncio
async def test_every_filter_lands_in_the_parameters(captured_query):
    await StockMovementRepository.get_movements(
        AsyncMock(), product_number="ACME-2003", location_id="1", type="Receipt",
    )

    assert captured_query["params"] == {
        "productNumber": "ACME-2003", "locationId": "1", "type": "Receipt",
    }
    assert captured_query["query"].count(" AND ") == 2


@pytest.mark.asyncio
async def test_filter_values_are_bound_not_inlined(captured_query):
    await StockMovementRepository.get_movements(
        AsyncMock(), product_number="'; MATCH (n) DETACH DELETE n //"
    )

    assert "DETACH DELETE" not in captured_query["query"]
    assert captured_query["params"]["productNumber"] == "'; MATCH (n) DETACH DELETE n //"


@pytest.mark.asyncio
async def test_an_empty_filter_value_produces_no_filter(captured_query):
    await StockMovementRepository.get_movements(AsyncMock(), product_number="")

    assert "WHERE" not in captured_query["query"]


@pytest.mark.asyncio
async def test_the_date_bounds_do_not_wrap_the_property_in_a_function(captured_query):
    """date(m.createdAt) would make an index on createdAt unusable."""
    from datetime import date

    await StockMovementRepository.get_movements(
        AsyncMock(), from_date=date(2026, 1, 1), to_date=date(2026, 12, 31)
    )

    assert "date(m.createdAt)" not in captured_query["query"]
    assert "m.createdAt >=" in captured_query["query"]
    # The upper bound includes its boundary day, hence "< toDate + 1 day".
    assert "duration({days: 1})" in captured_query["query"]


# ==========================================
# Read paths — mapping and error translation
# ==========================================

@pytest.mark.asyncio
async def test_get_locations_maps_the_records(monkeypatch):
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_many",
        AsyncMock(return_value=[
            {"l": {"id": "1", "name": "Central Warehouse", "type": "Warehouse"}},
            {"l": {"id": "2", "name": "Service Van 1", "type": "Vehicle"}},
        ]),
    )

    result = await LocationRepository.get_locations(AsyncMock())

    assert [x.id for x in result] == ["1", "2"]


@pytest.mark.asyncio
async def test_get_locations_translates_a_validation_error(monkeypatch):
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_many",
        AsyncMock(return_value=[{"l": {"id": "1"}}]),  # name and type missing
    )

    with pytest.raises(DatabaseError):
        await LocationRepository.get_locations(AsyncMock())


@pytest.mark.asyncio
async def test_get_stock_maps_the_record(monkeypatch):
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_single",
        AsyncMock(return_value={"stock": {
            "productNumber": "ACME-2003", "label": "Filter Cartridge",
            "totalStock": 68.0, "reserved": 6.0, "available": 62.0,
            "byLocation": [{
                "locationId": "1", "locationName": "Central Warehouse",
                "quantity": 62.0, "reserved": 6.0, "available": 56.0,
            }],
        }}),
    )

    result = await StockRepository.get_stock("ACME-2003", AsyncMock())

    assert result is not None
    assert result.totalStock == 68.0
    assert result.byLocation[0].locationName == "Central Warehouse"


@pytest.mark.asyncio
async def test_get_stock_without_a_hit_returns_none(monkeypatch):
    """None means "product unknown" — the 404 translation is the service's job."""
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_single", AsyncMock(return_value=None))

    assert await StockRepository.get_stock("nope", AsyncMock()) is None


@pytest.mark.asyncio
async def test_get_stock_list_binds_the_filter(captured_query):
    await StockRepository.get_stock_list(AsyncMock(), below_min_stock=True)

    assert captured_query["params"] == {"belowMinStock": True}


@pytest.mark.asyncio
async def test_get_stock_list_without_the_filter_binds_false(captured_query):
    await StockRepository.get_stock_list(AsyncMock())

    assert captured_query["params"] == {"belowMinStock": False}
