"""Unit tests of the asset services — against mocks, without a database.

Tested is exactly what the service decides itself:

* the translation of a `None` from the repository into a `NotFoundError` — the only own
  contribution of this layer on nearly every method
* that path parameters, write models and the employee id from the token are passed on
  unchanged and by name
* that exceptions from the repository are not swallowed — the global handler in `main.py`
  turns them into the matching status code
* which event goes out, and that none goes out when the operation failed

Not here: the status derivation, the reservation on the release and the excess over the
document. All three are conditions of the Cypher query or pure functions of the repository
and are covered in `test_repository_assets.py`.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from core.exceptions import BusinessLogicError, DatabaseError, NotFoundError
from domains.assets.schemas_assets import (
    Asset,
    AssetCreate,
    AssetCreated,
    AssetDraftConfirmation,
    AssetListItem,
    AssetRelease,
    AssetReleaseRequest,
    AssetReleaseResponse,
    AssetUpdate,
    AssetUpdated,
    ComponentLineCreate,
    ComponentLineDeleted,
    ServiceCompletion,
    ServiceCompletionRequest,
    ServiceForecast,
    SparePartsLink,
    SparePartsLinkCreated,
)
from domains.assets.service_assets import AssetService, AssetServiceForecastService

# Base path for every mock: patch where the name is ACTUALLY used (service_assets.py
# imports the repositories via "from .repository_assets import ..." -> the names therefore
# live in the service_assets module).
SERVICE_MODULE = "domains.assets.service_assets"
MANAGER_MODULE = "core.websocket"


def _asset(**overrides) -> Asset:
    """Builds a complete asset; individual fields can be overridden."""
    data: dict = {
        "serialNumber": "SN-2026-0001",
        "internalNumber": "ACME-SN-0001",
        "status": "shipped",
        "productNumber": "ACME-1000",
        "customer": {"id": "C-1001", "name": "Northwind Systems GmbH"},
        "documentNumber": "OC-2026-0001",
        "shippedOn": date(2026, 3, 18),
    }
    data.update(overrides)
    return Asset.model_validate(data)


def _release_response(**overrides) -> AssetReleaseResponse:
    data: dict = {
        "serialNumber": "SN-2026-0001",
        "status": "released",
        "release": AssetRelease(
            released=True, releasedOn=date(2026, 3, 10), employee="Dana Weber"
        ),
    }
    data.update(overrides)
    return AssetReleaseResponse(**data)


def _forecast(**overrides) -> ServiceForecast:
    data: dict = {
        "componentInstanceId": "SN-2026-0001_ACME-2003",
        "accountManager": "Dana Weber",
        "customer": "Northwind Systems GmbH",
        "serialNumber": "SN-2026-0001",
        "wearPart": "Filter Cartridge",
        "replacementDueOn": date(2027, 3, 18),
    }
    data.update(overrides)
    return ServiceForecast(**data)


@pytest.fixture
def sent_events(monkeypatch):
    """Collects every event instead of pushing it into a websocket."""
    events: list[dict] = []

    async def fake_send_event(event: dict) -> None:
        events.append(event)

    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", fake_send_event)
    return events


# ==========================================
# Reading
# ==========================================

@pytest.mark.asyncio
async def test_get_assets_passes_every_filter_through(monkeypatch):
    """The service decides nothing here — but a swapped keyword would silently deliver the
    wrong list."""
    repository = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.AssetRepository.get_assets", repository)
    session = AsyncMock()

    await AssetService.get_assets(
        session, customerId="C-1001", status="shipped", search="SN-2026"
    )

    repository.assert_awaited_once_with(
        session, customerId="C-1001", status="shipped", search="SN-2026"
    )


@pytest.mark.asyncio
async def test_get_assets_without_filters_is_no_error(monkeypatch):
    repository = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.AssetRepository.get_assets", repository)

    assert await AssetService.get_assets(AsyncMock()) == []


@pytest.mark.asyncio
async def test_get_assets_delivers_the_hits_unchanged(monkeypatch):
    hits = [AssetListItem(serialNumber="SN-2026-0001", status="shipped")]
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.get_assets", AsyncMock(return_value=hits)
    )

    assert await AssetService.get_assets(AsyncMock()) == hits


@pytest.mark.asyncio
async def test_get_asset_delivers_the_asset(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.get_asset", AsyncMock(return_value=_asset())
    )

    asset = await AssetService.get_asset("SN-2026-0001", AsyncMock())

    assert asset.serialNumber == "SN-2026-0001"


@pytest.mark.asyncio
async def test_get_asset_raises_not_found_on_none(monkeypatch):
    """The repository delivers `None`. Whether that becomes a 404 is a business decision,
    and it is made here."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.get_asset", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError, match="SN-2026-9999"):
        await AssetService.get_asset("SN-2026-9999", AsyncMock())


