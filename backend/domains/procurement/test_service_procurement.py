"""Unit tests of the procurement services — against mocks, without a database.

Tested is exactly what the service decides itself:

* the translation of a `None` from the repository into a `NotFoundError` — on
  `get_supplier` and `update_supplier` the only own contribution of this layer
* that filters and write models are passed on unchanged and by name
* that exceptions from the repository are not swallowed — the global handler in
  `main.py` turns them into the matching status code

Not here: the selection of the preferred supplier, the exclusion of replaced products and
the clamping of the order quantity. The first two are conditions of the Cypher query and
cannot be proven against a mock (integration tests), the third sits in
`_to_reorder_suggestion` in the repository, because only there does the raw record with
`targetStock` and `totalStock` exist.
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from core.exceptions import DatabaseError, NotFoundError
from domains.procurement.schemas_procurement import (
    ReorderSuggestion,
    Supplier,
    SupplierCreate,
    SupplierProductCreate,
    SupplierUpdate,
)
from domains.procurement.service_procurement import (
    ReorderSuggestionService,
    SupplierService,
)

# Base path for every mock: patch where the name is ACTUALLY used
# (service_procurement.py imports the repositories via "from .repository_procurement
# import ..." -> the names therefore live in the service_procurement module).
SERVICE_MODULE = "domains.procurement.service_procurement"
MANAGER_MODULE = "core.websocket"


def _supplier(**overrides) -> Supplier:
    """Builds a complete supplier; individual fields can be overridden."""
    data: dict = {
        "id": "S-001",
        "name": "Northern Supply GmbH",
        "email": "sales@northern-supply.example",
        "street": "Hafenstrasse 1",
        "city": "Hamburg",
        "country": "DE",
        "vatId": "DE100000001",
    }
    data.update(overrides)
    return Supplier(**data)


def _new_supplier(**overrides) -> SupplierCreate:
    """Builds a valid creation request; only `name` is mandatory."""
    data: dict = {"name": "Eastern Trading Ltd"}
    data.update(overrides)
    return SupplierCreate(**data)


def _supply_range(**overrides) -> SupplierProductCreate:
    """Builds a valid addition to a supply range."""
    data: dict = {
        "productNumber": "ACME-2003",
        "leadTimeDays": 5,
        "purchasePrice": Decimal("11.00"),
        "isPreferredSupplier": True,
    }
    data.update(overrides)
    return SupplierProductCreate(**data)


def _supply_response(**overrides) -> dict:
    """Builds the repository's answer to an addition to a supply range."""
    data: dict = {
        "supplierId": "S-001",
        "productNumber": "ACME-2003",
        "leadTimeDays": 5,
        "purchasePrice": Decimal("11.00"),
        "isPreferredSupplier": True,
        "wasUpdated": False,
    }
    data.update(overrides)
    return data


# ==========================================
# GET /api/suppliers/{id}
# ==========================================

@pytest.mark.asyncio
async def test_get_supplier_passes_the_result_through(monkeypatch):
    expected = _supplier()
    repo = AsyncMock(return_value=expected)
    monkeypatch.setattr(f"{SERVICE_MODULE}.SupplierRepository.get_supplier", repo)

    session = AsyncMock()
    result = await SupplierService.get_supplier("S-001", session)

    assert result == expected
    repo.assert_awaited_once_with("S-001", session)


@pytest.mark.asyncio
async def test_get_supplier_translates_none_into_not_found(monkeypatch):
    """The repository reports an unknown id as None.

    Only the service turns that into the business error the global handler translates
    into a 404.
    """
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.get_supplier", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await SupplierService.get_supplier("S-nope", AsyncMock())


@pytest.mark.asyncio
async def test_get_supplier_names_the_requested_id_in_the_message(monkeypatch):
    """Without the id in the message the log does not say what was looked for."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.get_supplier", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError) as excinfo:
        await SupplierService.get_supplier("S-nope", AsyncMock())

    assert "S-nope" in str(excinfo.value)


# ==========================================
# GET /api/suppliers
# ==========================================

@pytest.mark.asyncio
async def test_get_suppliers_passes_the_list_through(monkeypatch):
    expected = [_supplier(), _supplier(id="S-002", name="Southern Parts AG", city="Munich")]
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.get_suppliers", AsyncMock(return_value=expected)
    )

    assert await SupplierService.get_suppliers(AsyncMock()) == expected


@pytest.mark.asyncio
async def test_get_suppliers_passes_the_search_on_as_a_keyword(monkeypatch):
    """Catches the most common mistake.

    The filter is accepted in the signature but not passed on — the endpoint then
    silently answers with unfiltered data.
    """
    repo = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.SupplierRepository.get_suppliers", repo)

    session = AsyncMock()
    await SupplierService.get_suppliers(session, search="Northern")

    repo.assert_awaited_once_with(session, search="Northern")


@pytest.mark.asyncio
async def test_get_suppliers_without_a_search_passes_none_on(monkeypatch):
    repo = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.SupplierRepository.get_suppliers", repo)

    session = AsyncMock()
    await SupplierService.get_suppliers(session)

    repo.assert_awaited_once_with(session, search=None)


@pytest.mark.asyncio
async def test_get_suppliers_an_empty_list_is_not_an_error(monkeypatch):
    """A search term without hits is a normal result, not an error case."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.get_suppliers", AsyncMock(return_value=[])
    )

    assert await SupplierService.get_suppliers(AsyncMock(), search="nothing") == []


