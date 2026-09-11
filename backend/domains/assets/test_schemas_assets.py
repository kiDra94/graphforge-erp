"""Unit tests of the asset schemas — pure validation, without a database.

Three groups, which no other test level covers:

* **The tolerance of the read model.** `Asset` has to accept nodes out of the graph that
  carry only a serial number, and it must not turn a stored state into an HTTP 500. Tested
  against the field sets that really occur.
* **The tightening at the system boundary.** `AssetCreate`, `AssetUpdate` and
  `AssetReleaseRequest` are the models a client fills. Whatever is forbidden there has to
  fail here, not in the repository.
* **The derived fields.** `status` deliberately carries no default, `release` and
  `components` deliberately do — both are proven, because either decision reads as an
  oversight without a test.

Not here: whether the projection in the repository really delivers `status`. That is what
`test_repository_assets.py` is for.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from neo4j.time import Date as Neo4jDateValue
from neo4j.time import DateTime as Neo4jDateTimeValue
from pydantic import ValidationError

from domains.assets.schemas_assets import (
    Asset,
    AssetCreate,
    AssetDraft,
    AssetListItem,
    AssetRelease,
    AssetReleaseRequest,
    AssetUpdate,
    ComponentLineCreate,
    InstalledComponent,
    ServiceCompletion,
    ServiceCompletionRequest,
    ServiceForecast,
    SparePartsLink,
)


def _asset(**overrides) -> dict:
    """Builds the field set of a complete asset as the projection delivers it."""
    data: dict = {
        "serialNumber": "SN-2026-0001",
        "internalNumber": "ACME-SN-0001",
        "projectNumber": "2026-0001",
        "status": "shipped",
        "productNumber": "ACME-1000",
        "customer": {"id": "C-1001", "name": "Northwind Systems GmbH"},
        "documentNumber": "OC-2026-0001",
        "release": {
            "released": True,
            "releasedOn": date(2026, 3, 10),
            "employee": "Dana Weber",
            "note": None,
        },
        "orderedOn": date(2026, 1, 22),
        "shippedOn": date(2026, 3, 18),
        "installedOn": date(2026, 3, 20),
        "components": [],
    }
    data.update(overrides)
    return data


# ==========================================
# Temporal types out of the driver
# ==========================================

def test_asset_accepts_neo4j_date_objects():
    """The driver delivers neo4j.time.Date, which is no subclass of datetime.date.

    Without the `Neo4jDate` annotation Pydantic rejects the value.
    """
    asset = Asset.model_validate(
        _asset(shippedOn=Neo4jDateValue(2026, 3, 18), orderedOn=Neo4jDateValue(2026, 1, 22))
    )

    assert asset.shippedOn == date(2026, 3, 18)
    assert asset.orderedOn == date(2026, 1, 22)


def test_date_fields_serialise_without_a_time():
    """A business date is a date. Were it typed as a datetime, the answer would carry a
    midnight that nobody entered."""
    asset = Asset.model_validate(_asset())

    assert asset.model_dump()["shippedOn"] == date(2026, 3, 18)
    assert not isinstance(asset.model_dump()["shippedOn"], datetime)


def test_system_timestamps_stay_datetimes():
    """`createdAt` and `updatedAt` are technical timestamps in UTC — unlike the business
    dates they carry a time of day."""
    asset = Asset.model_validate(
        _asset(createdAt=Neo4jDateTimeValue(2026, 3, 18, 9, 30, 0))
    )

    assert asset.createdAt is not None
    assert asset.createdAt.hour == 9


# ==========================================
# Tolerance of the read model
# ==========================================

def test_asset_needs_only_a_serial_number_and_a_status():
    """The graph holds assets from legacy stock that carry nothing but their key.

    Were the read model to demand more, a stored node would turn into an HTTP 500.
    """
    asset = Asset.model_validate({"serialNumber": "SN-2026-0009", "status": "planned"})

    assert asset.serialNumber == "SN-2026-0009"
    assert asset.internalNumber is None
    assert asset.shippedOn is None


def test_asset_without_edges_delivers_empty_blocks():
    """An asset before the release has no releaser, one without a bill of materials no
    twin. The empty block and the empty list are the right answer, not an error."""
    asset = Asset.model_validate({"serialNumber": "SN-2026-0009", "status": "planned"})

    assert asset.release == AssetRelease()
    assert asset.release.released is False
    assert asset.components == []


def test_asset_without_a_status_is_an_error():
    """`status` deliberately carries no default: a caller who forgets the projection is
    meant to fail, not to have an already shipped asset reported as `planned`."""
    with pytest.raises(ValidationError):
        Asset.model_validate({"serialNumber": "SN-2026-0009"})


def test_asset_without_a_serial_number_is_an_error():
    with pytest.raises(ValidationError):
        Asset.model_validate({"status": "planned"})


def test_asset_rejects_an_unknown_status():
    """The three states are a closed set. A fourth value would be a bug in the derivation
    rule, and it must not reach the answer unnoticed."""
    with pytest.raises(ValidationError):
        Asset.model_validate({"serialNumber": "SN-2026-0009", "status": "in_transit"})


def test_asset_ignores_an_additional_property():
    """`.*` in the projection hands over every property of the node — including ones no
    current schema knows about."""
    asset = Asset.model_validate(
        _asset(legacyAssetNumber="42", bomReleased=True)
    )

    assert asset.serialNumber == "SN-2026-0001"
    assert not hasattr(asset, "legacyAssetNumber")


def test_a_missing_release_flag_becomes_false():
    """`bomReleased` can be missing on nodes that came about through MERGE. `false` is the
    honest reading: what was never released is not released."""
    release = AssetRelease.model_validate({"releasedOn": None})

    assert release.released is False


def test_installed_component_needs_a_product_number_and_a_quantity():
    component = InstalledComponent.model_validate(
        {"productNumber": "ACME-2001", "quantity": Decimal("2")}
    )

    assert component.productNumber == "ACME-2001"
    assert component.label is None
    assert component.installedOn is None


def test_installed_component_without_a_quantity_is_invalid():
    """The quantity sits on the `HAS_COMPONENT` edge and is written on every path. Missing
    means the edge is broken, and that is worth a 500."""
    with pytest.raises(ValidationError):
        InstalledComponent.model_validate({"productNumber": "ACME-2001"})


def test_list_item_carries_the_edge_fields():
    item = AssetListItem.model_validate({
        "serialNumber": "SN-2026-0001",
        "status": "shipped",
        "productNumber": "ACME-1000",
        "customerId": "C-1001",
        "documentNumber": "OC-2026-0001",
    })

    assert item.customerId == "C-1001"
    assert item.documentNumber == "OC-2026-0001"


def test_list_item_without_a_document_is_valid():
    """Assets from legacy stock do not carry the edge `BASED_ON_DOCUMENT`."""
    item = AssetListItem.model_validate({"serialNumber": "SN-2026-0009", "status": "planned"})

    assert item.documentNumber is None
    assert item.customerId is None


# ==========================================
# Tightening at the system boundary
# ==========================================

def test_create_demands_a_document():
    with pytest.raises(ValidationError):
        AssetCreate.model_validate({"internalNumber": "ACME-SN-0005"})


def test_create_knows_no_customer_field():
    """The customer is resolved through the document. Were it sent separately, asset and
    document could point at different customers."""
    with pytest.raises(ValidationError):
        AssetCreate.model_validate(
            {"documentNumber": "OC-2026-0001", "customerId": "C-1001"}
        )


def test_create_knows_no_serial_number():
    """The server forms the serial number. A client claiming it would collide with the
    running number over the order."""
    with pytest.raises(ValidationError):
        AssetCreate.model_validate(
            {"documentNumber": "OC-2026-0001", "serialNumber": "SN-2026-0099"}
        )


def test_create_works_without_a_product():
    """Most assets come about without a catalogue product — their bill of materials comes
    from the draft flow."""
    asset = AssetCreate(documentNumber="OC-2026-0001")

    assert asset.productNumber is None
    assert asset.internalNumber is None


def test_update_carries_only_the_two_date_fields():
    """The release has its own endpoint, and the serial number is the key. Both would be
    ambiguous in a PATCH on the asset itself."""
    with pytest.raises(ValidationError):
        AssetUpdate.model_validate({"bomReleased": True})


def test_update_distinguishes_an_omitted_field_from_an_explicit_null():
    """`exclude_unset` is the only thing that keeps a partial update from overwriting the
    stored value with a null it never sent."""
    only_installed = AssetUpdate(installedOn=date(2026, 3, 20))
    explicit_null = AssetUpdate.model_validate({"shippedOn": None})

    assert only_installed.model_dump(exclude_unset=True) == {"installedOn": date(2026, 3, 20)}
    assert explicit_null.model_dump(exclude_unset=True) == {"shippedOn": None}


def test_release_request_needs_a_direction():
    """`released` decides between release and withdrawal — without it the request has no
    meaning."""
    with pytest.raises(ValidationError):
        AssetReleaseRequest.model_validate({"note": "checked"})


def test_release_accepts_a_note():
    request = AssetReleaseRequest(released=True, note="checked, all dimensions verified")

    assert request.note == "checked, all dimensions verified"


def test_withdrawal_rejects_a_note():
    """The withdrawal deletes the release note. Without this check the text sent along
    would vanish silently and the client would believe it stored."""
    with pytest.raises(ValidationError):
        AssetReleaseRequest(released=False, note="taken back")


def test_withdrawal_without_a_note_is_valid():
    request = AssetReleaseRequest(released=False)

    assert request.released is False
    assert request.note is None


def test_component_line_rejects_a_quantity_of_zero():
    """A line with quantity 0 is a deletion, and that has its own endpoint."""
    with pytest.raises(ValidationError):
        ComponentLineCreate(productNumber="ACME-2001", quantity=Decimal("0"))


def test_component_line_rejects_a_negative_quantity():
    with pytest.raises(ValidationError):
        ComponentLineCreate(productNumber="ACME-2001", quantity=Decimal("-1"))


def test_component_line_rejects_an_empty_product_number():
    with pytest.raises(ValidationError):
        ComponentLineCreate(productNumber="", quantity=Decimal("1"))


def test_component_line_allows_a_fractional_quantity():
    """Whether a fraction is admissible depends on the unit of the product, and that is not
    known at the system boundary. The check therefore sits in the repository."""
    line = ComponentLineCreate(productNumber="ACME-2002", quantity=Decimal("0.5"))

    assert line.quantity == Decimal("0.5")


def test_spare_parts_link_demands_at_least_one_serial_number():
    """An empty list would draw no edge and report success all the same."""
    with pytest.raises(ValidationError):
        SparePartsLink(serialNumbers=[])


# ==========================================
# Service forecast
# ==========================================

def test_service_forecast_without_a_completion_is_valid():
    """Before the first report there is no anchor — the forecast then counts from the
    shipping date."""
    forecast = ServiceForecast.model_validate({
        "componentInstanceId": "SN-2026-0001_ACME-2003",
        "accountManager": "Unassigned",
        "customer": "Northwind Systems GmbH",
        "serialNumber": "SN-2026-0001",
        "wearPart": "Filter Cartridge",
        "replacementDueOn": Neo4jDateValue(2027, 3, 18),
    })

    assert forecast.completion is None
    assert forecast.installedOn is None
    assert forecast.replacementDueOn == date(2027, 3, 18)


def test_service_forecast_carries_the_completion_as_a_nested_block():
    """A record with a set completion is no contradiction: it reports the next cycle, not
    the same one again."""
    forecast = ServiceForecast.model_validate({
        "componentInstanceId": "SN-2026-0001_ACME-2003",
        "accountManager": "Dana Weber",
        "customer": "Northwind Systems GmbH",
        "serialNumber": "SN-2026-0001",
        "wearPart": "Filter Cartridge",
        "replacementDueOn": date(2027, 3, 18),
        "completion": {"completedAt": date(2026, 3, 18), "technicianInitials": "MF"},
    })

    assert isinstance(forecast.completion, ServiceCompletion)
    assert forecast.completion.completedAt == date(2026, 3, 18)
    assert forecast.completion.note is None


def test_service_forecast_needs_a_due_date():
    """The due date is the whole point of the answer."""
    with pytest.raises(ValidationError):
        ServiceForecast.model_validate({
            "componentInstanceId": "SN-2026-0001_ACME-2003",
            "accountManager": "Dana Weber",
            "customer": "Northwind Systems GmbH",
            "serialNumber": "SN-2026-0001",
            "wearPart": "Filter Cartridge",
        })


def test_completion_request_knows_no_completion_date():
    """The server sets `completedAt`. A client must not be able to claim it retroactively —
    it doubles as the anchor of the next cycle."""
    with pytest.raises(ValidationError):
        ServiceCompletionRequest.model_validate({"completedAt": date(2020, 1, 1)})


def test_completion_request_is_valid_when_entirely_empty():
    """Neither initials nor a note are mandatory — reporting alone is the point."""
    request = ServiceCompletionRequest()

    assert request.technicianInitials is None
    assert request.note is None


# ==========================================
# Drafts
# ==========================================

def test_asset_draft_without_a_customer_is_valid():
    """The customer edge is resolved through OPTIONAL MATCH — a document without one is
    thin data, not a reason to drop the row."""
    draft = AssetDraft.model_validate({
        "documentNumber": "OC-2026-0002",
        "customer": None,
        "lines": [{"productNumber": "ACME-1001", "quantity": Decimal("1")}],
    })

    assert draft.customer is None
    assert draft.lines[0].label is None