@pytest.mark.asyncio
async def test_get_bom_distinguishes_an_unknown_asset_from_an_empty_one(monkeypatch):
    """`[]` is a valid bill of materials — `None` means the asset does not exist."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.get_bom", AsyncMock(return_value=[])
    )
    assert await AssetService.get_bom("SN-2026-0009", AsyncMock()) == []

    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.get_bom", AsyncMock(return_value=None)
    )
    with pytest.raises(NotFoundError):
        await AssetService.get_bom("SN-2026-9999", AsyncMock())


# ==========================================
# Creating
# ==========================================

@pytest.mark.asyncio
async def test_create_asset_passes_the_input_through(monkeypatch, sent_events):
    created = AssetCreated(serialNumber="SN-2026-0004", status="planned")
    repository = AsyncMock(return_value=created)
    monkeypatch.setattr(f"{SERVICE_MODULE}.AssetRepository.create_asset", repository)
    session = AsyncMock()
    request = AssetCreate(documentNumber="OC-2026-0001")

    result = await AssetService.create_asset(request, session)

    repository.assert_awaited_once_with(request, session)
    assert result.serialNumber == "SN-2026-0004"


@pytest.mark.asyncio
async def test_create_asset_does_not_catch_a_wrong_document_type(monkeypatch, sent_events):
    """The check belongs in the same transaction as the write. The service passes it
    through, and the global handler makes a 400 of it."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.create_asset",
        AsyncMock(side_effect=BusinessLogicError("wrong document type")),
    )

    with pytest.raises(BusinessLogicError):
        await AssetService.create_asset(AssetCreate(documentNumber="QU-2026-0001"), AsyncMock())


@pytest.mark.asyncio
async def test_create_asset_does_not_catch_an_unknown_document(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.create_asset",
        AsyncMock(side_effect=NotFoundError("document missing")),
    )

    with pytest.raises(NotFoundError):
        await AssetService.create_asset(AssetCreate(documentNumber="OC-9999"), AsyncMock())


# ==========================================
# Release
# ==========================================

@pytest.mark.asyncio
async def test_set_release_passes_request_and_key_through(monkeypatch, sent_events):
    """The employee id comes from the token, not from the body — a client must never claim
    who released."""
    repository = AsyncMock(return_value=_release_response())
    monkeypatch.setattr(f"{SERVICE_MODULE}.AssetRepository.set_release", repository)
    session = AsyncMock()
    request = AssetReleaseRequest(released=True, note="checked")

    await AssetService.set_release("SN-2026-0001", request, "2", session)

    repository.assert_awaited_once_with("SN-2026-0001", request, "2", session)


@pytest.mark.asyncio
async def test_set_release_raises_not_found_on_none(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.set_release", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError, match="SN-2026-9999"):
        await AssetService.set_release(
            "SN-2026-9999", AssetReleaseRequest(released=True), "2", AsyncMock()
        )


@pytest.mark.asyncio
async def test_set_release_does_not_catch_a_withdrawal_after_shipping(monkeypatch, sent_events):
    """In business terms a machine already delivered cannot be declared unchecked
    retroactively. The check needs the stored state and therefore sits in the
    repository."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.set_release",
        AsyncMock(side_effect=BusinessLogicError("already shipped")),
    )

    with pytest.raises(BusinessLogicError):
        await AssetService.set_release(
            "SN-2026-0001", AssetReleaseRequest(released=False), "2", AsyncMock()
        )


@pytest.mark.asyncio
async def test_set_release_does_not_catch_an_unknown_employee(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.set_release",
        AsyncMock(side_effect=NotFoundError("employee missing")),
    )

    with pytest.raises(NotFoundError):
        await AssetService.set_release(
            "SN-2026-0001", AssetReleaseRequest(released=True), "999", AsyncMock()
        )


# ==========================================
# Updating
# ==========================================

@pytest.mark.asyncio
async def test_update_asset_passes_the_changes_through(monkeypatch, sent_events):
    updated = AssetUpdated(**_asset().model_dump(), createdComponents=0)
    repository = AsyncMock(return_value=updated)
    monkeypatch.setattr(f"{SERVICE_MODULE}.AssetRepository.update_asset", repository)
    session = AsyncMock()
    request = AssetUpdate(installedOn=date(2026, 3, 20))

    result = await AssetService.update_asset("SN-2026-0001", request, session)

    repository.assert_awaited_once_with("SN-2026-0001", request, session)
    assert result.createdComponents == 0


@pytest.mark.asyncio
async def test_update_asset_raises_not_found_on_none(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.update_asset", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError, match="SN-2026-9999"):
        await AssetService.update_asset(
            "SN-2026-9999", AssetUpdate(installedOn=date(2026, 3, 20)), AsyncMock()
        )


@pytest.mark.asyncio
async def test_update_asset_does_not_catch_an_empty_request(monkeypatch, sent_events):
    """An empty PATCH is a client error, and the message naming it comes out of the
    repository, which builds the SET clause."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.update_asset",
        AsyncMock(side_effect=BusinessLogicError("no fields")),
    )

    with pytest.raises(BusinessLogicError):
        await AssetService.update_asset("SN-2026-0001", AssetUpdate(), AsyncMock())


