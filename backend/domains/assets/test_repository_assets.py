"""Unit tests of the assets repository — without a database.

Four things no other test level covers:

* **The status derivation.** `status_expression`, `asset_projection` and
  `status_condition` are pure string builders. The rule they carry — a shipped asset is
  released as well, so the order of the branches decides — is checked here, because with a
  real database it would only show up as a wrong filter result.
* **The construction of the filter query.** `read_many` is intercepted and the generated
  Cypher inspected along with its parameters. What is checked is the parameter binding, not
  the wording — except where the wording carries meaning, and those places are marked.
* **`excess_over_document`.** A pure function on two lists, and the only place deciding
  what a draft confirmation books on top. The summing on both sides is the part that would
  quietly bind stock twice if it were wrong.
* **What gets written on a service completion.** That `completedAt` comes from the server
  and not from the request can be proven on the bound parameters without storing anything.

Not here: whether the MERGE really creates only one component instance, and whether the
release booking rolls back together with the release. Both need a real database.
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from neo4j.time import Date as Neo4jDateValue

from core.exceptions import DatabaseError
from domains.assets.repository_assets import (
    AssetRepository,
    AssetServiceRepository,
    asset_detail_projection,
    asset_list_projection,
    asset_projection,
    excess_over_document,
    status_condition,
    status_expression,
)
from domains.assets.schemas_assets import ComponentLineCreate, ServiceCompletionRequest

REPOSITORY_MODULE = "domains.assets.repository_assets"


def _component(product_number: str, quantity: str) -> ComponentLineCreate:
    """Builds one line of a confirmed bill of materials."""
    return ComponentLineCreate(productNumber=product_number, quantity=Decimal(quantity))


# ==========================================
# Status derivation
# ==========================================

def test_projection_inserts_the_given_variable_everywhere():
    """The placeholder occurs several times in the cascade. One left standing would end up
    in the query as literal text and let Neo4j fail."""
    projection = asset_projection("node")

    assert "{var}" not in projection
    assert "node.shippedOn" in projection
    assert "node.bomReleased" in projection


def test_projection_checks_shipped_before_released():
    """A shipped asset is always released as well. Both conditions then apply, and without
    this ranking the result would depend on the order of evaluation."""
    expression = " ".join(status_expression().split())

    assert expression.index("shippedOn") < expression.index("bomReleased")


def test_projection_catches_a_missing_release_flag():
    """`bomReleased` can be missing on nodes that came about through MERGE. Without the
    default the branch yields null and the asset gets no status at all."""
    assert "coalesce(a.bomReleased, false)" in status_expression()


def test_projection_takes_over_every_node_property():
    """`.*` is what keeps a new property from having to be listed in the projection as
    well."""
    assert ".*" in asset_projection()


def test_projection_has_planned_as_the_default():
    """The ELSE branch has to be a real value — a null there would fail validation on every
    freshly created asset."""
    assert "ELSE 'planned'" in status_expression()


@pytest.mark.parametrize("status", ["planned", "released", "shipped"])
def test_condition_is_parenthesised_and_free_of_placeholders(status):
    """The fragments are appended to further filters with AND. An unparenthesised
    "A AND NOT B" would tangle with a neighbouring OR."""
    condition = status_condition(status)

    assert condition.startswith("(")
    assert condition.endswith(")")
    assert "{var}" not in condition


def test_condition_released_excludes_the_shipped_ones():
    """Without the exclusion ?status=released also returns the assets already shipped — the
    filter would then no longer match the cascade."""
    assert "shippedOn IS NULL" in status_condition("released")


def test_condition_planned_excludes_released_and_shipped():
    condition = status_condition("planned")

    assert "shippedOn IS NULL" in condition
    assert "NOT coalesce(a.bomReleased, false)" in condition


def test_condition_shipped_hangs_on_the_shipping_date_alone():
    """The release plays no role there: the shipping date wins the cascade."""
    condition = status_condition("shipped")

    assert "shippedOn IS NOT NULL" in condition
    assert "bomReleased" not in condition


def test_condition_takes_over_the_variable():
    assert status_condition("shipped", "node") == "(node.shippedOn IS NOT NULL)"


def test_condition_rejects_an_unknown_status():
    """A silent fallback would turn a programming error into an inconspicuously wrong
    result list."""
    with pytest.raises(ValueError):
        status_condition("in_transit")  # type: ignore[arg-type]


def test_list_projection_adds_the_three_edge_fields():
    projection = asset_list_projection()

    assert "productNumber: p.number" in projection
    assert "customerId: c.id" in projection
    assert "documentNumber: d.number" in projection


def test_detail_projection_assembles_the_release_block():
    """The three release properties must not additionally stand flat in the answer — a
    client would otherwise have two sources for the same state."""
    projection = asset_detail_projection()

    assert "release: {" in projection
    assert "released: coalesce(a.bomReleased, false)" in projection
    assert "employee: e.name" in projection


def test_detail_projection_reads_the_components_as_a_comprehension():
    """A pattern comprehension needs no WITH and yields `[]` by itself — unlike an
    OPTIONAL MATCH plus collect(), which has to group by every remaining variable."""
    projection = asset_detail_projection()

    assert "components: [" in projection
    assert "HAS_COMPONENT" in projection
    assert "componentInstance.status = 'active'" in projection


def test_detail_projection_takes_over_the_given_variables():
    projection = asset_detail_projection("node", "prod", "cust", "doc", "emp")

    assert "productNumber: prod.number" in projection
    assert "customer: cust{.id, .name}" in projection
    assert "employee: emp.name" in projection


# ==========================================
# Excess over the document
# ==========================================

def test_excess_ignores_what_already_stood_on_the_document():
    """Those lines were reserved with the order confirmation. Booking for them again would
    bind the same physical stock twice."""
    excess = excess_over_document(
        [_component("ACME-2001", "2")],
        [{"productNumber": "ACME-2001", "quantity": 2.0}],
    )

    assert excess == []


def test_excess_books_an_added_component_in_full():
    """Nobody has booked a component that was not on the document."""
    excess = excess_over_document(
        [_component("ACME-2002", "3")],
        [{"productNumber": "ACME-2001", "quantity": 2.0}],
    )

    assert excess == [("ACME-2002", 3.0)]


def test_excess_books_only_the_difference_on_a_raised_quantity():
    excess = excess_over_document(
        [_component("ACME-2001", "5")],
        [{"productNumber": "ACME-2001", "quantity": 2.0}],
    )

    assert excess == [("ACME-2001", 3.0)]


def test_excess_releases_nothing_on_a_lowered_quantity():
    """A shortfall is not released here — the cancellation of the document is responsible
    for that, and it knows the line it has to give back to."""
    excess = excess_over_document(
        [_component("ACME-2001", "1")],
        [{"productNumber": "ACME-2001", "quantity": 4.0}],
    )

    assert excess == []


def test_excess_sums_the_same_product_across_several_document_lines():
    """The same product may stand on one document more than once."""
    excess = excess_over_document(
        [_component("ACME-2001", "5")],
        [
            {"productNumber": "ACME-2001", "quantity": 2.0},
            {"productNumber": "ACME-2001", "quantity": 2.0},
        ],
    )

    assert excess == [("ACME-2001", 1.0)]


def test_excess_sums_the_same_product_across_several_bom_lines():
    """Without the summing every line competes singly against the full document quantity
    and thereby counts as covered: two lines of 2 against a document quantity of 2 would
    yield no excess at all instead of the actual 2."""
    excess = excess_over_document(
        [_component("ACME-2001", "2"), _component("ACME-2001", "2")],
        [{"productNumber": "ACME-2001", "quantity": 2.0}],
    )

    assert excess == [("ACME-2001", 2.0)]


def test_excess_folds_a_repeated_product_into_one_booking():
    """One booking per product, not one per line — otherwise the movement history would
    show two entries for what is one decision."""
    excess = excess_over_document(
        [_component("ACME-2002", "1"), _component("ACME-2002", "1")], []
    )

    assert excess == [("ACME-2002", 2.0)]


def test_excess_skips_none_out_of_an_empty_collect():
    """A document without lines delivers `[null]` out of the collect, not `[]`."""
    excess = excess_over_document([_component("ACME-2002", "1")], [None])

    assert excess == [("ACME-2002", 1.0)]


def test_excess_survives_a_missing_line_list():
    excess = excess_over_document([_component("ACME-2002", "1")], None)

    assert excess == [("ACME-2002", 1.0)]


# ==========================================
# Conversion
# ==========================================

def test_to_asset_reports_a_missing_status_as_a_database_error():
    """A node the read model cannot map is a data problem of the server: 500, not 422."""
    with pytest.raises(DatabaseError):
        AssetRepository._to_asset({"serialNumber": "SN-2026-0001"})


def test_to_list_item_reports_a_missing_serial_number_as_a_database_error():
    with pytest.raises(DatabaseError):
        AssetRepository._to_list_item({"status": "planned"})


def test_to_asset_accepts_a_thin_node():
    asset = AssetRepository._to_asset({"serialNumber": "SN-2026-0009", "status": "planned"})

    assert asset.serialNumber == "SN-2026-0009"
    assert asset.components == []


# ==========================================
# Construction of the filter query
# ==========================================

@pytest.fixture
def captured_read(monkeypatch):
    """Intercepts the next read_many/read_single query instead of running it."""
    record: dict = {"query": "", "params": {}, "records": [], "record": None}

    async def fake_read_many(session, query, **params):
        record["query"] = " ".join(query.split())
        record["params"] = params
        return record["records"]

    async def fake_read_single(session, query, **params):
        record["query"] = " ".join(query.split())
        record["params"] = params
        return record["record"]

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_many", fake_read_many)
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_single", fake_read_single)
    return record


@pytest.mark.asyncio
async def test_list_without_filters_builds_no_conditions(captured_read):
    await AssetRepository.get_assets(AsyncMock())

    assert captured_read["params"] == {}
    assert "WHERE" not in captured_read["query"]


@pytest.mark.asyncio
async def test_list_uses_the_status_condition(captured_read):
    """The value from the client selects a fragment, it is not written into the query."""
    await AssetRepository.get_assets(AsyncMock(), status="shipped")

    assert "a.shippedOn IS NOT NULL" in captured_read["query"]
    assert "shipped" not in captured_read["params"].values()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["planned", "released", "shipped"])
async def test_every_status_produces_its_own_fragment(captured_read, status):
    await AssetRepository.get_assets(AsyncMock(), status=status)

    assert status_condition(status) in captured_read["query"]


@pytest.mark.asyncio
async def test_the_search_term_is_bound_and_not_inserted(captured_read):
    """A search term is client input. Were it inserted into the query text, a quotation
    mark would be enough to change the query."""
    await AssetRepository.get_assets(AsyncMock(), search="SN-2026")

    assert captured_read["params"]["search"] == "SN-2026"
    assert "SN-2026" not in captured_read["query"]


@pytest.mark.asyncio
async def test_the_customer_is_bound_as_a_parameter(captured_read):
    await AssetRepository.get_assets(AsyncMock(), customerId="C-1001")

    assert captured_read["params"]["customerId"] == "C-1001"
    assert "C-1001" not in captured_read["query"]


@pytest.mark.asyncio
async def test_the_search_covers_the_project_number(captured_read):
    """Whoever searches for an asset usually has the project number of the case at hand,
    not its serial number."""
    await AssetRepository.get_assets(AsyncMock(), search="2026-0001")

    assert "projectNumber" in captured_read["query"]


@pytest.mark.asyncio
async def test_an_empty_search_produces_no_filter(captured_read):
    """An empty string is not a search. Were it bound, CONTAINS '' would match everything
    and the filter would look effective without being it."""
    await AssetRepository.get_assets(AsyncMock(), search="")

    assert "search" not in captured_read["params"]


@pytest.mark.asyncio
async def test_filters_are_joined_with_and(captured_read):
    """Additive, not alternative: more filters mean a narrower result."""
    await AssetRepository.get_assets(AsyncMock(), customerId="C-1001", status="shipped")

    assert " AND " in captured_read["query"]
    assert " OR c.id" not in captured_read["query"]


@pytest.mark.asyncio
async def test_the_list_uses_optional_match_for_every_edge(captured_read):
    """An ordinary MATCH would silently throw an asset missing one of the three edges out
    of the list — without an error and without anyone noticing.

    This test checks a piece of wording, because the wording is the rule here.
    """
    await AssetRepository.get_assets(AsyncMock())

    assert captured_read["query"].count("OPTIONAL MATCH") == 3


@pytest.mark.asyncio
async def test_the_list_delivers_the_calculated_status(captured_read):
    captured_read["records"] = [
        {"asset": {"serialNumber": "SN-2026-0001", "status": "shipped", "customerId": "C-1001"}}
    ]

    assets = await AssetRepository.get_assets(AsyncMock())

    assert assets[0].status == "shipped"
    assert assets[0].customerId == "C-1001"


@pytest.mark.asyncio
async def test_an_unknown_serial_number_delivers_none(captured_read):
    """Translating that into a 404 is the service's business — the query itself knows
    nothing about HTTP."""
    captured_read["record"] = None

    assert await AssetRepository.get_asset("SN-2026-9999", AsyncMock()) is None


@pytest.mark.asyncio
async def test_the_bom_distinguishes_an_unknown_asset_from_an_empty_one(captured_read):
    """`collect()` over zero rows already returns one row holding an empty list. Without
    the `assetExists` flag both cases would be indistinguishable."""
    captured_read["record"] = {"assetExists": False, "components": []}
    assert await AssetRepository.get_bom("SN-2026-9999", AsyncMock()) is None

    captured_read["record"] = {"assetExists": True, "components": [None]}
    assert await AssetRepository.get_bom("SN-2026-0009", AsyncMock()) == []


# ==========================================
# Service completion
# ==========================================

@pytest.fixture
def captured_write(monkeypatch):
    """Intercepts the parameters of the next write_single call."""
    record: dict = {"query": "", "params": {}, "record": None}

    async def fake_write_single(session, query, **params):
        record["query"] = " ".join(query.split())
        record["params"] = params
        return record["record"]

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.write_single", fake_write_single)
    return record


def _stored_completion(**overrides) -> dict:
    data: dict = {
        "completedAt": Neo4jDateValue(2026, 6, 1),
        "technicianInitials": "MF",
        "note": None,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_set_completion_binds_the_component_instance_id(captured_write):
    captured_write["record"] = _stored_completion()

    await AssetServiceRepository.set_completion(
        "SN-2026-0001_ACME-2003", ServiceCompletionRequest(), AsyncMock()
    )

    assert captured_write["params"]["componentInstanceId"] == "SN-2026-0001_ACME-2003"


@pytest.mark.asyncio
async def test_set_completion_binds_initials_and_note(captured_write):
    captured_write["record"] = _stored_completion(note="battery swollen")

    result = await AssetServiceRepository.set_completion(
        "SN-2026-0001_ACME-2003",
        ServiceCompletionRequest(technicianInitials="MF", note="battery swollen"),
        AsyncMock(),
    )

    assert captured_write["params"]["technicianInitials"] == "MF"
    assert captured_write["params"]["note"] == "battery swollen"
    assert result is not None
    assert result.note == "battery swollen"


@pytest.mark.asyncio
async def test_set_completion_does_not_take_the_date_from_the_request(captured_write):
    """`completedAt` doubles as the anchor of the next cycle. A client that could claim it
    retroactively would move the next replacement date along with it.

    This test checks a piece of wording, because the wording is the rule here.
    """
    captured_write["record"] = _stored_completion()

    await AssetServiceRepository.set_completion(
        "SN-2026-0001_ACME-2003", ServiceCompletionRequest(), AsyncMock()
    )

    assert "event.completedAt = date()" in captured_write["query"]
    assert "completedAt" not in captured_write["params"]


@pytest.mark.asyncio
async def test_set_completion_merges_onto_one_node_per_component(captured_write):
    """There is deliberately no history of several completions — only the current anchor
    point. A CREATE would produce a second node on every report."""
    captured_write["record"] = _stored_completion()

    await AssetServiceRepository.set_completion(
        "SN-2026-0001_ACME-2003", ServiceCompletionRequest(), AsyncMock()
    )

    assert "MERGE (event:ServiceEvent {id: $componentInstanceId})" in captured_write["query"]


@pytest.mark.asyncio
async def test_set_completion_on_an_unknown_component_delivers_none(captured_write):
    captured_write["record"] = None

    result = await AssetServiceRepository.set_completion(
        "SN-2026-0001_NOPE", ServiceCompletionRequest(), AsyncMock()
    )

    assert result is None