# ==========================================
# POST /api/suppliers
# ==========================================

@pytest.mark.asyncio
async def test_create_supplier_hands_the_model_over_unchanged(monkeypatch):
    repo = AsyncMock(return_value=_supplier())
    monkeypatch.setattr(f"{SERVICE_MODULE}.SupplierRepository.create_supplier", repo)
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    new = _new_supplier()
    session = AsyncMock()
    await SupplierService.create_supplier(new, session)

    repo.assert_awaited_once_with(new, session)


@pytest.mark.asyncio
async def test_create_supplier_returns_the_created_supplier(monkeypatch):
    """The answer is the complete supplier including the server-assigned id.

    The client does not know it beforehand and needs it for every follow-up call.
    """
    created = _supplier(id="S-0e6f2c1a-1111-2222-3333-444455556666")
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.create_supplier", AsyncMock(return_value=created)
    )
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    result = await SupplierService.create_supplier(_new_supplier(), AsyncMock())

    assert result.id == created.id


# ==========================================
# PATCH /api/suppliers/{id}
# ==========================================

@pytest.mark.asyncio
async def test_update_supplier_passes_the_result_through(monkeypatch):
    expected = _supplier(email="new@northern-supply.example")
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.update_supplier", AsyncMock(return_value=expected)
    )
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    result = await SupplierService.update_supplier(
        "S-001", SupplierUpdate(email="new@northern-supply.example"), AsyncMock()
    )

    assert result == expected


@pytest.mark.asyncio
async def test_update_supplier_translates_none_into_not_found(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.update_supplier", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError) as excinfo:
        await SupplierService.update_supplier(
            "S-nope", SupplierUpdate(email="new@northern-supply.example"), AsyncMock()
        )

    assert "S-nope" in str(excinfo.value)


@pytest.mark.asyncio
async def test_update_supplier_does_not_pre_filter_the_model(monkeypatch):
    """Which fields were set is known only to the model itself (exclude_unset).

    Were the service to throw fields away here, the repository could no longer tell a
    deliberately sent null from a field that was not sent at all.
    """
    repo = AsyncMock(return_value=_supplier())
    monkeypatch.setattr(f"{SERVICE_MODULE}.SupplierRepository.update_supplier", repo)
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    change = SupplierUpdate(email="new@northern-supply.example")
    session = AsyncMock()
    await SupplierService.update_supplier("S-001", change, session)

    repo.assert_awaited_once_with("S-001", change, session)
    call = repo.await_args
    assert call is not None
    assert call.args[1].model_dump(exclude_unset=True) == {
        "email": "new@northern-supply.example"
    }


# ==========================================
# POST /api/suppliers/{id}/products
# ==========================================

@pytest.mark.asyncio
async def test_supply_range_passes_the_update_flag_through(monkeypatch):
    """The router decides between 201 and 200 on `wasUpdated`.

    If the service swallows the flag, every second call falsely reports "newly created".
    """
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.add_supplied_product",
        AsyncMock(return_value=_supply_response(wasUpdated=True)),
    )
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    result = await SupplierService.add_supplied_product("S-001", _supply_range(), AsyncMock())

    assert result["wasUpdated"] is True


@pytest.mark.asyncio
async def test_supply_range_calls_the_repository_with_every_argument(monkeypatch):
    repo = AsyncMock(return_value=_supply_response())
    monkeypatch.setattr(f"{SERVICE_MODULE}.SupplierRepository.add_supplied_product", repo)
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    supply_range = _supply_range()
    session = AsyncMock()
    await SupplierService.add_supplied_product("S-001", supply_range, session)

    repo.assert_awaited_once_with("S-001", supply_range, session)