# ==========================================
# Bill of materials of the single asset
# ==========================================

@pytest.mark.asyncio
async def test_set_component_passes_the_line_through(monkeypatch, sent_events):
    result = {
        "productNumber": "ACME-2001", "label": "Sealing Ring 40mm",
        "quantity": 2.0, "installedOn": None, "was_updated": False,
    }
    repository = AsyncMock(return_value=result)
    monkeypatch.setattr(f"{SERVICE_MODULE}.AssetRepository.set_component", repository)
    session = AsyncMock()
    line = ComponentLineCreate(productNumber="ACME-2001", quantity=Decimal("2"))

    answer = await AssetService.set_component("SN-2026-0001", line, session)

    repository.assert_awaited_once_with("SN-2026-0001", line, session)
    assert answer["was_updated"] is False


@pytest.mark.asyncio
async def test_set_component_raises_not_found_on_none(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.set_component", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError, match="SN-2026-9999"):
        await AssetService.set_component(
            "SN-2026-9999",
            ComponentLineCreate(productNumber="ACME-2001", quantity=Decimal("2")),
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_set_component_does_not_catch_a_released_bom(monkeypatch, sent_events):
    """After the release the bill of materials is locked — otherwise it could be changed
    after stock had already been reserved for it."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.set_component",
        AsyncMock(side_effect=BusinessLogicError("already released")),
    )

    with pytest.raises(BusinessLogicError):
        await AssetService.set_component(
            "SN-2026-0001",
            ComponentLineCreate(productNumber="ACME-2001", quantity=Decimal("2")),
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_delete_component_delivers_the_remaining_lines(monkeypatch, sent_events):
    deleted = ComponentLineDeleted(
        serialNumber="SN-2026-0001", productNumber="ACME-2001", remainingLines=3
    )
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.delete_component", AsyncMock(return_value=deleted)
    )

    result = await AssetService.delete_component("SN-2026-0001", "ACME-2001", AsyncMock())

    assert result.remainingLines == 3


@pytest.mark.asyncio
async def test_delete_component_raises_not_found_on_none(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.delete_component", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError, match="SN-2026-9999"):
        await AssetService.delete_component("SN-2026-9999", "ACME-2001", AsyncMock())


# ==========================================
# Drafts
# ==========================================

@pytest.mark.asyncio
async def test_confirm_draft_passes_the_employee_from_the_token(monkeypatch, sent_events):
    created = [AssetCreated(serialNumber="SN-2026-0002", status="released")]
    repository = AsyncMock(return_value=created)
    monkeypatch.setattr(f"{SERVICE_MODULE}.AssetRepository.confirm_draft", repository)
    session = AsyncMock()
    confirmation = AssetDraftConfirmation(
        documentNumber="OC-2026-0002",
        components=[ComponentLineCreate(productNumber="ACME-2001", quantity=Decimal("2"))],
    )

    result = await AssetService.confirm_draft(confirmation, "2", session)

    repository.assert_awaited_once_with(confirmation, "2", session)
    assert result[0].status == "released"


@pytest.mark.asyncio
async def test_confirm_draft_does_not_catch_a_document_already_confirmed(
    monkeypatch, sent_events
):
    """A document becomes at most one asset. The check reads the stored state and therefore
    belongs in the transaction."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.confirm_draft",
        AsyncMock(side_effect=BusinessLogicError("already confirmed")),
    )

    with pytest.raises(BusinessLogicError):
        await AssetService.confirm_draft(
            AssetDraftConfirmation(documentNumber="OC-2026-0001", components=[]),
            "2",
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_link_spare_parts_passes_the_serial_numbers_through(monkeypatch, sent_events):
    linked = SparePartsLinkCreated(
        documentNumber="OC-2026-0003", serialNumbers=["SN-2026-0001"]
    )
    repository = AsyncMock(return_value=linked)
    monkeypatch.setattr(f"{SERVICE_MODULE}.AssetRepository.link_spare_parts", repository)
    session = AsyncMock()
    link = SparePartsLink(serialNumbers=["SN-2026-0001"])

    result = await AssetService.link_spare_parts("OC-2026-0003", link, session)

    repository.assert_awaited_once_with("OC-2026-0003", link, session)
    assert result.serialNumbers == ["SN-2026-0001"]


@pytest.mark.asyncio
async def test_link_spare_parts_raises_not_found_on_none(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.link_spare_parts", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError, match="OC-9999"):
        await AssetService.link_spare_parts(
            "OC-9999", SparePartsLink(serialNumbers=["SN-2026-0001"]), AsyncMock()
        )


# ==========================================
# Service forecast
# ==========================================

@pytest.mark.asyncio
async def test_the_forecast_passes_the_session_through(monkeypatch):
    repository = AsyncMock(return_value=[_forecast()])
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetServiceRepository.get_due_services", repository
    )
    session = AsyncMock()

    result = await AssetServiceForecastService.get_due_services(session)

    repository.assert_awaited_once_with(session)
    assert result[0].wearPart == "Filter Cartridge"


@pytest.mark.asyncio
async def test_set_completion_passes_the_result_through(monkeypatch, sent_events):
    stored = ServiceCompletion(completedAt=date(2026, 6, 1), technicianInitials="MF")
    repository = AsyncMock(return_value=stored)
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetServiceRepository.set_completion", repository
    )
    session = AsyncMock()
    request = ServiceCompletionRequest(technicianInitials="MF")

    result = await AssetServiceForecastService.set_completion(
        "SN-2026-0001_ACME-2003", request, session
    )

    repository.assert_awaited_once_with("SN-2026-0001_ACME-2003", request, session)
    assert result.completedAt == date(2026, 6, 1)


@pytest.mark.asyncio
async def test_set_completion_raises_not_found_on_none(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetServiceRepository.set_completion",
        AsyncMock(return_value=None),
    )

    with pytest.raises(NotFoundError, match="SN-2026-0001_NOPE"):
        await AssetServiceForecastService.set_completion(
            "SN-2026-0001_NOPE", ServiceCompletionRequest(), AsyncMock()
        )


@pytest.mark.asyncio
async def test_the_forecast_does_not_catch_a_database_error(monkeypatch):
    """A failed graph query is a 500 and must not be answered as an empty list."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetServiceRepository.get_due_services",
        AsyncMock(side_effect=DatabaseError("query failed")),
    )

    with pytest.raises(DatabaseError):
        await AssetServiceForecastService.get_due_services(AsyncMock())


# ==========================================
# Events
# ==========================================

@pytest.mark.asyncio
async def test_create_asset_sends_an_event(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.create_asset",
        AsyncMock(return_value=AssetCreated(serialNumber="SN-2026-0004", status="planned")),
    )

    await AssetService.create_asset(AssetCreate(documentNumber="OC-2026-0001"), AsyncMock())

    assert sent_events[0]["trigger"] == "asset_created"
    assert sent_events[0]["ids"] == ["SN-2026-0004"]


@pytest.mark.asyncio
async def test_create_asset_sends_no_event_on_a_duplicate(monkeypatch, sent_events):
    """An event announcing something that never came about would make every open client
    reload for nothing."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.create_asset",
        AsyncMock(side_effect=BusinessLogicError("duplicate")),
    )

    with pytest.raises(BusinessLogicError):
        await AssetService.create_asset(AssetCreate(documentNumber="OC-2026-0001"), AsyncMock())

    assert sent_events == []


