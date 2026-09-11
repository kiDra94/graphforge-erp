"""Unit tests of the catalog service layer — repository mocked, no Neo4j needed."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from core.exceptions import BusinessLogicError, DuplicateKeyError, NotFoundError
from domains.catalog.schemas_catalog import (
    BomLineCreate,
    BomLineDeleted,
    CategoryOption,
    Product,
    ProductCreate,
    ProductUpdate,
)
from domains.catalog.service_catalog import BomService, CategoryService, ProductService

SERVICE_MODULE = "domains.catalog.service_catalog"
MANAGER_MODULE = "core.websocket"


def _product(**overrides) -> Product:
    data = {
        "number": "ACME-2003",
        "label": "Filter Cartridge",
        "unit": "pcs",
        "listPrice": Decimal("24.50"),
    }
    data.update(overrides)
    return Product(**data)


def _create_payload(**overrides) -> ProductCreate:
    data = {
        "number": "ACME-2003",
        "label": "Filter Cartridge",
        "unit": "pcs",
        "minStock": 20,
        "targetStock": 80,
        "listPrice": Decimal("24.50"),
    }
    data.update(overrides)
    return ProductCreate(**data)


# ==========================================
# ProductService — the 404 translation
# ==========================================

@pytest.mark.asyncio
async def test_get_product_not_found_is_a_not_found_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.get_product", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await ProductService.get_product("nope", AsyncMock())


@pytest.mark.asyncio
async def test_get_product_found_is_passed_through(monkeypatch):
    expected = _product()
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.get_product", AsyncMock(return_value=expected)
    )

    assert await ProductService.get_product("ACME-2003", AsyncMock()) is expected


@pytest.mark.asyncio
async def test_update_product_not_found_is_a_not_found_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.update_product", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await ProductService.update_product("nope", ProductUpdate(label="X"), AsyncMock())


@pytest.mark.asyncio
async def test_delete_product_not_found_is_a_not_found_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.delete_product", AsyncMock(return_value=False)
    )

    with pytest.raises(NotFoundError):
        await ProductService.delete_product("nope", AsyncMock())


@pytest.mark.asyncio
async def test_create_product_lets_a_conflict_through(monkeypatch):
    """The service does not swallow a DuplicateKeyError — it belongs at the client as a 409."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.create_product",
        AsyncMock(side_effect=DuplicateKeyError("already exists")),
    )

    with pytest.raises(DuplicateKeyError):
        await ProductService.create_product(_create_payload(), AsyncMock())


# ==========================================
# ProductService — pass-through of the filters
# ==========================================

@pytest.mark.asyncio
async def test_get_products_passes_the_filters_through_unchanged(monkeypatch):
    repo = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.ProductRepository.get_products", repo)
    session = AsyncMock()

    await ProductService.get_products(session, search="filter", type="Part", active=False)

    repo.assert_awaited_once_with(session, search="filter", type="Part", active=False)


@pytest.mark.asyncio
async def test_get_products_empty_list_is_not_an_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.get_products", AsyncMock(return_value=[])
    )

    assert await ProductService.get_products(AsyncMock()) == []


@pytest.mark.asyncio
async def test_check_product_exists_is_passed_through(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.check_product_exists", AsyncMock(return_value=True)
    )

    assert await ProductService.check_product_exists("ACME-2003", AsyncMock()) is True


# ==========================================
# BomService
# ==========================================

def test_bom_line_create_requires_a_positive_quantity():
    with pytest.raises(ValidationError):
        BomLineCreate(componentNumber="ACME-2002", quantity=0)


