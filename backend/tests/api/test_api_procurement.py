"""API tests of the procurement routes.

Two decisions of this domain live at the HTTP level: reading supplier master data is open to
every signed-in user (the list appears in several views), while everything that writes and
the reorder analysis with its purchase prices belong to purchasing alone.
"""

from unittest.mock import AsyncMock

import pytest

from core.exceptions import NotFoundError
from domains.procurement.schemas_procurement import ReorderSuggestion, Supplier

SUPPLIER_SERVICE = "domains.procurement.router_procurement.SupplierService"
REORDER_SERVICE = "domains.procurement.router_procurement.ReorderSuggestionService"


def _supplier(**overrides) -> Supplier:
    data: dict = {"id": "S-001", "name": "Northern Supply GmbH", "city": "Hamburg"}
    data.update(overrides)
    return Supplier(**data)


def _supply_range() -> dict:
    return {
        "productNumber": "ACME-2003", "leadTimeDays": 5,
        "purchasePrice": "11.00", "isPreferredSupplier": True,
    }


@pytest.mark.asyncio
async def test_reading_suppliers_is_open_to_every_signed_in_user(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{SUPPLIER_SERVICE}.get_suppliers", AsyncMock(return_value=[_supplier()])
    )

    response = await async_client.get("/api/suppliers", headers=auth("Warehouse"))

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_search_reaches_the_service_by_name(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SUPPLIER_SERVICE}.get_suppliers", service)

    await async_client.get("/api/suppliers?search=hamburg", headers=auth("Sales"))

    assert service.await_args is not None
    assert service.await_args.kwargs["search"] == "hamburg"


@pytest.mark.asyncio
async def test_an_unknown_supplier_answers_404(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{SUPPLIER_SERVICE}.get_supplier", AsyncMock(side_effect=NotFoundError("no such supplier"))
    )

    response = await async_client.get("/api/suppliers/S-999", headers=auth("Sales"))

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_creating_a_supplier_needs_purchasing(async_client, auth):
    response = await async_client.post(
        "/api/suppliers", json={"name": "Eastern Trading Ltd"}, headers=auth("Sales")
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_creating_a_supplier_answers_201(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{SUPPLIER_SERVICE}.create_supplier",
        AsyncMock(return_value=_supplier(id="S-004", name="Eastern Trading Ltd")),
    )

    response = await async_client.post(
        "/api/suppliers", json={"name": "Eastern Trading Ltd"}, headers=auth("Purchasing")
    )

    assert response.status_code == 201
    assert response.json()["id"] == "S-004"


@pytest.mark.asyncio
async def test_taking_a_product_into_the_range_answers_201(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{SUPPLIER_SERVICE}.add_supplied_product",
        AsyncMock(return_value={
            "supplierId": "S-001", "productNumber": "ACME-2003", "leadTimeDays": 5,
            "purchasePrice": "11.00", "isPreferredSupplier": True, "wasUpdated": False,
        }),
    )

    response = await async_client.post(
        "/api/suppliers/S-001/products", json=_supply_range(), headers=auth("Purchasing")
    )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_changing_the_conditions_answers_200(async_client, auth, monkeypatch):
    """The call is repeatable: a second one for the same combination does not create a
    second source of supply, it overwrites the conditions."""
    monkeypatch.setattr(
        f"{SUPPLIER_SERVICE}.add_supplied_product",
        AsyncMock(return_value={
            "supplierId": "S-001", "productNumber": "ACME-2003", "leadTimeDays": 3,
            "purchasePrice": "10.50", "isPreferredSupplier": True, "wasUpdated": True,
        }),
    )

    response = await async_client.post(
        "/api/suppliers/S-001/products", json=_supply_range(), headers=auth("Purchasing")
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_reorder_analysis_belongs_to_purchasing(async_client, auth):
    """It carries purchase prices in every row — the one reason this list is not open the
    way the supplier master data is."""
    response = await async_client.get("/api/reorder-suggestions", headers=auth("Warehouse"))

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_reorder_analysis_answers_a_list(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{REORDER_SERVICE}.get_reorder_suggestions",
        AsyncMock(return_value=[ReorderSuggestion(
            productNumber="ACME-2003", currentStock=12.0, minStock=20, suggestedQuantity=68.0
        )]),
    )

    response = await async_client.get("/api/reorder-suggestions", headers=auth("Purchasing"))

    assert response.status_code == 200
    assert response.json()[0]["suggestedQuantity"] == 68.0


@pytest.mark.asyncio
async def test_an_empty_reorder_analysis_is_no_error(async_client, auth, monkeypatch):
    """Nothing to reorder is the good case, not a 404."""
    monkeypatch.setattr(
        f"{REORDER_SERVICE}.get_reorder_suggestions", AsyncMock(return_value=[])
    )

    response = await async_client.get("/api/reorder-suggestions", headers=auth("Purchasing"))

    assert response.status_code == 200
    assert response.json() == []