@pytest.mark.asyncio
async def test_supply_range_lets_not_found_through(monkeypatch):
    """Whether supplier or product is missing is decided by the transaction.

    The service must not catch the error, otherwise the 404 turns into a 500.
    """
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.add_supplied_product",
        AsyncMock(side_effect=NotFoundError("A product with the number 'ACME-9999' does not exist.")),
    )

    with pytest.raises(NotFoundError):
        await SupplierService.add_supplied_product(
            "S-001", _supply_range(productNumber="ACME-9999"), AsyncMock()
        )


# ==========================================
# GET /api/reorder-suggestions
# ==========================================

@pytest.mark.asyncio
async def test_reorder_suggestions_passes_the_list_through(monkeypatch):
    expected = [
        ReorderSuggestion(
            productNumber="ACME-2003",
            label="Filter Cartridge",
            currentStock=12.0,
            minStock=20,
            suggestedQuantity=68.0,
            supplier="Northern Supply GmbH",
            unitPrice=Decimal("11.00"),
            leadTimeDays=5,
        )
    ]
    repo = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ReorderSuggestionRepository.get_reorder_suggestions", repo
    )

    session = AsyncMock()
    result = await ReorderSuggestionService.get_reorder_suggestions(session)

    assert result == expected
    repo.assert_awaited_once_with(session)


@pytest.mark.asyncio
async def test_reorder_suggestions_an_empty_list_is_not_an_error(monkeypatch):
    """No product below its minimum stock is the best of all news.

    It has to arrive as an empty list, not as a 404.
    """
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ReorderSuggestionRepository.get_reorder_suggestions",
        AsyncMock(return_value=[]),
    )

    assert await ReorderSuggestionService.get_reorder_suggestions(AsyncMock()) == []


# ==========================================
# Transparency for errors
# ==========================================

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repository_path", "call"),
    [
        (
            "SupplierRepository.get_suppliers",
            lambda session: SupplierService.get_suppliers(session),
        ),
        (
            "SupplierRepository.create_supplier",
            lambda session: SupplierService.create_supplier(_new_supplier(), session),
        ),
        (
            "SupplierRepository.add_supplied_product",
            lambda session: SupplierService.add_supplied_product(
                "S-001", _supply_range(), session
            ),
        ),
        (
            "ReorderSuggestionRepository.get_reorder_suggestions",
            lambda session: ReorderSuggestionService.get_reorder_suggestions(session),
        ),
    ],
)
async def test_a_database_error_is_not_swallowed(monkeypatch, repository_path, call):
    """A caught DatabaseError would pass as an empty result.

    The client would see "no data" instead of "the query failed".
    """
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.{repository_path}",
        AsyncMock(side_effect=DatabaseError("connection lost")),
    )

    with pytest.raises(DatabaseError):
        await call(AsyncMock())


# ==========================================
# Real-time events
# ==========================================
# manager.send_event is patched at its point of definition (core.websocket) — the same
# instance service_procurement imports as `manager`. One event per operation, none on a
# rollback.

@pytest.mark.asyncio
async def test_create_supplier_sends_an_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.create_supplier",
        AsyncMock(return_value=_supplier(id="S-001")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await SupplierService.create_supplier(_new_supplier(), AsyncMock())

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "supplier", "trigger": "supplier_created",
        "reference": "S-001", "ids": ["S-001"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_update_supplier_sends_an_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.update_supplier",
        AsyncMock(return_value=_supplier(id="S-001", email="new@northern-supply.example")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await SupplierService.update_supplier(
        "S-001", SupplierUpdate(email="new@northern-supply.example"), AsyncMock()
    )

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "supplier", "trigger": "supplier_updated",
        "reference": "S-001", "ids": ["S-001"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_update_supplier_sends_no_event_for_an_unknown_supplier(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.update_supplier", AsyncMock(return_value=None)
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(NotFoundError):
        await SupplierService.update_supplier(
            "S-nope", SupplierUpdate(email="new@northern-supply.example"), AsyncMock()
        )

    send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_supply_range_sends_an_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.add_supplied_product",
        AsyncMock(return_value=_supply_response()),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await SupplierService.add_supplied_product("S-001", _supply_range(), AsyncMock())

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "supplier", "trigger": "supply_range_changed",
        "reference": "S-001", "ids": ["S-001"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_supply_range_sends_no_event_for_a_missing_product(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.SupplierRepository.add_supplied_product",
        AsyncMock(side_effect=NotFoundError("A product with the number 'ACME-9999' does not exist.")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(NotFoundError):
        await SupplierService.add_supplied_product(
            "S-001", _supply_range(productNumber="ACME-9999"), AsyncMock()
        )

    send_event.assert_not_awaited()