@pytest.mark.asyncio
async def test_get_bom_for_a_missing_product_is_a_not_found_error(monkeypatch):
    """Without the existence check, a missing product would answer with an empty 200."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.check_product_exists", AsyncMock(return_value=False)
    )
    repo = AsyncMock()
    monkeypatch.setattr(f"{SERVICE_MODULE}.BomRepository.get_bom", repo)

    with pytest.raises(NotFoundError):
        await BomService.get_bom("nope", 1, AsyncMock())

    repo.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_bom_passes_the_result_through(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.check_product_exists", AsyncMock(return_value=True)
    )
    expected = [{"quantity": Decimal("2"), "component": _product(), "subComponents": []}]
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.BomRepository.get_bom", AsyncMock(return_value=expected)
    )

    assert await BomService.get_bom("ACME-1000", 1, AsyncMock()) is expected


@pytest.mark.asyncio
async def test_add_component_rejects_a_self_reference(monkeypatch):
    """Caught in the service for the error message; the query would reject it too."""
    repo = AsyncMock()
    monkeypatch.setattr(f"{SERVICE_MODULE}.BomRepository.add_component", repo)

    with pytest.raises(BusinessLogicError):
        await BomService.add_component(
            "ACME-1000", BomLineCreate(componentNumber="ACME-1000", quantity=1), AsyncMock()
        )

    repo.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_component_lets_a_missing_assembly_through(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.BomRepository.add_component",
        AsyncMock(side_effect=NotFoundError("does not exist")),
    )

    with pytest.raises(NotFoundError):
        await BomService.add_component(
            "nope", BomLineCreate(componentNumber="ACME-2002", quantity=1), AsyncMock()
        )


@pytest.mark.asyncio
async def test_add_component_passes_the_result_through(monkeypatch):
    expected = {
        "quantity": 2, "component": _product(), "subComponents": [], "wasUpdated": False,
    }
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.BomRepository.add_component", AsyncMock(return_value=expected)
    )

    result = await BomService.add_component(
        "ACME-1000", BomLineCreate(componentNumber="ACME-2002", quantity=2), AsyncMock()
    )

    assert result is expected


@pytest.mark.asyncio
async def test_delete_component_translates_none_into_not_found(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.BomRepository.delete_component", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await BomService.delete_component("ACME-1000", "ACME-2002", AsyncMock())


@pytest.mark.asyncio
async def test_delete_component_reports_an_empty_bill_of_materials(monkeypatch):
    """remainingLines: 0 is how the client learns the assembly became a part again."""
    expected = BomLineDeleted(
        number="ACME-1000", componentNumber="ACME-2002", remainingLines=0
    )
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.BomRepository.delete_component", AsyncMock(return_value=expected)
    )

    result = await BomService.delete_component("ACME-1000", "ACME-2002", AsyncMock())

    assert result.remainingLines == 0


def test_bom_line_deleted_rejects_a_negative_count():
    with pytest.raises(ValidationError):
        BomLineDeleted(number="A", componentNumber="B", remainingLines=-1)


# ==========================================
# CategoryService
# ==========================================

@pytest.mark.asyncio
async def test_get_hierarchy_passes_the_repository_result_through(monkeypatch):
    expected = [CategoryOption(id=1, name="Hardware")]
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.CategoryRepository.get_hierarchy", AsyncMock(return_value=expected)
    )

    assert await CategoryService.get_hierarchy(AsyncMock()) is expected


# ==========================================
# REAL-TIME EVENTS
# ==========================================
# One event per operation, none on a failure. Patched at the definition site
# (core.websocket) — the same instance service_catalog imports as `manager`.

@pytest.mark.asyncio
async def test_create_product_sends_an_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.create_product", AsyncMock(return_value=_product())
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await ProductService.create_product(_create_payload(), AsyncMock())

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "product", "trigger": "product_created",
        "reference": "ACME-2003", "ids": ["ACME-2003"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_create_product_sends_no_event_on_a_conflict(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.create_product",
        AsyncMock(side_effect=DuplicateKeyError("already exists")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(DuplicateKeyError):
        await ProductService.create_product(_create_payload(), AsyncMock())

    send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_product_sends_an_event_with_the_old_number(monkeypatch):
    """A rename must report the OLD number — that is the id the clients still hold."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.update_product",
        AsyncMock(return_value=_product(number="ACME-9999")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await ProductService.update_product(
        "ACME-2003", ProductUpdate(number="ACME-9999"), AsyncMock()
    )

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "product", "trigger": "product_updated",
        "reference": "ACME-2003", "ids": ["ACME-2003"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_update_product_sends_no_event_for_an_unknown_product(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.update_product", AsyncMock(return_value=None)
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(NotFoundError):
        await ProductService.update_product("nope", ProductUpdate(label="X"), AsyncMock())

    send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_product_sends_an_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.delete_product", AsyncMock(return_value=True)
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await ProductService.delete_product("ACME-2003", AsyncMock())

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "product", "trigger": "product_deleted",
        "reference": "ACME-2003", "ids": ["ACME-2003"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_delete_product_sends_no_event_for_an_unknown_product(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ProductRepository.delete_product", AsyncMock(return_value=False)
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(NotFoundError):
        await ProductService.delete_product("nope", AsyncMock())

    send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_component_sends_an_event_naming_both_products(monkeypatch):
    """Both sides changed — a client showing either of them has to reload."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.BomRepository.add_component",
        AsyncMock(return_value={"quantity": 2, "component": _product(),
                                "subComponents": [], "wasUpdated": False}),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await BomService.add_component(
        "ACME-1000", BomLineCreate(componentNumber="ACME-2002", quantity=2), AsyncMock()
    )

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "product", "trigger": "bom_changed",
        "reference": "ACME-1000", "ids": ["ACME-1000", "ACME-2002"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_add_component_sends_no_event_on_a_self_reference(monkeypatch):
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(BusinessLogicError):
        await BomService.add_component(
            "ACME-1000", BomLineCreate(componentNumber="ACME-1000", quantity=1), AsyncMock()
        )

    send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_component_sends_an_event_naming_both_products(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.BomRepository.delete_component",
        AsyncMock(return_value=BomLineDeleted(
            number="ACME-1000", componentNumber="ACME-2002", remainingLines=3
        )),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await BomService.delete_component("ACME-1000", "ACME-2002", AsyncMock())

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "product", "trigger": "bom_changed",
        "reference": "ACME-1000", "ids": ["ACME-1000", "ACME-2002"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_delete_component_sends_no_event_for_a_missing_edge(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.BomRepository.delete_component", AsyncMock(return_value=None)
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(NotFoundError):
        await BomService.delete_component("ACME-1000", "ACME-2002", AsyncMock())

    send_event.assert_not_awaited()