@pytest.mark.asyncio
async def test_release_and_withdrawal_report_their_own_trigger(monkeypatch, sent_events):
    """Both directions have to stay distinguishable — the release is a formally relevant
    act."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.set_release",
        AsyncMock(return_value=_release_response()),
    )

    await AssetService.set_release(
        "SN-2026-0001", AssetReleaseRequest(released=True), "2", AsyncMock()
    )
    await AssetService.set_release(
        "SN-2026-0001", AssetReleaseRequest(released=False), "2", AsyncMock()
    )

    assert [event["trigger"] for event in sent_events] == [
        "release_granted", "release_withdrawn"
    ]


@pytest.mark.asyncio
async def test_set_release_sends_no_event_for_an_unknown_asset(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.set_release", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await AssetService.set_release(
            "SN-2026-9999", AssetReleaseRequest(released=True), "2", AsyncMock()
        )

    assert sent_events == []


@pytest.mark.asyncio
async def test_update_asset_sends_an_event(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.update_asset",
        AsyncMock(return_value=AssetUpdated(**_asset().model_dump(), createdComponents=2)),
    )

    await AssetService.update_asset(
        "SN-2026-0001", AssetUpdate(shippedOn=date(2026, 3, 18)), AsyncMock()
    )

    assert sent_events[0]["entity"] == "asset"
    assert sent_events[0]["trigger"] == "asset_changed"


@pytest.mark.asyncio
async def test_update_asset_sends_no_event_for_an_unknown_asset(monkeypatch, sent_events):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.update_asset", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await AssetService.update_asset(
            "SN-2026-9999", AssetUpdate(shippedOn=date(2026, 3, 18)), AsyncMock()
        )

    assert sent_events == []


@pytest.mark.asyncio
async def test_both_bom_operations_send_the_same_trigger(monkeypatch, sent_events):
    """Adding and striking change the same list. A client refreshing the bill of materials
    must not have to distinguish two cases."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.set_component",
        AsyncMock(return_value={
            "productNumber": "ACME-2001", "label": None,
            "quantity": 2.0, "installedOn": None, "was_updated": False,
        }),
    )
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.delete_component",
        AsyncMock(return_value=ComponentLineDeleted(
            serialNumber="SN-2026-0001", productNumber="ACME-2001", remainingLines=0
        )),
    )

    await AssetService.set_component(
        "SN-2026-0001",
        ComponentLineCreate(productNumber="ACME-2001", quantity=Decimal("2")),
        AsyncMock(),
    )
    await AssetService.delete_component("SN-2026-0001", "ACME-2001", AsyncMock())

    assert [event["trigger"] for event in sent_events] == ["bom_changed", "bom_changed"]


