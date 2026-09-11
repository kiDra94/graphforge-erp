"""Integration tests of the assets domain against a real Neo4j.

These tests answer one question: **does the Cypher do what the code claims?**
Everything that can be checked without a database — status codes, error translation,
schema validation — lives in the API and unit tests and is not repeated here.

Covered are the areas that cannot be checked with mocks in principle:

* the **status derivation** from `shippedOn` and `bomReleased`, as a projected field and
  as a filter
* **creation with three edges**, including the customer derived through the document and
  the serial number derived from the order
* **duplicate detection through the constraint** rather than a check beforehand
* the **release and its withdrawal**, including the `RELEASED_BY` edge and the
  all-or-nothing reservation of the components
* the **copy of the bill of materials** on creation — flat, leaves only, quantities
  multiplied along the path
* the **draft flow**, which reserves only what engineering added beyond the document
* the **service forecast**, whose interval runs from the shipping date or the last
  completion
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from core.exceptions import BusinessLogicError, DuplicateKeyError, NotFoundError
from domains.assets.repository_assets import AssetRepository, AssetServiceRepository
from domains.assets.schemas_assets import (
    AssetCreate,
    AssetDraftConfirmation,
    AssetReleaseRequest,
    AssetUpdate,
    ComponentLineCreate,
    ServiceCompletionRequest,
    SparePartsLink,
)

# ==========================================
# HELPERS — building the graph
# ==========================================

STANDARD_DOCUMENT = "OC-2026-0001"

# The server assigns the serial number, not the test: the prefix plus the project number of
# the order the order confirmation belongs to.
STANDARD_SN = "SN-2026-0001"

# The asset product and its bill of materials:
#
#   ACME-1000 Starter Kit A
#   ├── ACME-2003 Filter Cartridge   wear part, 48 months
#   ├── ACME-2004 Pump Unit          intermediate assembly
#   │   └── ACME-2002 O-Ring 10x2    wear part, 24 months
#   └── ACME-2008 Mounting Bracket   no wear part
ASSET_PRODUCT = "ACME-1000"
LEAVES = {"ACME-2002", "ACME-2003", "ACME-2008"}


async def create_customer(session, id: str = "C-1001", name: str = "Example Industries GmbH"):
    await session.run("CREATE (:Customer {id: $id, name: $name})", id=id, name=name)


async def create_employee(session, id: str = "2", name: str = "Erika Musterfrau"):
    await session.run("CREATE (:Employee {id: $id, name: $name})", id=id, name=name)


async def create_order(session, projectNumber: str = "2026-0001"):
    """Creates an order — the bracket over every document of one case.

    Its project number becomes the core of every serial number the server forms for this
    case.
    """
    await session.run(
        "CREATE (:Order {projectNumber: $projectNumber})", projectNumber=projectNumber
    )


async def create_document(
    session,
    number: str = STANDARD_DOCUMENT,
    type: str = "OrderConfirmation",
    customerId: str | None = "C-1001",
    documentDate: str | None = "2026-02-10",
    projectNumber: str | None = "2026-0001",
    assetPurpose: str | None = None,
):
    """Creates a document and hangs it optionally off customer and order.

    The optional parameters reproduce the error cases of the creation:

    * `customerId=None` — a document without `BELONGS_TO_CUSTOMER`.
    * `projectNumber=None` — a document without an order. The project number is then
      missing and the server cannot form a serial number.
    * `documentDate=None` — without a date no order date on the asset.
    """
    await session.run(
        """
        CREATE (:Document {
            number: $number,
            type: $type,
            date: CASE WHEN $documentDate IS NULL THEN null ELSE date($documentDate) END,
            assetPurpose: $assetPurpose
        })
        """,
        number=number,
        type=type,
        documentDate=documentDate,
        assetPurpose=assetPurpose,
    )
    if customerId:
        await session.run(
            """
            MATCH (d:Document {number: $number}), (c:Customer {id: $customerId})
            CREATE (d)-[:BELONGS_TO_CUSTOMER]->(c)
            """,
            number=number,
            customerId=customerId,
        )
    if projectNumber:
        await session.run(
            """
            MATCH (d:Document {number: $number}), (o:Order {projectNumber: $projectNumber})
            CREATE (d)-[:BELONGS_TO_ORDER]->(o)
            """,
            number=number,
            projectNumber=projectNumber,
        )


async def second_order(session):
    """A second case with another customer and another project number.

    Yields the serial number `SN-2026-0999` — sharing no part with `STANDARD_SN` beyond the
    prefix and the year, and therefore suitable for search and filter tests.
    """
    await create_customer(session, "C-1002", "Sample Logistics AG")
    await create_order(session, projectNumber="2026-0999")
    await create_document(
        session, number="OC-2026-0999", customerId="C-1002", projectNumber="2026-0999"
    )


async def create_product(
    session,
    number: str,
    label: str = "Test product",
    isWearPart: bool = False,
    serviceIntervalMonths: int | None = None,
):
    await session.run(
        """
        CREATE (:Product {
            number: $number,
            label: $label,
            unit: 'pcs',
            active: true,
            isWearPart: $isWearPart,
            serviceIntervalMonths: $serviceIntervalMonths
        })
        """,
        number=number,
        label=label,
        isWearPart=isWearPart,
        serviceIntervalMonths=serviceIntervalMonths,
    )


async def contains(session, parent: str, child: str, quantity: float = 1):
    await session.run(
        """
        MATCH (p:Product {number: $parent}), (c:Product {number: $child})
        CREATE (p)-[:CONTAINS {quantity: $quantity}]->(c)
        """,
        parent=parent,
        child=child,
        quantity=quantity,
    )


async def create_location(session, id: str = "1", type: str = "Warehouse") -> None:
    await session.run(
        "MERGE (l:Location {id: $id}) SET l.type = $type, l.name = 'Location ' + $id",
        id=id, type=type,
    )


async def create_stock(session, productNumber: str, locationId: str, quantity: float) -> None:
    await session.run(
        """
        MATCH (p:Product {number: $productNumber}), (l:Location {id: $locationId})
        MERGE (s:StockLevel {id: $productNumber + '_' + $locationId})
        SET s.quantity = $quantity, s.reserved = coalesce(s.reserved, 0.0)
        MERGE (p)-[:HAS_STOCK]->(s)
        MERGE (s)-[:AT_LOCATION]->(l)
        """,
        productNumber=productNumber, locationId=locationId, quantity=quantity,
    )


async def read_stock(session, productNumber: str, locationId: str = "1") -> tuple[float, float]:
    result = await session.run(
        "MATCH (s:StockLevel {id: $id}) RETURN s.quantity AS quantity, s.reserved AS reserved",
        id=f"{productNumber}_{locationId}",
    )
    record = await result.single()
    return (record["quantity"], record["reserved"]) if record else (0.0, 0.0)


async def base_environment(session):
    """Creates customer, employee, order, order confirmation and the main warehouse —
    everything an asset needs around it, without any product."""
    await create_customer(session)
    await create_employee(session)
    await create_order(session)
    await create_document(session)
    await create_location(session)


async def standard_environment(session):
    """The base environment plus the asset product with its bill of materials.

    The bill of materials carries two wear parts on two levels and one part that is none —
    enough to check the filtering and the recursion. Every leaf has 100 pieces in the main
    warehouse, because every release reserves against it.
    """
    await base_environment(session)

    await create_product(session, ASSET_PRODUCT, "Starter Kit A")
    await create_product(session, "ACME-2003", "Filter Cartridge", isWearPart=True,
                         serviceIntervalMonths=48)
    await create_product(session, "ACME-2004", "Pump Unit")
    await create_product(session, "ACME-2002", "O-Ring 10x2", isWearPart=True,
                         serviceIntervalMonths=24)
    await create_product(session, "ACME-2008", "Mounting Bracket")

    await contains(session, ASSET_PRODUCT, "ACME-2003")
    await contains(session, ASSET_PRODUCT, "ACME-2004")
    await contains(session, ASSET_PRODUCT, "ACME-2008")
    await contains(session, "ACME-2004", "ACME-2002")

    for productNumber in LEAVES:
        await create_stock(session, productNumber, "1", quantity=100.0)


async def create_asset(
    session,
    documentNumber: str = STANDARD_DOCUMENT,
    internalNumber: str | None = None,
    productNumber: str | None = ASSET_PRODUCT,
):
    """Creates an asset through the regular repository path.

    The serial number is not in the call — it comes about in the server. Whoever needs it
    takes it from the return value.
    """
    return await AssetRepository.create_asset(
        AssetCreate(
            documentNumber=documentNumber,
            productNumber=productNumber,
            internalNumber=internalNumber,
        ),
        session,
    )


async def release(session, serial_number: str = STANDARD_SN, employee_id: str = "2", note=None):
    return await AssetRepository.set_release(
        serial_number, AssetReleaseRequest(released=True, note=note), employee_id, session
    )


async def withdraw(session, serial_number: str = STANDARD_SN):
    return await AssetRepository.set_release(
        serial_number, AssetReleaseRequest(released=False), "2", session
    )


async def ship(session, serial_number: str = STANDARD_SN, on: date = date(2026, 3, 20)):
    return await AssetRepository.update_asset(
        serial_number, AssetUpdate(shippedOn=on), session
    )


# ==========================================
# HELPERS — reading the graph
# ==========================================

async def edges_of(session, serial_number: str) -> dict[str, str]:
    """Reads the outgoing edges of an asset together with the key of their target."""
    result = await session.run(
        """
        MATCH (a:AssetInstance {serialNumber: $sn})-[r]->(target)
        WHERE NOT target:ComponentInstance
        RETURN type(r) AS type,
               coalesce(target.number, target.id, target.serialNumber) AS key
        """,
        sn=serial_number,
    )
    return {record["type"]: record["key"] async for record in result}


async def components_of(session, serial_number: str) -> list[dict]:
    """Reads the digital twin of an asset directly from the graph."""
    result = await session.run(
        """
        MATCH (:AssetInstance {serialNumber: $sn})-[rel:HAS_COMPONENT]->(ci:ComponentInstance)
              -[:IS_TYPE]->(p:Product)
        RETURN ci.id AS id, ci.status AS status, ci.installedOn AS installedOn,
               p.number AS productNumber, rel.quantity AS quantity
        ORDER BY p.number
        """,
        sn=serial_number,
    )
    return [dict(record) async for record in result]


async def component_numbers(session, serial_number: str = STANDARD_SN) -> set[str]:
    return {c["productNumber"] for c in await components_of(session, serial_number)}


async def properties_of(session, serial_number: str) -> dict:
    result = await session.run(
        "MATCH (a:AssetInstance {serialNumber: $sn}) RETURN properties(a) AS p",
        sn=serial_number,
    )
    record = await result.single()
    return dict(record["p"]) if record else {}


async def server_date(session) -> date:
    """Today's date from the point of view of the DATABASE SERVER.

    Not `date.today()` where a date set server-side is checked: `releasedOn = date()` and
    `completedAt = date()` are evaluated by Neo4j with the clock of its container. Running on
    UTC while the development machine runs on local time, the two differ by a day between
    midnight and the UTC day change — the test would be red for those hours without
    anything in the code having changed.
    """
    result = await session.run("RETURN date() AS today")
    record = await result.single()
    return record["today"].to_native()


# ==========================================
# CREATING: edges, derived customer, start state
# ==========================================

@pytest.mark.asyncio
async def test_create_draws_all_three_edges(neo4j_session):
    await standard_environment(neo4j_session)

    await create_asset(neo4j_session)

    edges = await edges_of(neo4j_session, STANDARD_SN)
    assert edges["BASED_ON"] == ASSET_PRODUCT
    assert edges["BASED_ON_DOCUMENT"] == STANDARD_DOCUMENT
    assert edges["SOLD_TO"] == "C-1001"


@pytest.mark.asyncio
async def test_create_derives_the_customer_from_the_document(neo4j_session):
    # The request knows no customer id — the customer comes through BELONGS_TO_CUSTOMER.
    await standard_environment(neo4j_session)

    await create_asset(neo4j_session)
    asset = await AssetRepository.get_asset(STANDARD_SN, neo4j_session)

    assert asset is not None
    assert asset.customer is not None
    assert asset.customer.id == "C-1001"


@pytest.mark.asyncio
async def test_create_starts_as_planned(neo4j_session):
    await standard_environment(neo4j_session)

    created = await create_asset(neo4j_session)

    assert created.status == "planned"
    assert created.serialNumber == STANDARD_SN


@pytest.mark.asyncio
async def test_create_copies_the_bill_of_materials_right_away(neo4j_session):
    # The bill of materials comes about with the asset, not only on shipping — engineering
    # is meant to check and change it before the build. Details (exact leaves, keys, wear
    # part filter) are in the BILL-OF-MATERIALS COPY section further down.
    await standard_environment(neo4j_session)

    await create_asset(neo4j_session)

    assert await components_of(neo4j_session, STANDARD_SN) != []


@pytest.mark.asyncio
async def test_create_stores_no_status_property(neo4j_session):
    # `status` is calculated. A stored property could go stale.
    await standard_environment(neo4j_session)

    await create_asset(neo4j_session)

    assert "status" not in await properties_of(neo4j_session, STANDARD_SN)


@pytest.mark.asyncio
async def test_an_asset_without_a_catalogue_product_draws_no_based_on_edge(neo4j_session):
    # Most assets come about without a catalogue product. BASED_ON then does not come about
    # at all — no placeholder, no edge into the void — and there is no bill of materials to
    # copy.
    await standard_environment(neo4j_session)

    await create_asset(neo4j_session, productNumber=None)

    edges = await edges_of(neo4j_session, STANDARD_SN)
    assert "BASED_ON" not in edges
    assert edges["SOLD_TO"] == "C-1001"
    assert await components_of(neo4j_session, STANDARD_SN) == []


# ==========================================
# CREATING: serial number and error cases
# ==========================================

@pytest.mark.asyncio
async def test_the_serial_number_comes_from_the_project_number_of_the_order(neo4j_session):
    # The client sends no number. The server forms it from the project number of the order
    # the order confirmation hangs off.
    await standard_environment(neo4j_session)

    created = await create_asset(neo4j_session)

    assert created.serialNumber == "SN-2026-0001"


@pytest.mark.asyncio
async def test_the_order_date_is_taken_from_the_document_date(neo4j_session):
    await standard_environment(neo4j_session)

    created = await create_asset(neo4j_session)

    props = await properties_of(neo4j_session, created.serialNumber)
    assert props["orderedOn"].to_native() == date(2026, 2, 10)
    assert props["projectNumber"] == "2026-0001"


@pytest.mark.asyncio
async def test_a_second_asset_on_the_same_order_gets_a_running_number(neo4j_session):
    # The first one stays without a suffix, so a key already assigned never has to be
    # renamed afterwards.
    await standard_environment(neo4j_session)

    first = await create_asset(neo4j_session)
    second = await create_asset(neo4j_session)
    third = await create_asset(neo4j_session)

    assert first.serialNumber == "SN-2026-0001"
    assert second.serialNumber == "SN-2026-0001-2"
    assert third.serialNumber == "SN-2026-0001-3"


@pytest.mark.asyncio
async def test_counting_runs_over_the_order_not_over_the_document(neo4j_session):
    # Several order confirmations can belong to one order. The running number still has to
    # be unique across the whole case.
    await standard_environment(neo4j_session)
    await create_document(neo4j_session, "OC-2026-0001-B")
    await create_asset(neo4j_session)

    second = await create_asset(neo4j_session, "OC-2026-0001-B")

    assert second.serialNumber == "SN-2026-0001-2"


@pytest.mark.asyncio
async def test_a_document_without_an_order_is_rejected(neo4j_session):
    # Without a project number no serial number can be formed. An invented one would be
    # worse than an error message.
    await standard_environment(neo4j_session)
    await create_document(neo4j_session, "OC-WITHOUT-ORDER", projectNumber=None)

    with pytest.raises(BusinessLogicError) as error:
        await create_asset(neo4j_session, "OC-WITHOUT-ORDER")

    assert "order" in str(error.value)


@pytest.mark.asyncio
async def test_a_document_without_a_date_is_rejected(neo4j_session):
    await standard_environment(neo4j_session)
    await create_document(neo4j_session, "OC-WITHOUT-DATE", documentDate=None)

    with pytest.raises(BusinessLogicError) as error:
        await create_asset(neo4j_session, "OC-WITHOUT-DATE")

    assert "date" in str(error.value)


@pytest.mark.asyncio
async def test_a_colliding_serial_number_is_rejected(neo4j_session):
    # Recognised through the constraint, not through a check beforehand. The pre-existing
    # node hangs off no document and is therefore not counted — the server forms the same
    # number a second time.
    await standard_environment(neo4j_session)
    await neo4j_session.run("CREATE (:AssetInstance {serialNumber: $sn})", sn=STANDARD_SN)

    with pytest.raises(DuplicateKeyError) as error:
        await create_asset(neo4j_session)

    assert STANDARD_DOCUMENT in str(error.value)


@pytest.mark.asyncio
async def test_a_duplicate_internal_number_is_rejected(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session, internalNumber="KIT-A-0001")

    with pytest.raises(DuplicateKeyError):
        await create_asset(neo4j_session, internalNumber="KIT-A-0001")


@pytest.mark.asyncio
async def test_a_duplicate_leaves_no_half_node_behind(neo4j_session):
    # The transaction has to be rolled back completely — otherwise a node without edges
    # would stay behind and turn up later as a broken asset.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session, internalNumber="KIT-A-0001")

    with pytest.raises(DuplicateKeyError):
        await create_asset(neo4j_session, internalNumber="KIT-A-0001")

    result = await neo4j_session.run("MATCH (a:AssetInstance) RETURN count(a) AS count")
    assert (await result.single())["count"] == 1


@pytest.mark.asyncio
async def test_a_quote_as_the_basis_is_rejected(neo4j_session):
    await standard_environment(neo4j_session)
    await create_document(neo4j_session, "QU-2026-0001", type="Quote")

    with pytest.raises(BusinessLogicError) as error:
        await create_asset(neo4j_session, "QU-2026-0001")

    # The message names the type actually found — which is why the check runs against the
    # property and not against a label.
    assert "Quote" in str(error.value)


@pytest.mark.asyncio
async def test_a_document_without_a_customer_is_rejected(neo4j_session):
    await standard_environment(neo4j_session)
    await create_document(neo4j_session, "OC-WITHOUT-CUSTOMER", customerId=None)

    with pytest.raises(BusinessLogicError):
        await create_asset(neo4j_session, "OC-WITHOUT-CUSTOMER")


@pytest.mark.asyncio
async def test_an_unknown_document_is_not_found(neo4j_session):
    await standard_environment(neo4j_session)

    with pytest.raises(NotFoundError):
        await create_asset(neo4j_session, "OC-DOES-NOT-EXIST")


@pytest.mark.asyncio
async def test_an_unknown_product_is_not_found(neo4j_session):
    await standard_environment(neo4j_session)

    with pytest.raises(NotFoundError):
        await create_asset(neo4j_session, productNumber="DOES-NOT-EXIST")


@pytest.mark.asyncio
async def test_a_rejected_creation_leaves_nothing_behind(neo4j_session):
    await standard_environment(neo4j_session)

    with pytest.raises(NotFoundError):
        await create_asset(neo4j_session, "OC-DOES-NOT-EXIST")

    result = await neo4j_session.run("MATCH (a:AssetInstance) RETURN count(a) AS count")
    assert (await result.single())["count"] == 0


# ==========================================
# DETAIL: nested blocks from one query
# ==========================================

@pytest.mark.asyncio
async def test_the_detail_returns_customer_and_document(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session, internalNumber="KIT-A-0001")

    asset = await AssetRepository.get_asset(STANDARD_SN, neo4j_session)

    assert asset is not None
    assert asset.productNumber == ASSET_PRODUCT
    assert asset.documentNumber == STANDARD_DOCUMENT
    assert asset.customer is not None
    assert asset.customer.name == "Example Industries GmbH"
    assert asset.internalNumber == "KIT-A-0001"


@pytest.mark.asyncio
async def test_the_detail_without_a_release_returns_an_empty_release_block(neo4j_session):
    # The bill of materials itself exists already — only the release block is empty before
    # the release.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    asset = await AssetRepository.get_asset(STANDARD_SN, neo4j_session)

    assert asset is not None
    assert asset.release.released is False
    assert asset.release.releasedOn is None
    assert asset.release.employee is None


@pytest.mark.asyncio
async def test_the_detail_of_an_unknown_serial_number_is_none(neo4j_session):
    # No error in the repository — the translation into a 404 is the service's business.
    await standard_environment(neo4j_session)

    assert await AssetRepository.get_asset("SN-9999-9999", neo4j_session) is None


# ==========================================
# LIST: status derivation and filters
# ==========================================

@pytest.mark.asyncio
async def test_the_status_falls_back_to_planned_without_a_release(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    asset = await AssetRepository.get_asset(STANDARD_SN, neo4j_session)

    assert asset is not None
    assert asset.status == "planned"


@pytest.mark.asyncio
async def test_the_status_becomes_released_with_the_release(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    await release(neo4j_session)
    asset = await AssetRepository.get_asset(STANDARD_SN, neo4j_session)

    assert asset is not None
    assert asset.status == "released"


@pytest.mark.asyncio
async def test_the_shipping_date_beats_the_release(neo4j_session):
    # The order of the CASE branches is required by the business rules: a shipped asset is
    # always released as well, both conditions apply.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session)

    await ship(neo4j_session)
    asset = await AssetRepository.get_asset(STANDARD_SN, neo4j_session)

    assert asset is not None
    assert asset.status == "shipped"


@pytest.mark.asyncio
async def test_the_status_filter_is_disjoint(neo4j_session):
    # Three assets, three states: every filter must return exactly one. Drop the exclusion
    # of the higher-ranked cases and `released` returns two.
    await standard_environment(neo4j_session)
    first = await create_asset(neo4j_session)
    second = await create_asset(neo4j_session)
    third = await create_asset(neo4j_session)

    for to_release in (second, third):
        await release(neo4j_session, to_release.serialNumber)
    await ship(neo4j_session, third.serialNumber)

    async def serial_numbers(status):
        hits = await AssetRepository.get_assets(neo4j_session, status=status)
        return {entry.serialNumber for entry in hits}

    assert await serial_numbers("planned") == {first.serialNumber}
    assert await serial_numbers("released") == {second.serialNumber}
    assert await serial_numbers("shipped") == {third.serialNumber}


@pytest.mark.asyncio
async def test_the_list_without_a_filter_returns_everything(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await create_asset(neo4j_session)

    hits = await AssetRepository.get_assets(neo4j_session)

    assert len(hits) == 2


@pytest.mark.asyncio
async def test_the_filter_by_customer(neo4j_session):
    await standard_environment(neo4j_session)
    await second_order(neo4j_session)
    await create_asset(neo4j_session)
    at_second_customer = await create_asset(neo4j_session, "OC-2026-0999")

    hits = await AssetRepository.get_assets(neo4j_session, customerId="C-1002")

    assert [entry.serialNumber for entry in hits] == [at_second_customer.serialNumber]


@pytest.mark.asyncio
async def test_an_empty_result_is_no_error(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    assert await AssetRepository.get_assets(neo4j_session, customerId="C-9999") == []


@pytest.mark.asyncio
async def test_the_search_finds_through_the_serial_number(neo4j_session):
    await standard_environment(neo4j_session)
    await second_order(neo4j_session)
    await create_asset(neo4j_session)
    await create_asset(neo4j_session, "OC-2026-0999")

    hits = await AssetRepository.get_assets(neo4j_session, search="0999")

    assert [entry.serialNumber for entry in hits] == ["SN-2026-0999"]


@pytest.mark.asyncio
async def test_the_search_finds_through_the_internal_number(neo4j_session):
    await standard_environment(neo4j_session)
    await second_order(neo4j_session)
    await create_asset(neo4j_session, internalNumber="KIT-A-0001")
    await create_asset(neo4j_session, "OC-2026-0999", internalNumber="PUMP-B-0001")

    hits = await AssetRepository.get_assets(neo4j_session, search="kit-a")

    assert [entry.serialNumber for entry in hits] == [STANDARD_SN]


@pytest.mark.asyncio
async def test_the_search_finds_a_legacy_asset_through_its_project_number(neo4j_session):
    # An imported asset may carry a serial number of an older scheme that does not contain
    # the project number. The third search field is what still finds it.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await neo4j_session.run(
        "CREATE (:AssetInstance {serialNumber: 'LEGACY-17', projectNumber: '2019-0042'})"
    )

    hits = await AssetRepository.get_assets(neo4j_session, search="2019-0042")

    assert [entry.serialNumber for entry in hits] == ["LEGACY-17"]


@pytest.mark.asyncio
async def test_the_search_works_together_with_the_status(neo4j_session):
    # The OR group of the search has to be parenthesised. AND binds tighter than OR in
    # Cypher — without the parentheses the condition would read as "serial number matches
    # OR (internal number matches AND status fits)", and an asset whose serial number
    # matches would get past the status filter. Both assets here match the search, only one
    # the status.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    second = await create_asset(neo4j_session)
    await release(neo4j_session, second.serialNumber)

    hits = await AssetRepository.get_assets(
        neo4j_session, search="2026-0001", status="released"
    )

    assert [entry.serialNumber for entry in hits] == [second.serialNumber]


@pytest.mark.asyncio
async def test_an_empty_search_filters_nothing(neo4j_session):
    # "?search=" arrives as an empty string. A CONTAINS "" would match every asset — the
    # filter would be without effect, but the query needlessly more expensive.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await create_asset(neo4j_session)

    hits = await AssetRepository.get_assets(neo4j_session, search="")

    assert len(hits) == 2


@pytest.mark.asyncio
async def test_filters_add_up(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    second = await create_asset(neo4j_session)
    await release(neo4j_session, second.serialNumber)

    hits = await AssetRepository.get_assets(
        neo4j_session, customerId="C-1001", status="released"
    )

    assert [entry.serialNumber for entry in hits] == [second.serialNumber]


# ==========================================
# RELEASE AND WITHDRAWAL
# ==========================================

@pytest.mark.asyncio
async def test_the_release_sets_fields_and_edge(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    response = await release(neo4j_session, note="Checked, all dimensions verified")

    assert response is not None
    assert response.status == "released"
    assert response.release.released is True
    # Compared against the clock that wrote the value — see `server_date`.
    assert response.release.releasedOn == await server_date(neo4j_session)
    assert response.release.employee == "Erika Musterfrau"
    assert response.release.note == "Checked, all dimensions verified"

    edges = await edges_of(neo4j_session, STANDARD_SN)
    assert edges["RELEASED_BY"] == "2"


@pytest.mark.asyncio
async def test_a_release_without_a_note_stores_null(neo4j_session):
    # "nothing noted" stays distinguishable from "deliberately left empty".
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    response = await release(neo4j_session)

    assert response is not None
    assert response.release.note is None


@pytest.mark.asyncio
async def test_the_withdrawal_resets_everything(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session, note="OK")

    response = await withdraw(neo4j_session)

    assert response is not None
    assert response.status == "planned"
    assert response.release.released is False
    assert response.release.releasedOn is None
    assert response.release.note is None
    assert "RELEASED_BY" not in await edges_of(neo4j_session, STANDARD_SN)


@pytest.mark.asyncio
async def test_the_withdrawal_really_removes_the_properties(neo4j_session):
    # A `SET ... = null` removes the property in Neo4j. Were it to stay with an empty value,
    # the asset would still look released in raw queries.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session, note="OK")

    await withdraw(neo4j_session)

    props = await properties_of(neo4j_session, STANDARD_SN)
    assert props.get("bomReleased") is False
    assert "releasedOn" not in props
    assert "releaseNote" not in props


@pytest.mark.asyncio
async def test_a_withdrawal_after_shipping_is_rejected(neo4j_session):
    # Otherwise the asset would stay at `shipped` — the shipping date wins the cascade —
    # while the release fields are empty.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session)
    await ship(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await withdraw(neo4j_session)


@pytest.mark.asyncio
async def test_a_rejected_withdrawal_changes_nothing(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session)
    await ship(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await withdraw(neo4j_session)

    props = await properties_of(neo4j_session, STANDARD_SN)
    assert props["bomReleased"] is True
    assert "RELEASED_BY" in await edges_of(neo4j_session, STANDARD_SN)


@pytest.mark.asyncio
async def test_a_release_of_an_unknown_asset_returns_none(neo4j_session):
    await standard_environment(neo4j_session)

    assert await release(neo4j_session, "SN-9999-9999") is None


@pytest.mark.asyncio
async def test_a_release_by_an_unknown_employee_is_not_found(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    with pytest.raises(NotFoundError):
        await release(neo4j_session, employee_id="99")


@pytest.mark.asyncio
async def test_a_second_release_replaces_the_first_edge(neo4j_session):
    await standard_environment(neo4j_session)
    await create_employee(neo4j_session, "6", "Alex Admin")
    await create_asset(neo4j_session)
    await release(neo4j_session, employee_id="2")

    await release(neo4j_session, employee_id="6")

    result = await neo4j_session.run(
        """
        MATCH (:AssetInstance {serialNumber: $sn})-[r:RELEASED_BY]->(e)
        RETURN count(r) AS count, collect(e.name) AS names
        """,
        sn=STANDARD_SN,
    )
    record = await result.single()
    assert record["count"] == 1
    assert record["names"] == ["Alex Admin"]


# ==========================================
# THE RELEASE RESERVES THE COMPONENTS
# ==========================================
# `standard_environment` gives every leaf 100 pieces in the main warehouse; every CONTAINS
# edge of the standard bill of materials carries quantity 1.

@pytest.mark.asyncio
async def test_the_release_reserves_every_component(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    await release(neo4j_session)

    for productNumber in LEAVES:
        assert await read_stock(neo4j_session, productNumber) == (100.0, 1.0)


@pytest.mark.asyncio
async def test_a_release_without_sufficient_stock_is_rejected(neo4j_session):
    # If the stock does not suffice for even one component, neither the release nor any
    # booking comes about.
    await standard_environment(neo4j_session)
    await create_stock(neo4j_session, "ACME-2002", "1", quantity=0.0)
    await create_asset(neo4j_session)

    with pytest.raises(BusinessLogicError, match="ACME-2002"):
        await release(neo4j_session)

    asset = await AssetRepository.get_asset(STANDARD_SN, neo4j_session)
    assert asset is not None
    assert asset.release.released is False
    # The components with sufficient stock must not be reserved either — all or none.
    assert await read_stock(neo4j_session, "ACME-2003") == (100.0, 0.0)
    assert await read_stock(neo4j_session, "ACME-2008") == (100.0, 0.0)


@pytest.mark.asyncio
async def test_the_release_checks_against_the_available_not_the_physical_stock(neo4j_session):
    # 100 pieces on the shelf, all of them reserved for someone else: nothing is available,
    # and a release that bound them a second time would promise the same goods twice.
    await standard_environment(neo4j_session)
    await neo4j_session.run("MATCH (s:StockLevel {id: 'ACME-2008_1'}) SET s.reserved = 100.0")
    await create_asset(neo4j_session)

    with pytest.raises(BusinessLogicError, match="ACME-2008"):
        await release(neo4j_session)


@pytest.mark.asyncio
async def test_releasing_again_does_not_reserve_twice(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session)

    await release(neo4j_session)

    assert await read_stock(neo4j_session, "ACME-2003") == (100.0, 1.0)


@pytest.mark.asyncio
async def test_the_withdrawal_releases_the_reservation(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session)

    await withdraw(neo4j_session)

    for productNumber in LEAVES:
        assert await read_stock(neo4j_session, productNumber) == (100.0, 0.0)


@pytest.mark.asyncio
async def test_withdrawing_again_does_not_release_twice(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session)
    await withdraw(neo4j_session)

    # Already withdrawn — a second withdrawal must not push reserved below 0.
    await withdraw(neo4j_session)

    assert await read_stock(neo4j_session, "ACME-2003") == (100.0, 0.0)


# ==========================================
# BILL-OF-MATERIALS COPY: comes about with the asset
# ==========================================
# The copy comes about on creation (`_create_asset_in_tx` -> `_copy_bom`) and holds the
# complete, flat bill of materials — release and delivery note have to act on the whole
# list, not only on the wear parts.

@pytest.mark.asyncio
async def test_the_copy_holds_every_leaf_not_only_wear_parts(neo4j_session):
    # ACME-2008 sits in the bill of materials and is NO wear part — it belongs to the copy
    # anyway.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    assert await component_numbers(neo4j_session) == LEAVES


@pytest.mark.asyncio
async def test_the_copy_resolves_several_levels_flat(neo4j_session):
    # ACME-2002 does not hang directly off the asset product but under the intermediate
    # assembly ACME-2004 — which itself does not appear in the copy (leaves only).
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    installed = await component_numbers(neo4j_session)
    assert "ACME-2002" in installed
    assert "ACME-2004" not in installed


@pytest.mark.asyncio
async def test_the_copy_multiplies_quantities_along_the_path_and_sums_over_paths(neo4j_session):
    # 3 pumps with 2 O-rings each, plus 1 O-ring directly on the kit: 3 * 2 + 1 = 7. The same
    # leaf reached over two paths becomes one component, not two.
    await base_environment(neo4j_session)
    await create_product(neo4j_session, "KIT", "Kit")
    await create_product(neo4j_session, "PUMP", "Pump")
    await create_product(neo4j_session, "ORING", "O-Ring")
    await contains(neo4j_session, "KIT", "PUMP", quantity=3)
    await contains(neo4j_session, "PUMP", "ORING", quantity=2)
    await contains(neo4j_session, "KIT", "ORING", quantity=1)

    await create_asset(neo4j_session, productNumber="KIT")

    components = await components_of(neo4j_session, STANDARD_SN)
    assert [(c["productNumber"], c["quantity"]) for c in components] == [("ORING", 7.0)]


@pytest.mark.asyncio
async def test_the_copy_carries_the_composite_key(neo4j_session):
    # Only with this key does MERGE stay idempotent.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    ids = {c["id"] for c in await components_of(neo4j_session, STANDARD_SN)}
    assert ids == {f"{STANDARD_SN}_{number}" for number in LEAVES}


@pytest.mark.asyncio
async def test_components_start_active(neo4j_session):
    # The service forecast and the as-built query filter on this value.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    assert all(c["status"] == "active" for c in await components_of(neo4j_session, STANDARD_SN))


@pytest.mark.asyncio
async def test_a_product_without_a_bill_of_materials_becomes_its_own_component(neo4j_session):
    # A simple asset without CONTAINS edges is its own leaf — otherwise a release or a
    # delivery note would have nothing to reserve or issue.
    await standard_environment(neo4j_session)
    await create_product(neo4j_session, "ACME-3000", "Simple unit")

    await create_asset(neo4j_session, productNumber="ACME-3000")

    assert [c["productNumber"] for c in await components_of(neo4j_session, STANDARD_SN)] == ["ACME-3000"]


# ==========================================
# SHIPPING AND INSTALLATION
# ==========================================

@pytest.mark.asyncio
async def test_shipping_does_not_copy_the_bill_of_materials_again(neo4j_session):
    # The copy exists since the creation. Shipping must neither create a second copy nor
    # touch a quantity engineering has already changed.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    before = await components_of(neo4j_session, STANDARD_SN)

    response = await ship(neo4j_session)

    assert response is not None
    assert response.createdComponents == 0
    assert response.status == "shipped"
    assert await components_of(neo4j_session, STANDARD_SN) == before


@pytest.mark.asyncio
async def test_a_legacy_asset_without_a_bill_of_materials_gets_it_on_shipping(neo4j_session):
    # The fallback: an asset that came about through an older route without a copy (built
    # directly in the graph here) gets it on shipping.
    await standard_environment(neo4j_session)
    await neo4j_session.run(
        """
        MATCH (document:Document {number: $documentNumber})
        MATCH (product:Product {number: $productNumber})
        MATCH (customer:Customer {id: 'C-1001'})
        CREATE (a:AssetInstance {serialNumber: $sn, bomReleased: false, createdAt: datetime()})
        CREATE (a)-[:BASED_ON]->(product)
        CREATE (a)-[:BASED_ON_DOCUMENT]->(document)
        CREATE (a)-[:SOLD_TO]->(customer)
        """,
        documentNumber=STANDARD_DOCUMENT, productNumber=ASSET_PRODUCT, sn=STANDARD_SN,
    )
    assert await components_of(neo4j_session, STANDARD_SN) == []

    response = await ship(neo4j_session)

    assert response is not None
    assert response.createdComponents == 3
    assert await component_numbers(neo4j_session) == LEAVES


@pytest.mark.asyncio
async def test_an_installation_date_alone_creates_no_twin(neo4j_session):
    # The trigger of the fallback is the shipping date alone.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    response = await AssetRepository.update_asset(
        STANDARD_SN, AssetUpdate(installedOn=date(2026, 3, 28)), neo4j_session
    )

    assert response is not None
    assert response.createdComponents == 0
    assert response.status == "planned"


@pytest.mark.asyncio
async def test_the_installation_date_is_carried_over_to_the_components(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await ship(neo4j_session)

    await AssetRepository.update_asset(
        STANDARD_SN, AssetUpdate(installedOn=date(2026, 3, 28)), neo4j_session
    )

    components = await components_of(neo4j_session, STANDARD_SN)
    assert all(c["installedOn"].to_native() == date(2026, 3, 28) for c in components)


@pytest.mark.asyncio
async def test_the_detail_returns_the_as_built_state(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await ship(neo4j_session)

    asset = await AssetRepository.get_asset(STANDARD_SN, neo4j_session)

    assert asset is not None
    assert {c.productNumber for c in asset.components} == LEAVES
    assert "O-Ring 10x2" in {c.label for c in asset.components}


@pytest.mark.asyncio
async def test_taking_back_the_shipping_date_is_rejected(neo4j_session):
    # The twin could not be withdrawn in any sensible way.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await ship(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await AssetRepository.update_asset(
            STANDARD_SN, AssetUpdate(shippedOn=None), neo4j_session
        )


@pytest.mark.asyncio
async def test_an_empty_update_is_rejected(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await AssetRepository.update_asset(STANDARD_SN, AssetUpdate(), neo4j_session)


@pytest.mark.asyncio
async def test_an_update_of_an_unknown_serial_number_returns_none(neo4j_session):
    await standard_environment(neo4j_session)

    assert await ship(neo4j_session, "SN-9999-9999") is None


# ==========================================
# ENGINEERING EDITS THE BILL OF MATERIALS OF AN ASSET
# ==========================================

@pytest.mark.asyncio
async def test_get_bom_returns_the_copied_list(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    components = await AssetRepository.get_bom(STANDARD_SN, neo4j_session)

    assert components is not None
    assert {c.productNumber for c in components} == LEAVES


@pytest.mark.asyncio
async def test_get_bom_tells_an_unknown_asset_from_an_empty_list(neo4j_session):
    # Over zero rows collect() still returns one row with an empty list. Without the
    # OPTIONAL MATCH on the asset itself, an unknown serial number would look exactly like an
    # asset without components.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session, productNumber=None)

    assert await AssetRepository.get_bom("SN-9999-9999", neo4j_session) is None
    assert await AssetRepository.get_bom(STANDARD_SN, neo4j_session) == []


@pytest.mark.asyncio
async def test_set_component_adds_a_new_line(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await create_product(neo4j_session, "ACME-2005", "Hose 2m")

    result = await AssetRepository.set_component(
        STANDARD_SN, ComponentLineCreate(productNumber="ACME-2005", quantity=Decimal("2")),
        neo4j_session,
    )

    assert result is not None
    assert result["was_updated"] is False
    assert result["quantity"] == 2.0
    components = await AssetRepository.get_bom(STANDARD_SN, neo4j_session)
    assert components is not None
    assert "ACME-2005" in {c.productNumber for c in components}


@pytest.mark.asyncio
async def test_set_component_changes_the_quantity_of_an_existing_line(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    result = await AssetRepository.set_component(
        STANDARD_SN, ComponentLineCreate(productNumber="ACME-2003", quantity=Decimal("5")),
        neo4j_session,
    )

    assert result is not None
    assert result["was_updated"] is True
    components = await AssetRepository.get_bom(STANDARD_SN, neo4j_session)
    assert components is not None
    changed = next(c for c in components if c.productNumber == "ACME-2003")
    assert changed.quantity == Decimal("5")
    # The other components stay untouched.
    assert len(components) == 3


@pytest.mark.asyncio
async def test_set_component_with_an_unknown_product_is_not_found(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    with pytest.raises(NotFoundError):
        await AssetRepository.set_component(
            STANDARD_SN, ComponentLineCreate(productNumber="does-not-exist", quantity=Decimal("1")),
            neo4j_session,
        )


@pytest.mark.asyncio
async def test_set_component_is_locked_after_the_release(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await AssetRepository.set_component(
            STANDARD_SN, ComponentLineCreate(productNumber="ACME-2003", quantity=Decimal("9")),
            neo4j_session,
        )

    # The quantity before the rejected attempt stays unchanged.
    components = await AssetRepository.get_bom(STANDARD_SN, neo4j_session)
    assert components is not None
    unchanged = next(c for c in components if c.productNumber == "ACME-2003")
    assert unchanged.quantity == Decimal("1")


@pytest.mark.asyncio
async def test_delete_component_strikes_a_line(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    result = await AssetRepository.delete_component(STANDARD_SN, "ACME-2008", neo4j_session)

    assert result is not None
    assert result.remainingLines == 2
    components = await AssetRepository.get_bom(STANDARD_SN, neo4j_session)
    assert components is not None
    assert "ACME-2008" not in {c.productNumber for c in components}


@pytest.mark.asyncio
async def test_delete_component_of_an_unknown_component_is_not_found(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    with pytest.raises(NotFoundError):
        await AssetRepository.delete_component(STANDARD_SN, "does-not-exist", neo4j_session)


@pytest.mark.asyncio
async def test_delete_component_is_locked_after_the_release(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await release(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await AssetRepository.delete_component(STANDARD_SN, "ACME-2008", neo4j_session)

    components = await AssetRepository.get_bom(STANDARD_SN, neo4j_session)
    assert components is not None
    assert "ACME-2008" in {c.productNumber for c in components}


# ==========================================
# ASSET DRAFTS AND SPARE PARTS
# ==========================================
# An order confirmation marked `assetPurpose: 'newAsset'` becomes a draft engineering
# confirms with the bill of materials actually built. What stood on the document was
# already reserved with the order confirmation; only what engineering adds beyond it is
# booked here.

async def draft_environment(session):
    """The standard environment plus an order confirmation marked as a new asset, with
    one line: 1 x ACME-2003."""
    await standard_environment(session)
    await create_document(session, "OC-2026-0002", assetPurpose="newAsset")
    await session.run(
        """
        MATCH (d:Document {number: 'OC-2026-0002'}), (p:Product {number: 'ACME-2003'})
        CREATE (l:DocumentLine {id: 'OC-2026-0002_1', lineNumber: 1, quantity: 1.0})
        CREATE (d)-[:HAS_LINE]->(l)
        CREATE (l)-[:OF_PRODUCT]->(p)
        """
    )


def confirmation(*components: tuple[str, str], note: str | None = None) -> AssetDraftConfirmation:
    return AssetDraftConfirmation(
        documentNumber="OC-2026-0002",
        components=[
            ComponentLineCreate(productNumber=number, quantity=Decimal(quantity))
            for number, quantity in components
        ],
        note=note,
    )


@pytest.mark.asyncio
async def test_an_open_draft_is_listed_with_its_lines(neo4j_session):
    await draft_environment(neo4j_session)

    drafts = await AssetRepository.get_asset_drafts(neo4j_session)

    assert [d.documentNumber for d in drafts] == ["OC-2026-0002"]
    assert [line.productNumber for line in drafts[0].lines] == ["ACME-2003"]
    assert drafts[0].customer is not None
    assert drafts[0].customer.id == "C-1001"


@pytest.mark.asyncio
async def test_confirming_a_draft_creates_a_released_asset_with_the_given_bom(neo4j_session):
    await draft_environment(neo4j_session)

    created = await AssetRepository.confirm_draft(
        confirmation(("ACME-2003", "1"), ("ACME-2002", "2"), note="Built as agreed"),
        "2", neo4j_session,
    )

    assert [c.status for c in created] == ["released"]
    serial_number = created[0].serialNumber
    components = await components_of(neo4j_session, serial_number)
    assert [(c["productNumber"], c["quantity"]) for c in components] == [
        ("ACME-2002", 2.0), ("ACME-2003", 1.0),
    ]
    asset = await AssetRepository.get_asset(serial_number, neo4j_session)
    assert asset is not None
    assert asset.release.employee == "Erika Musterfrau"
    assert asset.release.note == "Built as agreed"
    # A confirmed draft is no longer open.
    assert await AssetRepository.get_asset_drafts(neo4j_session) == []


@pytest.mark.asyncio
async def test_confirming_reserves_only_the_quantity_added_beyond_the_document(neo4j_session):
    # ACME-2003 stood on the document and was reserved with the order confirmation already —
    # reserving it again would bind the same stock twice. ACME-2002 was added by
    # engineering and has been booked by nobody yet.
    await draft_environment(neo4j_session)

    await AssetRepository.confirm_draft(
        confirmation(("ACME-2003", "1"), ("ACME-2002", "2")), "2", neo4j_session,
    )

    assert await read_stock(neo4j_session, "ACME-2003") == (100.0, 0.0)
    assert await read_stock(neo4j_session, "ACME-2002") == (100.0, 2.0)


@pytest.mark.asyncio
async def test_confirming_without_stock_for_the_added_quantity_creates_nothing(neo4j_session):
    await draft_environment(neo4j_session)
    await create_stock(neo4j_session, "ACME-2002", "1", quantity=1.0)

    with pytest.raises(BusinessLogicError, match="ACME-2002"):
        await AssetRepository.confirm_draft(
            confirmation(("ACME-2003", "1"), ("ACME-2002", "2")), "2", neo4j_session,
        )

    result = await neo4j_session.run("MATCH (a:AssetInstance) RETURN count(a) AS n")
    assert (await result.single())["n"] == 0
    assert await read_stock(neo4j_session, "ACME-2002") == (1.0, 0.0)


@pytest.mark.asyncio
async def test_a_draft_is_confirmed_at_most_once(neo4j_session):
    await draft_environment(neo4j_session)
    await AssetRepository.confirm_draft(confirmation(("ACME-2003", "1")), "2", neo4j_session)

    with pytest.raises(BusinessLogicError):
        await AssetRepository.confirm_draft(confirmation(("ACME-2003", "1")), "2", neo4j_session)


@pytest.mark.asyncio
async def test_a_document_not_marked_as_a_new_asset_cannot_be_confirmed(neo4j_session):
    await standard_environment(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await AssetRepository.confirm_draft(
            AssetDraftConfirmation(
                documentNumber=STANDARD_DOCUMENT,
                components=[ComponentLineCreate(productNumber="ACME-2003", quantity=Decimal("1"))],
            ),
            "2", neo4j_session,
        )


@pytest.mark.asyncio
async def test_a_fractional_piece_count_in_the_confirmed_bom_is_rejected(neo4j_session):
    # The same rule as on document lines — a bill of materials edited through a draft would
    # otherwise be the only way to an asset with half a piece.
    await draft_environment(neo4j_session)

    with pytest.raises(BusinessLogicError, match="whole number"):
        await AssetRepository.confirm_draft(
            confirmation(("ACME-2003", "1.5")), "2", neo4j_session,
        )

    result = await neo4j_session.run("MATCH (a:AssetInstance) RETURN count(a) AS n")
    assert (await result.single())["n"] == 0


@pytest.mark.asyncio
async def test_spare_parts_are_linked_to_existing_assets(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await create_document(neo4j_session, "OC-2026-0003", assetPurpose="spareParts")

    open_before = await AssetRepository.get_spare_parts_drafts(neo4j_session)
    result = await AssetRepository.link_spare_parts(
        "OC-2026-0003", SparePartsLink(serialNumbers=[STANDARD_SN]), neo4j_session
    )
    open_after = await AssetRepository.get_spare_parts_drafts(neo4j_session)

    assert [d.documentNumber for d in open_before] == ["OC-2026-0003"]
    assert result is not None
    assert result.serialNumbers == [STANDARD_SN]
    assert open_after == []
    # Pure traceability: no new asset, no reservation.
    result = await neo4j_session.run("MATCH (a:AssetInstance) RETURN count(a) AS n")
    assert (await result.single())["n"] == 1


@pytest.mark.asyncio
async def test_linking_spare_parts_is_all_or_nothing(neo4j_session):
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)
    await create_document(neo4j_session, "OC-2026-0003", assetPurpose="spareParts")

    with pytest.raises(NotFoundError):
        await AssetRepository.link_spare_parts(
            "OC-2026-0003",
            SparePartsLink(serialNumbers=[STANDARD_SN, "SN-9999-9999"]),
            neo4j_session,
        )

    result = await neo4j_session.run("MATCH ()-[r:SPARE_PART_FOR]->() RETURN count(r) AS n")
    assert (await result.single())["n"] == 0


# ==========================================
# SERVICE FORECAST
# ==========================================

async def asset_with_twin(session, shippedOn: date) -> str:
    """Creates an asset and ships it through the regular path."""
    created = await create_asset(session)
    await ship(session, created.serialNumber, on=shippedOn)
    return created.serialNumber


def shipped_almost_24_months_ago() -> date:
    """A shipping date that makes the 24-month O-ring fall due within the 30-day window."""
    return date.today() - timedelta(days=2 * 365 - 10)


@pytest.mark.asyncio
async def test_the_forecast_reports_parts_falling_due(neo4j_session):
    # Interval 24 months (ACME-2002); shipped almost 24 months ago, so the interval runs out
    # within a few days. The filter cartridge (48 months) is nowhere near due.
    await standard_environment(neo4j_session)
    await asset_with_twin(neo4j_session, shipped_almost_24_months_ago())

    due = await AssetServiceRepository.get_due_services(neo4j_session)

    assert [case.wearPart for case in due] == ["O-Ring 10x2"]
    assert due[0].serialNumber == STANDARD_SN
    assert due[0].customer == "Example Industries GmbH"


@pytest.mark.asyncio
async def test_the_forecast_ignores_parts_outside_the_window(neo4j_session):
    await standard_environment(neo4j_session)
    await asset_with_twin(neo4j_session, date.today() - timedelta(days=2 * 365 - 100))

    assert await AssetServiceRepository.get_due_services(neo4j_session) == []


@pytest.mark.asyncio
async def test_the_forecast_counts_from_shipping_not_from_installation(neo4j_session):
    # The counter-check of the interval calculation: the installation date lies much later.
    # Were the query to count from installation, nothing would be due.
    await standard_environment(neo4j_session)
    await asset_with_twin(neo4j_session, shipped_almost_24_months_ago())
    await AssetRepository.update_asset(
        STANDARD_SN, AssetUpdate(installedOn=date.today()), neo4j_session
    )

    due = await AssetServiceRepository.get_due_services(neo4j_session)

    assert len(due) == 1
    assert due[0].installedOn == date.today()


@pytest.mark.asyncio
async def test_the_forecast_ignores_assets_without_a_shipping_date(neo4j_session):
    # Without a shipping date there is no start of the interval. Falling back to the
    # installation date would report a wrong replacement date.
    await standard_environment(neo4j_session)
    await create_asset(neo4j_session)

    assert await AssetServiceRepository.get_due_services(neo4j_session) == []


@pytest.mark.asyncio
async def test_the_forecast_reports_unassigned_without_an_account_manager(neo4j_session):
    # OPTIONAL MATCH: a customer without an ACCOUNT_MANAGER_OF edge must not swallow the row.
    await standard_environment(neo4j_session)
    await asset_with_twin(neo4j_session, shipped_almost_24_months_ago())

    due = await AssetServiceRepository.get_due_services(neo4j_session)

    assert due[0].accountManager == "Unassigned"


@pytest.mark.asyncio
async def test_the_forecast_names_the_account_manager(neo4j_session):
    await standard_environment(neo4j_session)
    await create_employee(neo4j_session, "1", "Max Mustermann")
    await asset_with_twin(neo4j_session, shipped_almost_24_months_ago())
    await neo4j_session.run(
        """
        MATCH (e:Employee {id: '1'}), (c:Customer {id: 'C-1001'})
        CREATE (e)-[:ACCOUNT_MANAGER_OF]->(c)
        """
    )

    due = await AssetServiceRepository.get_due_services(neo4j_session)

    assert due[0].accountManager == "Max Mustermann"


@pytest.mark.asyncio
async def test_the_full_chain_from_creation_to_service(neo4j_session):
    """Create, release, ship — and the asset shows up in the service forecast.

    An asset created through the API only reaches the forecast once a shipping date is
    set; without one the query filters it out. This test shows the chain is closed.
    """
    await standard_environment(neo4j_session)

    # 1. Create — the server assigns the serial number and copies the bill of materials.
    created = await create_asset(neo4j_session)
    assert created.status == "planned"
    assert len(await components_of(neo4j_session, created.serialNumber)) == 3

    # 2. Release the bill of materials.
    after_release = await release(neo4j_session, created.serialNumber, note="Checked")
    assert after_release is not None
    assert after_release.status == "released"

    # 3. Ship — the bill of materials exists already, only the service interval starts.
    after_shipping = await ship(
        neo4j_session, created.serialNumber, on=shipped_almost_24_months_ago()
    )
    assert after_shipping is not None
    assert after_shipping.status == "shipped"
    assert after_shipping.createdComponents == 0

    # 4. The asset shows up in the service forecast.
    due = await AssetServiceRepository.get_due_services(neo4j_session)

    assert [case.serialNumber for case in due] == [created.serialNumber]
    assert due[0].wearPart == "O-Ring 10x2"


# ==========================================
# SERVICE COMPLETION
# ==========================================

@pytest.mark.asyncio
async def test_the_completion_is_empty_at_first(neo4j_session):
    await standard_environment(neo4j_session)
    await asset_with_twin(neo4j_session, shipped_almost_24_months_ago())

    due = await AssetServiceRepository.get_due_services(neo4j_session)

    assert due[0].componentInstanceId == f"{STANDARD_SN}_ACME-2002"
    assert due[0].completion is None


@pytest.mark.asyncio
async def test_the_completion_stores_technician_and_note(neo4j_session):
    await standard_environment(neo4j_session)
    await asset_with_twin(neo4j_session, shipped_almost_24_months_ago())

    result = await AssetServiceRepository.set_completion(
        f"{STANDARD_SN}_ACME-2002",
        ServiceCompletionRequest(technicianInitials="SS", note="Ring replaced"),
        neo4j_session,
    )

    assert result is not None
    assert result.completedAt == await server_date(neo4j_session)
    assert result.technicianInitials == "SS"
    assert result.note == "Ring replaced"


@pytest.mark.asyncio
async def test_the_completion_moves_the_next_due_date(neo4j_session):
    # Without switching the anchor the date would stay at the original shipping date for
    # ever. Shipping lies far in the past here: with the old formula (shipping + interval)
    # the part would be permanently overdue and would have to stay in the list.
    #
    # Both wear parts are overdue after ten years. That is the contrast, not a disturbance:
    # the filter cartridge rightly stays (never reported), only the O-ring (just reported)
    # has to disappear.
    await standard_environment(neo4j_session)
    await asset_with_twin(neo4j_session, date.today() - timedelta(days=10 * 365))
    component_instance_id = f"{STANDARD_SN}_ACME-2002"

    await AssetServiceRepository.set_completion(
        component_instance_id, ServiceCompletionRequest(technicianInitials="SS"), neo4j_session
    )

    ids = [f.componentInstanceId for f in await AssetServiceRepository.get_due_services(neo4j_session)]
    assert component_instance_id not in ids
    assert f"{STANDARD_SN}_ACME-2003" in ids


@pytest.mark.asyncio
async def test_the_completion_comes_back_with_the_next_cycle(neo4j_session):
    # The counter-check: a completion that lies almost 24 months back has to bring the
    # component into the due list again — the anchor is now completedAt, no longer the
    # (deliberately very old) shipping date.
    await standard_environment(neo4j_session)
    await asset_with_twin(neo4j_session, date.today() - timedelta(days=10 * 365))
    component_instance_id = f"{STANDARD_SN}_ACME-2002"
    await neo4j_session.run(
        """
        MATCH (ci:ComponentInstance {id: $id})
        MERGE (event:ServiceEvent {id: $id})
        SET event.completedAt = $completedAt, event.technicianInitials = 'SS',
            event.note = 'Last replacement'
        MERGE (event)-[:CONCERNS]->(ci)
        """,
        id=component_instance_id, completedAt=shipped_almost_24_months_ago(),
    )

    due = await AssetServiceRepository.get_due_services(neo4j_session)
    hit = next(f for f in due if f.componentInstanceId == component_instance_id)

    assert hit.completion is not None
    assert hit.completion.note == "Last replacement"


@pytest.mark.asyncio
async def test_reporting_again_overwrites_instead_of_duplicating(neo4j_session):
    # MERGE on ServiceEvent.id = componentInstanceId: no history, one node that gets
    # overwritten — there is only ever the one current anchor point.
    await standard_environment(neo4j_session)
    await asset_with_twin(neo4j_session, shipped_almost_24_months_ago())
    component_instance_id = f"{STANDARD_SN}_ACME-2002"

    await AssetServiceRepository.set_completion(
        component_instance_id, ServiceCompletionRequest(technicianInitials="SS"), neo4j_session
    )
    second = await AssetServiceRepository.set_completion(
        component_instance_id,
        ServiceCompletionRequest(technicianInitials="EM", note="Second report"),
        neo4j_session,
    )

    assert second is not None
    assert second.technicianInitials == "EM"
    assert second.note == "Second report"

    result = await neo4j_session.run("MATCH (event:ServiceEvent) RETURN count(event) AS n")
    assert (await result.single())["n"] == 1


@pytest.mark.asyncio
async def test_a_completion_of_an_unknown_component_returns_none(neo4j_session):
    await standard_environment(neo4j_session)

    result = await AssetServiceRepository.set_completion(
        "does-not-exist_00000", ServiceCompletionRequest(), neo4j_session
    )

    assert result is None
