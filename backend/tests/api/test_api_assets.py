"""API tests of the asset routes.

The one thing this level has to prove for this domain is the route order: `/service` and
`/drafts/...` are static paths that have to be registered BEFORE `/{serialNumber}`. FastAPI
evaluates in registration order, so a wrong order would silently take "service" for a serial
number — the route would answer, just the wrong one.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from core.exceptions import BusinessLogicError, NotFoundError
from domains.assets.schemas_assets import (
    Asset,
    AssetCreated,
    AssetDraft,
    AssetRelease,
    AssetReleaseResponse,
    AssetUpdated,
    ComponentLineDeleted,
    ServiceCompletion,
    ServiceForecast,
    SparePartsLinkCreated,
)

ASSET_SERVICE = "domains.assets.router_assets.AssetService"
FORECAST_SERVICE = "domains.assets.router_assets.AssetServiceForecastService"


def _asset(**overrides) -> Asset:
    data: dict = {"serialNumber": "SN-2026-0001", "status": "shipped"}
    data.update(overrides)
    return Asset.model_validate(data)


# ==========================================
# Route order
# ==========================================

@pytest.mark.asyncio
async def test_the_service_path_is_not_taken_for_a_serial_number(async_client, auth, monkeypatch):
    """`/api/assets/service` has to reach the forecast, not `get_asset("service")`."""
    forecast = AsyncMock(return_value=[])
    detail = AsyncMock(return_value=_asset())
    monkeypatch.setattr(f"{FORECAST_SERVICE}.get_due_services", forecast)
    monkeypatch.setattr(f"{ASSET_SERVICE}.get_asset", detail)

    await async_client.get("/api/assets/service", headers=auth("Sales"))

    assert forecast.await_count == 1
    assert detail.await_count == 0


@pytest.mark.asyncio
async def test_the_draft_paths_are_not_taken_for_a_serial_number(async_client, auth, monkeypatch):
    drafts = AsyncMock(return_value=[])
    detail = AsyncMock(return_value=_asset())
    monkeypatch.setattr(f"{ASSET_SERVICE}.get_asset_drafts", drafts)
    monkeypatch.setattr(f"{ASSET_SERVICE}.get_spare_parts_drafts", AsyncMock(return_value=[]))
    monkeypatch.setattr(f"{ASSET_SERVICE}.get_asset", detail)

    await async_client.get("/api/assets/drafts/assets", headers=auth("Engineering"))
    await async_client.get("/api/assets/drafts/spare-parts", headers=auth("Engineering"))

    assert drafts.await_count == 1
    assert detail.await_count == 0


# ==========================================
# Reading
# ==========================================

@pytest.mark.asyncio
async def test_the_asset_list_passes_every_filter_on(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{ASSET_SERVICE}.get_assets", service)

    await async_client.get(
        "/api/assets?customerId=C-1001&status=shipped&search=SN-2026", headers=auth("Sales")
    )

    assert service.await_args is not None
    kwargs = service.await_args.kwargs
    assert kwargs["customerId"] == "C-1001"
    assert kwargs["status"] == "shipped"
    assert kwargs["search"] == "SN-2026"


@pytest.mark.asyncio
async def test_an_unknown_status_answers_422(async_client, auth):
    """The Literal rejects it, so an unknown value does not turn into a silently empty
    list."""
    response = await async_client.get("/api/assets?status=in_transit", headers=auth("Sales"))

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_serial_number_answers_404(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.get_asset", AsyncMock(side_effect=NotFoundError("no such asset"))
    )

    response = await async_client.get("/api/assets/SN-9999", headers=auth("Sales"))

    assert response.status_code == 404


# ==========================================
# Release
# ==========================================

@pytest.mark.asyncio
async def test_the_release_needs_engineering(async_client, auth):
    response = await async_client.patch(
        "/api/assets/SN-2026-0002/release", json={"released": True}, headers=auth("Sales")
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_releasing_employee_comes_from_the_token(async_client, auth, monkeypatch):
    """A client must never claim who released — the release is a formally relevant act."""
    service = AsyncMock(return_value=AssetReleaseResponse(
        serialNumber="SN-2026-0002", status="released",
        release=AssetRelease(released=True, releasedOn=date(2026, 9, 10), employee="Dana"),
    ))
    monkeypatch.setattr(f"{ASSET_SERVICE}.set_release", service)

    await async_client.patch(
        "/api/assets/SN-2026-0002/release", json={"released": True},
        headers=auth("Engineering", sub="42"),
    )

    assert service.await_args is not None
    assert service.await_args.args[2] == "42"


@pytest.mark.asyncio
async def test_a_note_on_a_withdrawal_answers_422(async_client, auth):
    """The withdrawal deletes the note. Without this check the text would vanish silently and
    the client would believe it stored."""
    response = await async_client.patch(
        "/api/assets/SN-2026-0002/release",
        json={"released": False, "note": "taken back"},
        headers=auth("Engineering"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_insufficient_stock_answers_400(async_client, auth, monkeypatch):
    """All or nothing: a half-reserved asset is no sensible intermediate state."""
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.set_release",
        AsyncMock(side_effect=BusinessLogicError("available stock does not suffice")),
    )

    response = await async_client.patch(
        "/api/assets/SN-2026-0002/release", json={"released": True}, headers=auth("Engineering")
    )

    assert response.status_code == 400


# ==========================================
# Bill of materials of the single asset
# ==========================================

@pytest.mark.asyncio
async def test_adding_a_component_answers_201(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.set_component",
        AsyncMock(return_value={
            "productNumber": "ACME-2001", "label": "Screw", "quantity": Decimal("2"),
            "installedOn": None, "was_updated": False,
        }),
    )

    response = await async_client.post(
        "/api/assets/SN-2026-0002/bom",
        json={"productNumber": "ACME-2001", "quantity": 2},
        headers=auth("Engineering"),
    )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_changing_a_quantity_answers_200(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.set_component",
        AsyncMock(return_value={
            "productNumber": "ACME-2001", "label": "Screw", "quantity": Decimal("5"),
            "installedOn": None, "was_updated": True,
        }),
    )

    response = await async_client.post(
        "/api/assets/SN-2026-0002/bom",
        json={"productNumber": "ACME-2001", "quantity": 5},
        headers=auth("Engineering"),
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_a_released_bom_rejects_a_change_with_400(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.delete_component",
        AsyncMock(side_effect=BusinessLogicError("already released")),
    )

    response = await async_client.delete(
        "/api/assets/SN-2026-0001/bom/ACME-2001", headers=auth("Engineering")
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_deleting_a_component_reports_what_is_left(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.delete_component",
        AsyncMock(return_value=ComponentLineDeleted(
            serialNumber="SN-2026-0002", productNumber="ACME-2001", remainingLines=6
        )),
    )

    response = await async_client.delete(
        "/api/assets/SN-2026-0002/bom/ACME-2001", headers=auth("Engineering")
    )

    assert response.json()["remainingLines"] == 6


# ==========================================
# Drafts
# ==========================================

@pytest.mark.asyncio
async def test_backoffice_may_read_the_drafts_but_not_confirm_them(async_client, auth, monkeypatch):
    """Reading is open because a deep link out of the warehouse view has to show something
    even when an order confirmation only exists as an open draft. Confirming stays with
    engineering."""
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.get_asset_drafts",
        AsyncMock(return_value=[AssetDraft(documentNumber="OC-2026-0002", lines=[])]),
    )

    reading = await async_client.get("/api/assets/drafts/assets", headers=auth("BackOffice"))
    confirming = await async_client.post(
        "/api/assets/drafts/assets/confirm",
        json={"documentNumber": "OC-2026-0002", "components": []},
        headers=auth("BackOffice"),
    )

    assert reading.status_code == 200
    assert confirming.status_code == 403


@pytest.mark.asyncio
async def test_confirming_a_draft_passes_the_employee_from_the_token(
    async_client, auth, monkeypatch
):
    service = AsyncMock(return_value=[AssetCreated(serialNumber="SN-2026-0002", status="released")])
    monkeypatch.setattr(f"{ASSET_SERVICE}.confirm_draft", service)

    response = await async_client.post(
        "/api/assets/drafts/assets/confirm",
        json={
            "documentNumber": "OC-2026-0002",
            "components": [{"productNumber": "ACME-2001", "quantity": 2}],
        },
        headers=auth("Engineering", sub="42"),
    )

    assert response.status_code == 200
    assert service.await_args is not None
    assert service.await_args.args[1] == "42"


@pytest.mark.asyncio
async def test_a_second_confirmation_answers_400(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.confirm_draft",
        AsyncMock(side_effect=BusinessLogicError("already confirmed")),
    )

    response = await async_client.post(
        "/api/assets/drafts/assets/confirm",
        json={"documentNumber": "OC-2026-0002", "components": []},
        headers=auth("Engineering"),
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_linking_spare_parts_demands_at_least_one_serial_number(async_client, auth):
    """An empty list would draw no edge and report success all the same."""
    response = await async_client.post(
        "/api/assets/drafts/spare-parts/OC-2026-0003/link",
        json={"serialNumbers": []},
        headers=auth("Engineering"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_linking_spare_parts_answers_the_linked_numbers(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.link_spare_parts",
        AsyncMock(return_value=SparePartsLinkCreated(
            documentNumber="OC-2026-0003", serialNumbers=["SN-2026-0001"]
        )),
    )

    response = await async_client.post(
        "/api/assets/drafts/spare-parts/OC-2026-0003/link",
        json={"serialNumbers": ["SN-2026-0001"]},
        headers=auth("Engineering"),
    )

    assert response.json()["serialNumbers"] == ["SN-2026-0001"]


# ==========================================
# Service forecast
# ==========================================

@pytest.mark.asyncio
async def test_the_forecast_belongs_to_sales(async_client, auth):
    """It is the list somebody calls the customer from — not a warehouse task."""
    response = await async_client.get("/api/assets/service", headers=auth("Warehouse"))

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_forecast_answers_a_list(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{FORECAST_SERVICE}.get_due_services",
        AsyncMock(return_value=[ServiceForecast(
            componentInstanceId="SN-2026-0001_ACME-2003",
            accountManager="Unassigned", customer="Example Industries GmbH",
            serialNumber="SN-2026-0001", wearPart="Filter Cartridge",
            replacementDueOn=date(2026, 9, 18),
        )]),
    )

    response = await async_client.get("/api/assets/service", headers=auth("Sales"))

    assert response.status_code == 200
    assert response.json()[0]["replacementDueOn"] == "2026-09-18"


@pytest.mark.asyncio
async def test_reporting_a_completion_rejects_a_claimed_date(async_client, auth):
    """The server sets `completedAt`; it doubles as the anchor of the next cycle, and a
    client able to claim it would move the next replacement date along."""
    response = await async_client.put(
        "/api/assets/service/SN-2026-0001_ACME-2003/completion",
        json={"completedAt": "2020-01-01"},
        headers=auth("Sales"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_reporting_a_completion_works_with_an_empty_body(async_client, auth, monkeypatch):
    """Neither initials nor a note are mandatory — reporting alone is the point."""
    monkeypatch.setattr(
        f"{FORECAST_SERVICE}.set_completion",
        AsyncMock(return_value=ServiceCompletion(completedAt=date(2026, 9, 10))),
    )

    response = await async_client.put(
        "/api/assets/service/SN-2026-0001_ACME-2003/completion", json={}, headers=auth("Sales")
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_update_reports_the_number_of_created_components(
    async_client, auth, monkeypatch
):
    """`0` is the normal case. Without the figure the frontend could not tell "nothing to
    do" from "something went wrong"."""
    monkeypatch.setattr(
        f"{ASSET_SERVICE}.update_asset",
        AsyncMock(return_value=AssetUpdated(
            **_asset().model_dump(), createdComponents=0
        )),
    )

    response = await async_client.patch(
        "/api/assets/SN-2026-0001", json={"installedOn": "2026-09-12"}, headers=auth("Sales")
    )

    assert response.status_code == 200
    assert response.json()["createdComponents"] == 0