@pytest.mark.asyncio
async def test_confirm_draft_reports_the_document_as_the_reference(monkeypatch, sent_events):
    """The confirmation acts on a document; the serial numbers created out of it belong in
    `ids`, not into the reference."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetRepository.confirm_draft",
        AsyncMock(return_value=[
            AssetCreated(serialNumber="SN-2026-0002", status="released")
        ]),
    )

    await AssetService.confirm_draft(
        AssetDraftConfirmation(documentNumber="OC-2026-0002", components=[]),
        "2",
        AsyncMock(),
    )

    assert sent_events[0]["reference"] == "OC-2026-0002"
    assert sent_events[0]["ids"] == ["SN-2026-0002"]


@pytest.mark.asyncio
async def test_set_completion_sends_a_service_event(monkeypatch, sent_events):
    """A completed service changes the forecast, not the asset — the entity therefore
    reads `service`."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetServiceRepository.set_completion",
        AsyncMock(return_value=ServiceCompletion(completedAt=date(2026, 6, 1))),
    )

    await AssetServiceForecastService.set_completion(
        "SN-2026-0001_ACME-2003", ServiceCompletionRequest(), AsyncMock()
    )

    assert sent_events[0]["entity"] == "service"
    assert sent_events[0]["trigger"] == "service_completed"


@pytest.mark.asyncio
async def test_set_completion_sends_no_event_for_an_unknown_component(
    monkeypatch, sent_events
):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.AssetServiceRepository.set_completion",
        AsyncMock(return_value=None),
    )

    with pytest.raises(NotFoundError):
        await AssetServiceForecastService.set_completion(
            "SN-2026-0001_NOPE", ServiceCompletionRequest(), AsyncMock()
        )

    assert sent_events == []
