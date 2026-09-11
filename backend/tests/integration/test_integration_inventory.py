"""Integration tests of the inventory domain against a real Neo4j.

These tests answer one question: **does the Cypher do what the code claims?**
Everything that can be checked without a database — status codes, error translation,
schema validation, the truth table of the movement types — lives in the API and unit tests
and is deliberately not repeated here.

Covered are the areas that cannot be checked with mocks in principle:

* the **aggregation** of stock across several locations, including `byLocation` and
  `available`
* the **traversal** behind the filters, because `productNumber` and `locationId` are not
  properties of the movement but are resolved through edges
* the **atomicity** of booking and cache update — in particular, that a rejected booking
  leaves nothing behind
* the **invariant `quantity = f(movements)`**, which can only be shown against real data
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.exceptions import BusinessLogicError, NotFoundError
from domains.inventory.repository_inventory import (
    LocationRepository,
    StockMovementRepository,
    StockRepository,
)
from domains.inventory.schemas_inventory import StockMovementCreate, StockMovementResponse

# ==========================================
# HELPERS
# ==========================================

async def create_base_data(session, product: str = "ACME-2001") -> None:
    """Creates a product, two locations, a customer and a document.

    Deliberately through Cypher rather than through the repositories of other domains:
    these tests check the inventory queries, not the product creation path of the catalog.
    A dependency on it would turn them red on a change in `catalog`.
    """
    await session.run(
        """
        MERGE (p:Product:Part {number: $product})
          ON CREATE SET p.label = 'Screw M6x20', p.unit = 'pcs'
        """,
        product=product,
    )
    await session.run(
        """
        MERGE (l1:Location {id: '1'}) ON CREATE SET l1.name = 'Central Warehouse', l1.type = 'Warehouse'
        MERGE (l2:Location {id: '2'}) ON CREATE SET l2.name = 'Service Van 1', l2.type = 'Vehicle'
        MERGE (c:Customer {id: 'C-1001'}) ON CREATE SET c.name = 'Example Industries GmbH'
        MERGE (d:Document {number: 'PO-1'}) ON CREATE SET d.type = 'PurchaseOrder'
        """
    )


async def book(session, **fields) -> StockMovementResponse:
    """Books through the regular repository path."""
    data = {"productNumber": "ACME-2001", "quantity": 10, "type": "Receipt", "locationId": "1"}
    data.update(fields)
    return await StockMovementRepository.post_movement(StockMovementCreate(**data), session)


async def transfer(session, **fields) -> StockMovementResponse:
    """Books a transfer through the regular repository path."""
    data = {
        "productNumber": "ACME-2001", "quantity": 10, "type": "Transfer",
        "locationId": "1", "targetLocationId": "2",
    }
    data.update(fields)
    return await StockMovementRepository.post_transfer(StockMovementCreate(**data), session)


async def cache_matches_history(session) -> bool:
    """Recomputes the stock cache from the history and compares it with the actual values.

    This is the invariant `quantity = f(movements)`, and the reason booking and cache update
    have to run in one transaction.

    The recomputation replays the movements in booking order with `reduce()` rather than
    summing them: an issue floors `reserved` at 0 at the moment it is booked, so a plain sum
    with a floor at the end would diverge as soon as an issue without a reservation precedes
    a reservation.
    """
    result = await session.run(
        """
        MATCH (s:StockLevel)
        OPTIONAL MATCH (s)<-[:POSTED_TO]-(m:StockMovement)
        WITH s, m ORDER BY m.createdAt, m.id
        WITH s, collect(m) AS movements
        WITH s,
             reduce(q = 0.0, m IN movements |
                 CASE m.type WHEN 'Issue' THEN q - m.quantity
                             WHEN 'Reservation' THEN q
                             ELSE q + m.quantity END) AS expectedQuantity,
             reduce(r = 0.0, m IN movements |
                 CASE m.type WHEN 'Reservation' THEN r + m.quantity
                             WHEN 'Issue' THEN CASE WHEN r - m.quantity < 0 THEN 0.0
                                                    ELSE r - m.quantity END
                             ELSE r END) AS expectedReserved
        RETURN count(CASE WHEN s.quantity <> expectedQuantity
                            OR s.reserved <> expectedReserved THEN 1 END) AS deviations
        """
    )
    record = await result.single()
    return record["deviations"] == 0


async def movement_count(session) -> int:
    result = await session.run("MATCH (m:StockMovement) RETURN count(m) AS n")
    return (await result.single())["n"]


# ==========================================
# LOCATIONS
# ==========================================

@pytest.mark.asyncio
async def test_locations_are_read_from_the_graph(neo4j_session):
    await create_base_data(neo4j_session)

    locations = await LocationRepository.get_locations(neo4j_session)

    assert {location.id for location in locations} == {"1", "2"}
    assert {location.type for location in locations} == {"Warehouse", "Vehicle"}


@pytest.mark.asyncio
async def test_locations_without_data_are_an_empty_list(neo4j_session):
    assert await LocationRepository.get_locations(neo4j_session) == []


@pytest.mark.asyncio
async def test_a_numeric_location_id_is_read_as_a_string(neo4j_session):
    # Depending on its origin the id may sit in the graph as a number. The projection casts
    # it, so the read model carries every key uniformly as a string.
    await neo4j_session.run("CREATE (:Location {id: 7, name: 'Numeric', type: 'Warehouse'})")

    locations = await LocationRepository.get_locations(neo4j_session)

    assert [location.id for location in locations] == ["7"]


# ==========================================
# STOCK ACROSS SEVERAL LOCATIONS
# ==========================================

@pytest.mark.asyncio
async def test_stock_aggregates_across_several_locations(neo4j_session):
    # The core of the stock query: there is a stock node of its own per product and
    # location. The sums and byLocation only come about in the query.
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100, locationId="1")
    await book(neo4j_session, quantity=20, locationId="2")

    stock = await StockRepository.get_stock("ACME-2001", neo4j_session)

    assert stock is not None
    assert stock.totalStock == 120.0
    assert {loc.locationId: loc.quantity for loc in stock.byLocation} == {"1": 100.0, "2": 20.0}
    assert stock.label == "Screw M6x20"


@pytest.mark.asyncio
async def test_available_is_quantity_minus_reserved(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100)
    await book(neo4j_session, quantity=30, type="Reservation", customerId="C-1001")

    stock = await StockRepository.get_stock("ACME-2001", neo4j_session)

    assert stock is not None
    assert (stock.totalStock, stock.reserved, stock.available) == (100.0, 30.0, 70.0)
    assert stock.byLocation[0].available == 70.0


@pytest.mark.asyncio
async def test_stock_of_an_unknown_product_is_none(neo4j_session):
    await create_base_data(neo4j_session)

    assert await StockRepository.get_stock("DOES-NOT-EXIST", neo4j_session) is None


@pytest.mark.asyncio
async def test_stock_without_a_stock_record_returns_zero_values(neo4j_session):
    # The distinction the query has to draw: an unknown product means None and becomes a
    # 404, a product without a stock record means 0 and is a valid result.
    await create_base_data(neo4j_session)

    stock = await StockRepository.get_stock("ACME-2001", neo4j_session)

    assert stock is not None
    assert stock.totalStock == 0.0
    assert stock.byLocation == []


# ==========================================
# STOCK LIST: EVERY PRODUCT, OPTIONALLY FILTERED
# ==========================================

@pytest.mark.asyncio
async def test_the_list_contains_every_product(neo4j_session):
    await create_base_data(neo4j_session, product="ACME-2001")
    await create_base_data(neo4j_session, product="ACME-2003")
    await book(neo4j_session, productNumber="ACME-2001", quantity=50)
    await book(neo4j_session, productNumber="ACME-2003", quantity=10)

    stock_list = await StockRepository.get_stock_list(neo4j_session)

    assert [s.productNumber for s in stock_list] == ["ACME-2001", "ACME-2003"]


@pytest.mark.asyncio
async def test_the_list_contains_products_without_a_stock_record_too(neo4j_session):
    # The same principle as for the single lookup: no stock record means 0, not omission.
    await create_base_data(neo4j_session, product="ACME-2001")

    stock_list = await StockRepository.get_stock_list(neo4j_session)

    assert len(stock_list) == 1
    assert stock_list[0].totalStock == 0.0
    assert stock_list[0].byLocation == []


@pytest.mark.asyncio
async def test_the_list_filters_to_products_below_minimum_stock(neo4j_session):
    await create_base_data(neo4j_session, product="BELOW")
    await create_base_data(neo4j_session, product="ABOVE")
    await neo4j_session.run("MATCH (p:Product {number: 'BELOW'}) SET p.minStock = 100")
    await neo4j_session.run("MATCH (p:Product {number: 'ABOVE'}) SET p.minStock = 5")
    await book(neo4j_session, productNumber="BELOW", quantity=10)
    await book(neo4j_session, productNumber="ABOVE", quantity=10)

    stock_list = await StockRepository.get_stock_list(neo4j_session, below_min_stock=True)

    assert {s.productNumber for s in stock_list} == {"BELOW"}


@pytest.mark.asyncio
async def test_the_filter_sums_across_locations_before_comparing(neo4j_session):
    # 6 + 6 at two locations is above a minimum of 10, although each location alone is
    # below it. The filter has to run after the aggregation, not per stock node.
    await create_base_data(neo4j_session, product="SPLIT")
    await neo4j_session.run("MATCH (p:Product {number: 'SPLIT'}) SET p.minStock = 10")
    await book(neo4j_session, productNumber="SPLIT", quantity=6, locationId="1")
    await book(neo4j_session, productNumber="SPLIT", quantity=6, locationId="2")

    stock_list = await StockRepository.get_stock_list(neo4j_session, below_min_stock=True)

    assert stock_list == []


@pytest.mark.asyncio
async def test_the_filter_leaves_out_products_without_a_maintained_minimum(neo4j_session):
    # The same rule as in the reorder analysis (procurement): `stock < null` is null in
    # Cypher, and the row drops out. An unmaintained product must not look inconspicuous in
    # the filter.
    await create_base_data(neo4j_session, product="UNMAINTAINED")

    stock_list = await StockRepository.get_stock_list(neo4j_session, below_min_stock=True)

    assert stock_list == []


@pytest.mark.asyncio
async def test_the_list_without_a_filter_shows_products_without_a_minimum_too(neo4j_session):
    await create_base_data(neo4j_session, product="UNMAINTAINED")

    stock_list = await StockRepository.get_stock_list(neo4j_session)

    assert {s.productNumber for s in stock_list} == {"UNMAINTAINED"}


@pytest.mark.asyncio
async def test_the_list_without_products_is_an_empty_list(neo4j_session):
    assert await StockRepository.get_stock_list(neo4j_session) == []


# ==========================================
# RESERVATION AND ITS RELEASE
# ==========================================

@pytest.mark.asyncio
async def test_a_reservation_binds_without_lowering_the_stock(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100)

    response = await book(neo4j_session, quantity=40, type="Reservation", customerId="C-1001")

    assert (response.newQuantity, response.reserved) == (100.0, 40.0)
    stock = await StockRepository.get_stock("ACME-2001", neo4j_session)
    assert stock is not None
    assert stock.available == 60.0


@pytest.mark.asyncio
async def test_an_issue_releases_the_reservation_in_the_same_operation(neo4j_session):
    # An issue lowers quantity AND reserved. Without that the goods would stay bound for
    # ever after the delivery.
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100)
    await book(neo4j_session, quantity=40, type="Reservation", customerId="C-1001")

    response = await book(neo4j_session, quantity=40, type="Issue", customerId="C-1001")

    assert (response.newQuantity, response.reserved) == (60.0, 0.0)


@pytest.mark.asyncio
async def test_an_issue_without_a_reservation_leaves_reserved_at_zero(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100)

    response = await book(neo4j_session, quantity=10, type="Issue")

    assert (response.newQuantity, response.reserved) == (90.0, 0.0)
    assert await cache_matches_history(neo4j_session)


@pytest.mark.asyncio
async def test_a_reservation_beyond_the_available_stock_is_capped(neo4j_session):
    # An order confirmation for more than is available should still come about — otherwise
    # it never shows up in the reorder analysis — but it must not reserve more than there
    # is. The movement records what was actually reserved, not what was requested, so the
    # history still adds up.
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=10)

    response = await book(neo4j_session, quantity=25, type="Reservation", customerId="C-1001")

    assert response.reserved == 10.0
    result = await neo4j_session.run(
        "MATCH (m:StockMovement {type: 'Reservation'}) RETURN m.quantity AS quantity"
    )
    assert (await result.single())["quantity"] == 10.0
    assert await cache_matches_history(neo4j_session)


@pytest.mark.asyncio
async def test_a_reservation_with_nothing_available_is_rejected(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=10)
    await book(neo4j_session, quantity=10, type="Reservation", customerId="C-1001")
    before = await movement_count(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await book(neo4j_session, quantity=1, type="Reservation", customerId="C-1001")

    assert await movement_count(neo4j_session) == before


# ==========================================
# REJECTED BOOKINGS LEAVE NOTHING BEHIND
# ==========================================

@pytest.mark.asyncio
async def test_overbooking_is_rejected_and_changes_nothing(neo4j_session):
    # The actual reason for the single transaction: the check sits between two statements.
    # Were it to run outside, a movement without an effect on the stock would remain here —
    # or a stock without the matching movement.
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=50)
    movements_before = await movement_count(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await book(neo4j_session, quantity=51, type="Issue")

    stock = await StockRepository.get_stock("ACME-2001", neo4j_session)
    assert stock is not None
    assert stock.totalStock == 50.0
    assert await movement_count(neo4j_session) == movements_before
    assert await cache_matches_history(neo4j_session)


@pytest.mark.asyncio
async def test_an_unknown_product_creates_no_stock_node(neo4j_session):
    # The MERGE on the stock level sits in the same query as the MATCH on the product. If
    # the MATCH fails, the MERGE must not have taken effect either.
    await create_base_data(neo4j_session)

    with pytest.raises(NotFoundError) as excinfo:
        await book(neo4j_session, productNumber="DOES-NOT-EXIST")

    assert "product" in str(excinfo.value)
    result = await neo4j_session.run("MATCH (s:StockLevel) RETURN count(s) AS n")
    assert (await result.single())["n"] == 0


@pytest.mark.asyncio
async def test_an_unknown_location_is_rejected(neo4j_session):
    await create_base_data(neo4j_session)

    with pytest.raises(NotFoundError) as excinfo:
        await book(neo4j_session, locationId="99")

    assert "location" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_unknown_customer_is_rejected(neo4j_session):
    await create_base_data(neo4j_session)

    with pytest.raises(NotFoundError) as excinfo:
        await book(neo4j_session, type="Reservation", customerId="999999")

    assert "customer" in str(excinfo.value)
    assert await movement_count(neo4j_session) == 0


@pytest.mark.asyncio
async def test_an_unknown_document_is_rejected(neo4j_session):
    await create_base_data(neo4j_session)

    with pytest.raises(NotFoundError) as excinfo:
        await book(neo4j_session, documentNumber="PO-999")

    assert "document" in str(excinfo.value)
    assert await movement_count(neo4j_session) == 0


# ==========================================
# TRANSFER — TWO LOCATIONS, ONE TRANSACTION
# ==========================================
# A direct transfer without an intermediate state: one call books both movements in the
# same transaction.

@pytest.mark.asyncio
async def test_a_transfer_moves_stock_between_locations(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=50, locationId="1")

    response = await transfer(neo4j_session, quantity=20)

    assert response.newQuantity == 30.0
    assert response.targetNewQuantity == 20.0
    stock = await StockRepository.get_stock("ACME-2001", neo4j_session)
    assert stock is not None
    locations = {loc.locationId: loc.quantity for loc in stock.byLocation}
    assert locations == {"1": 30.0, "2": 20.0}
    assert await cache_matches_history(neo4j_session)


@pytest.mark.asyncio
async def test_a_transfer_leaves_the_reservation_untouched(neo4j_session):
    # Unlike an issue, a transfer must neither create nor release a reservation — it is a
    # warehouse-internal operation, not a sale.
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=50, locationId="1")
    await book(neo4j_session, quantity=15, type="Reservation", customerId="C-1001", locationId="1")

    response = await transfer(neo4j_session, quantity=20)

    assert response.reserved == 15.0


@pytest.mark.asyncio
async def test_a_transfer_beyond_the_stock_is_rejected_and_changes_nothing(neo4j_session):
    # The reason for the single transaction: if the outgoing booking at the source fails,
    # nothing may arrive at the destination.
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=10, locationId="1")
    movements_before = await movement_count(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await transfer(neo4j_session, quantity=20)

    stock = await StockRepository.get_stock("ACME-2001", neo4j_session)
    assert stock is not None
    locations = {loc.locationId: loc.quantity for loc in stock.byLocation}
    assert locations.get("1") == 10.0
    assert "2" not in locations
    assert await movement_count(neo4j_session) == movements_before


@pytest.mark.asyncio
async def test_a_transfer_to_an_unknown_target_location_is_rejected(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=50, locationId="1")

    with pytest.raises(NotFoundError) as excinfo:
        await transfer(neo4j_session, quantity=10, targetLocationId="99")

    assert "location" in str(excinfo.value)
    assert "99" in str(excinfo.value)
    # The outgoing booking at the source was rolled back with the failed incoming one.
    stock = await StockRepository.get_stock("ACME-2001", neo4j_session)
    assert stock is not None
    assert stock.totalStock == 50.0


@pytest.mark.asyncio
async def test_a_transfer_creates_two_movements_with_opposite_signs(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=50, locationId="1")

    await transfer(neo4j_session, quantity=20)

    result = await neo4j_session.run(
        "MATCH (m:StockMovement {type: 'Transfer'}) RETURN m.quantity AS quantity ORDER BY m.quantity"
    )
    quantities = [record["quantity"] async for record in result]
    assert quantities == [-20.0, 20.0]


# ==========================================
# SHAPE OF THE CREATED MOVEMENT
# ==========================================

@pytest.mark.asyncio
async def test_a_booking_creates_the_node_and_its_edges(neo4j_session):
    await create_base_data(neo4j_session)

    response = await book(
        neo4j_session, purchasePrice=Decimal("0.14"), documentNumber="PO-1", note="Test"
    )

    result = await neo4j_session.run(
        """
        MATCH (m:StockMovement {id: $id})
        OPTIONAL MATCH (m)-[r]->()
        RETURN m.type AS type, m.quantity AS quantity, m.note AS note,
               m.purchasePriceCent AS cent, m.createdAt AS createdAt,
               collect(DISTINCT type(r)) AS edges
        """,
        id=response.movementId,
    )
    record = await result.single()

    assert record["type"] == "Receipt"
    assert record["note"] == "Test"
    # Money sits in the graph as integer cents, not as euro.
    assert record["cent"] == 14
    assert record["createdAt"] is not None
    assert set(record["edges"]) == {"POSTED_TO", "BASED_ON_DOCUMENT"}


@pytest.mark.asyncio
async def test_the_first_booking_creates_the_stock_node_with_its_edges(neo4j_session):
    # On the first receipt at a location the stock level does not exist yet. Without
    # HAS_STOCK and AT_LOCATION it could be found neither through the product nor through
    # the location — the stock query would never find it.
    await create_base_data(neo4j_session)

    await book(neo4j_session, locationId="2")

    result = await neo4j_session.run(
        """
        MATCH (p:Product {number: 'ACME-2001'})-[:HAS_STOCK]->(s:StockLevel)-[:AT_LOCATION]->(l:Location)
        RETURN s.id AS id, l.id AS locationId
        """
    )
    record = await result.single()

    # The key follows the same construction as in the seed.
    assert record["id"] == "ACME-2001_2"
    assert record["locationId"] == "2"


@pytest.mark.asyncio
async def test_a_second_booking_creates_no_second_stock_node(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=10)
    await book(neo4j_session, quantity=10)

    result = await neo4j_session.run("MATCH (s:StockLevel) RETURN count(s) AS n")
    assert (await result.single())["n"] == 1


# ==========================================
# INVARIANT quantity = f(movements)
# ==========================================

@pytest.mark.asyncio
async def test_the_invariant_holds_across_every_movement_type(neo4j_session):
    # After every movement booked through the API, recomputing the cache from the history
    # has to yield the same value.
    await create_base_data(neo4j_session)

    await book(neo4j_session, quantity=100)
    await book(neo4j_session, quantity=40, type="Reservation", customerId="C-1001")
    await book(neo4j_session, quantity=40, type="Issue", customerId="C-1001")
    await book(neo4j_session, quantity=-10, type="Correction", note="Stocktaking")
    await book(neo4j_session, quantity=25, locationId="2")
    await transfer(neo4j_session, quantity=5)

    assert await cache_matches_history(neo4j_session)
    stock = await StockRepository.get_stock("ACME-2001", neo4j_session)
    assert stock is not None
    assert stock.totalStock == 75.0


@pytest.mark.asyncio
async def test_no_stock_goes_negative(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=10)

    for quantity, type in [(20, "Issue"), (-20, "Correction")]:
        with pytest.raises(BusinessLogicError):
            await book(neo4j_session, quantity=quantity, type=type)

    result = await neo4j_session.run(
        "MATCH (s:StockLevel) WHERE s.quantity < 0 RETURN count(s) AS n"
    )
    assert (await result.single())["n"] == 0


# ==========================================
# FILTERS OF THE MOVEMENT HISTORY
# ==========================================

@pytest.mark.asyncio
async def test_the_history_resolves_product_location_and_customer(neo4j_session):
    # None of the three fields is a property of the movement — all three only come about
    # through the traversal in the query.
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100)
    await book(neo4j_session, quantity=5, type="Reservation", customerId="C-1001")

    movements = await StockMovementRepository.get_movements(neo4j_session)

    assert {m.productNumber for m in movements} == {"ACME-2001"}
    assert {m.locationName for m in movements} == {"Central Warehouse"}
    assert {m.customerName for m in movements} == {None, "Example Industries GmbH"}


@pytest.mark.asyncio
async def test_the_history_filters_by_location_through_the_edge(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, locationId="1")
    await book(neo4j_session, locationId="2")
    await book(neo4j_session, locationId="2")

    hits = await StockMovementRepository.get_movements(neo4j_session, location_id="2")

    assert len(hits) == 2
    assert {m.locationName for m in hits} == {"Service Van 1"}


@pytest.mark.asyncio
async def test_the_history_filters_by_type(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100)
    await book(neo4j_session, quantity=5, type="Issue")
    await book(neo4j_session, quantity=5, type="Issue")

    hits = await StockMovementRepository.get_movements(neo4j_session, type="Issue")

    assert len(hits) == 2


@pytest.mark.asyncio
async def test_the_history_filters_by_product(neo4j_session):
    await create_base_data(neo4j_session)
    await neo4j_session.run(
        "MERGE (p:Product:Part {number: 'ACME-2007'}) SET p.label = 'Vibration Sensor', p.unit = 'pcs'"
    )
    await book(neo4j_session, productNumber="ACME-2001")
    await book(neo4j_session, productNumber="ACME-2007")

    hits = await StockMovementRepository.get_movements(
        neo4j_session, product_number="ACME-2007"
    )

    assert len(hits) == 1
    assert hits[0].productNumber == "ACME-2007"


@pytest.mark.asyncio
async def test_the_history_filters_by_period_inclusively(neo4j_session):
    # createdAt is a timestamp, the filter bounds are dates. A booking from today therefore
    # has to stay in the result when to_date is today — a naive comparison against midnight
    # would exclude it.
    await create_base_data(neo4j_session)
    await book(neo4j_session)
    today = datetime.now(UTC).date()

    assert len(await StockMovementRepository.get_movements(
        neo4j_session, from_date=today, to_date=today)) == 1
    assert len(await StockMovementRepository.get_movements(
        neo4j_session, from_date=today + timedelta(days=1))) == 0
    assert len(await StockMovementRepository.get_movements(
        neo4j_session, to_date=today - timedelta(days=1))) == 0


@pytest.mark.asyncio
async def test_the_history_combines_filters_with_and(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100, locationId="1")
    await book(neo4j_session, quantity=5, type="Issue", locationId="1")
    await book(neo4j_session, quantity=100, locationId="2")

    hits = await StockMovementRepository.get_movements(
        neo4j_session, location_id="1", type="Issue"
    )

    assert len(hits) == 1


@pytest.mark.asyncio
async def test_the_history_without_hits_is_an_empty_list(neo4j_session):
    await create_base_data(neo4j_session)
    await book(neo4j_session)

    assert await StockMovementRepository.get_movements(
        neo4j_session, product_number="DOES-NOT-EXIST"
    ) == []


@pytest.mark.asyncio
async def test_the_history_converts_cents_back_to_euro(neo4j_session):
    # The round trip of the money convention: euro in, integer cents in the graph, euro out.
    await create_base_data(neo4j_session)
    await book(neo4j_session, purchasePrice=Decimal("12.34"))

    movements = await StockMovementRepository.get_movements(neo4j_session)

    assert movements[0].purchasePrice == Decimal("12.34")


@pytest.mark.asyncio
async def test_the_history_is_sorted_newest_first(neo4j_session):
    await create_base_data(neo4j_session)
    for _ in range(3):
        await book(neo4j_session)

    movements = await StockMovementRepository.get_movements(neo4j_session)

    timestamps = [m.createdAt for m in movements]
    assert all(t is not None for t in timestamps)
    assert timestamps == sorted(timestamps, key=lambda t: t, reverse=True)  # type: ignore[arg-type,return-value]


@pytest.mark.asyncio
async def test_movement_ids_are_unique(neo4j_session):
    # There is a uniqueness constraint on id. A reused id would not be a duplicate but a
    # rejected second booking.
    await create_base_data(neo4j_session)
    ids = {(await book(neo4j_session)).movementId for _ in range(5)}

    assert len(ids) == 5
    assert all(movement_id.startswith("mov-") for movement_id in ids)


# ==========================================
# FRACTIONAL QUANTITIES FOR PIECE GOODS
# ==========================================
# A piece cannot be moved in halves. The check sits in `post_movement` and therefore in the
# shared funnel of every booking — documents and assets book through it as well — rather
# than only in the API path.


@pytest.mark.asyncio
@pytest.mark.parametrize("type", ["Receipt", "Issue", "Reservation"])
async def test_a_fractional_quantity_for_pcs_is_rejected(neo4j_session, type):
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100)
    before = await movement_count(neo4j_session)

    with pytest.raises(BusinessLogicError, match="whole number"):
        await book(neo4j_session, type=type, quantity=2.5)

    # Nothing half-booked left behind.
    assert await movement_count(neo4j_session) == before
    assert await cache_matches_history(neo4j_session)


@pytest.mark.asyncio
async def test_a_transfer_with_a_fractional_quantity_for_pcs_is_rejected(neo4j_session):
    # A test of its own, because the transfer takes a different path through
    # `post_transfer`: two bookings in one transaction.
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=100)
    before = await movement_count(neo4j_session)

    with pytest.raises(BusinessLogicError, match="whole number"):
        await transfer(neo4j_session, quantity=2.5)

    # Both bookings of the transfer have to stay out, not only the second — otherwise the
    # source would be debited and nothing would have arrived at the destination.
    assert await movement_count(neo4j_session) == before
    assert await cache_matches_history(neo4j_session)


@pytest.mark.asyncio
async def test_a_correction_may_book_a_fractional_quantity(neo4j_session):
    # The deliberate exception. The correction is the only signed movement type and the
    # emergency exit for wrong stock figures: if 2.5 pieces sit in the system through bad
    # data, straightening it out needs exactly that -0.5. A rule forbidding it would lock the
    # tool against the state it exists to remove.
    await create_base_data(neo4j_session)
    await book(neo4j_session, quantity=10)

    result = await book(neo4j_session, type="Correction", quantity=-0.5)

    assert result.newQuantity == 9.5
    assert await cache_matches_history(neo4j_session)


@pytest.mark.asyncio
async def test_a_product_in_metres_may_book_a_fractional_quantity(neo4j_session):
    # The counter-check: the rule depends on the unit, not on the movement type. Without it
    # the test above would only prove that something gets rejected at all.
    await create_base_data(neo4j_session)
    await neo4j_session.run(
        """
        MERGE (p:Product:Part {number: 'ACME-2005'})
          ON CREATE SET p.label = 'Hose 2m', p.unit = 'm'
        """
    )

    result = await book(neo4j_session, productNumber="ACME-2005", quantity=2.5)

    assert result.newQuantity == 2.5


@pytest.mark.asyncio
async def test_a_whole_quantity_for_pcs_stays_allowed(neo4j_session):
    await create_base_data(neo4j_session)

    result = await book(neo4j_session, quantity=3.0)

    assert result.newQuantity == 3.0
