"""Integration tests of the procurement domain against a real Neo4j.

These tests answer one question: **does the Cypher do what the code claims?**
Everything that can be checked without a database — status codes, error translation, the
cent conversion as a pure function — lives in the API and unit tests and is deliberately
not repeated here.

The rules of the reorder analysis live here, because none of them can be shown with mocks:

* **The preferred supplier beats the price** — the ordering inside the query decides
* **A product without a supplier still appears** — the `OPTIONAL MATCH` on the edge
* **A product without `minStock` is left out deliberately** — the `IS NOT NULL` filter
* **Taking a product into the supply range is idempotent** — the `MERGE` on the edge

Every test builds its data itself through Cypher. Deliberately not through the repositories
of other domains: these tests check the procurement queries, not the product creation path
of the catalog. A dependency on it would turn them red on a change in `catalog`.
"""

from decimal import Decimal

import pytest

from core.exceptions import NotFoundError
from domains.procurement.repository_procurement import (
    ReorderSuggestionRepository,
    SupplierRepository,
)
from domains.procurement.schemas_procurement import (
    ReorderSuggestion,
    SupplierCreate,
    SupplierProductCreate,
    SupplierUpdate,
)

# ==========================================
# HELPERS
# ==========================================

async def create_product(
    session,
    number: str,
    *,
    label: str = "Test product",
    minStock: int | None = None,
    targetStock: int | None = None,
) -> None:
    """Creates a product with optional stock bounds.

    A `None` removes the property in Neo4j — exactly the state "bound not maintained" that
    the reorder analysis has to filter out.
    """
    await session.run(
        """
        MERGE (p:Product:Part {number: $number})
        SET p.label       = $label,
            p.minStock    = $minStock,
            p.targetStock = $targetStock
        """,
        number=number,
        label=label,
        minStock=minStock,
        targetStock=targetStock,
    )


async def create_stock(
    session, productNumber: str, locationId: str, quantity: float, reserved: float = 0.0
) -> None:
    """Creates a stock record — one node per product and location, as in the seed."""
    await session.run(
        """
        MATCH (p:Product {number: $productNumber})
        MERGE (l:Location {id: $locationId})
          ON CREATE SET l.name = 'Test location ' + $locationId, l.type = 'Warehouse'
        MERGE (s:StockLevel {id: $productNumber + '_' + $locationId})
        SET s.quantity = $quantity, s.reserved = $reserved
        MERGE (p)-[:HAS_STOCK]->(s)
        MERGE (s)-[:AT_LOCATION]->(l)
        """,
        productNumber=productNumber,
        locationId=locationId,
        quantity=quantity,
        reserved=reserved,
    )


async def create_supplier(session, id: str, name: str, city: str | None = None) -> None:
    """Creates a supplier directly in the graph, bypassing the repository."""
    await session.run(
        "MERGE (s:Supplier {id: $id}) SET s.name = $name, s.city = $city",
        id=id,
        name=name,
        city=city,
    )


async def supplies(
    session,
    supplierId: str,
    productNumber: str,
    *,
    priceCent: int,
    leadTimeDays: int = 5,
    preferred: bool | None = False,
) -> None:
    """Links supplier and product with the conditions on the edge.

    `preferred=None` leaves the flag unset — the state of an imported edge.
    """
    await session.run(
        """
        MATCH (s:Supplier {id: $supplierId})
        MATCH (p:Product  {number: $productNumber})
        MERGE (s)-[sp:SUPPLIES_PRODUCT]->(p)
        SET sp.purchasePriceCent   = $priceCent,
            sp.leadTimeDays        = $leadTimeDays,
            sp.isPreferredSupplier = $preferred
        """,
        supplierId=supplierId,
        productNumber=productNumber,
        priceCent=priceCent,
        leadTimeDays=leadTimeDays,
        preferred=preferred,
    )


async def edges_between(session, supplierId: str, productNumber: str) -> list[dict]:
    """Reads every SUPPLIES_PRODUCT edge between the two nodes."""
    result = await session.run(
        """
        MATCH (s:Supplier {id: $supplierId})-[sp:SUPPLIES_PRODUCT]->(p:Product {number: $productNumber})
        RETURN sp{.*} AS edge
        """,
        supplierId=supplierId,
        productNumber=productNumber,
    )
    return [record["edge"] async for record in result]


def find(suggestions: list[ReorderSuggestion], number: str) -> ReorderSuggestion | None:
    """Looks a suggestion up by its product number."""
    return next((s for s in suggestions if s.productNumber == number), None)


# ==========================================
# CREATING SUPPLIERS
# ==========================================

@pytest.mark.asyncio
async def test_creating_writes_the_node_with_a_prefixed_id(neo4j_session):
    # The server assigns the id. The prefix makes it recognisable in the graph that the
    # node came about through the API.
    created = await SupplierRepository.create_supplier(
        SupplierCreate(name="Example Hydraulics GmbH", city="4020 Linz"), neo4j_session
    )

    assert created.id.startswith("S-")

    read = await SupplierRepository.get_supplier(created.id, neo4j_session)
    assert read is not None
    assert read.name == "Example Hydraulics GmbH"
    assert read.city == "4020 Linz"


@pytest.mark.asyncio
async def test_two_suppliers_get_different_ids(neo4j_session):
    first = await SupplierRepository.create_supplier(SupplierCreate(name="First"), neo4j_session)
    second = await SupplierRepository.create_supplier(SupplierCreate(name="Second"), neo4j_session)

    assert first.id != second.id


@pytest.mark.asyncio
async def test_creating_sets_createdat_but_no_updatedat(neo4j_session):
    # An updatedAt on creation would claim a change that never happened. It stays empty
    # until the first PATCH arrives.
    created = await SupplierRepository.create_supplier(
        SupplierCreate(name="Example Hydraulics GmbH"), neo4j_session
    )

    read = await SupplierRepository.get_supplier(created.id, neo4j_session)
    assert read is not None
    assert read.createdAt is not None
    assert read.updatedAt is None


@pytest.mark.asyncio
async def test_creating_with_a_name_only_leaves_the_other_fields_empty(neo4j_session):
    # Only `name` is mandatory. The six other fields may be missing and then have to come
    # back as null — not as an empty string.
    created = await SupplierRepository.create_supplier(
        SupplierCreate(name="Example Hydraulics GmbH"), neo4j_session
    )

    read = await SupplierRepository.get_supplier(created.id, neo4j_session)
    assert read is not None
    assert (
        read.email, read.phone, read.street, read.city, read.country, read.vatId
    ) == (None, None, None, None, None, None)


@pytest.mark.asyncio
async def test_an_unknown_id_returns_none(neo4j_session):
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")

    assert await SupplierRepository.get_supplier("S-does-not-exist", neo4j_session) is None


# ==========================================
# READING AND SEARCHING SUPPLIERS
# ==========================================

@pytest.mark.asyncio
async def test_the_list_returns_every_supplier(neo4j_session):
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_supplier(neo4j_session, "S-002", "Beta Supply", "Munich")

    suppliers = await SupplierRepository.get_suppliers(neo4j_session)

    assert {supplier.id for supplier in suppliers} == {"S-001", "S-002"}


@pytest.mark.asyncio
async def test_the_search_matches_the_name(neo4j_session):
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_supplier(neo4j_session, "S-002", "Beta Supply", "Munich")

    hits = await SupplierRepository.get_suppliers(neo4j_session, search="Alpha")

    assert [supplier.id for supplier in hits] == ["S-001"]


@pytest.mark.asyncio
async def test_the_search_matches_the_city(neo4j_session):
    # The city is the second search field — purchasing searches by it to find out whom it
    # has in a region.
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_supplier(neo4j_session, "S-002", "Beta Supply", "Munich")

    hits = await SupplierRepository.get_suppliers(neo4j_session, search="Munich")

    assert [supplier.id for supplier in hits] == ["S-002"]


@pytest.mark.asyncio
async def test_the_search_ignores_case(neo4j_session):
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")

    hits = await SupplierRepository.get_suppliers(neo4j_session, search="aLpHa")

    assert [supplier.id for supplier in hits] == ["S-001"]


@pytest.mark.asyncio
async def test_a_blank_search_filters_nothing(neo4j_session):
    # A cleared search field arrives as whitespace. Read as a filter it would be a "contains
    # nothing", and the list would look empty although nothing was searched for.
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_supplier(neo4j_session, "S-002", "Beta Supply", "Munich")

    hits = await SupplierRepository.get_suppliers(neo4j_session, search="   ")

    assert {supplier.id for supplier in hits} == {"S-001", "S-002"}


@pytest.mark.asyncio
async def test_a_search_without_hits_returns_an_empty_list(neo4j_session):
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")

    assert await SupplierRepository.get_suppliers(neo4j_session, search="Omega") == []


# ==========================================
# CHANGING SUPPLIERS
# ==========================================

@pytest.mark.asyncio
async def test_a_partial_update_leaves_the_other_fields_in_place(neo4j_session):
    # The actual proof of exclude_unset: fields that were not sent must not be written as
    # null. Otherwise a PATCH on the e-mail clears the whole remaining address — and nobody
    # notices, because the call reports 200.
    created = await SupplierRepository.create_supplier(
        SupplierCreate(
            name="Example Hydraulics GmbH",
            email="office@hydraulics.example",
            street="Industriestrasse 10",
            city="4020 Linz",
            country="AT",
            vatId="ATU00000009",
        ),
        neo4j_session,
    )

    changed = await SupplierRepository.update_supplier(
        created.id, SupplierUpdate(email="new@hydraulics.example"), neo4j_session
    )

    assert changed is not None
    assert changed.email == "new@hydraulics.example"
    assert changed.name == "Example Hydraulics GmbH"
    assert changed.street == "Industriestrasse 10"
    assert changed.city == "4020 Linz"
    assert changed.country == "AT"
    assert changed.vatId == "ATU00000009"


@pytest.mark.asyncio
async def test_a_partial_update_sets_updatedat(neo4j_session):
    created = await SupplierRepository.create_supplier(
        SupplierCreate(name="Example Hydraulics GmbH"), neo4j_session
    )

    changed = await SupplierRepository.update_supplier(
        created.id, SupplierUpdate(email="new@hydraulics.example"), neo4j_session
    )

    assert changed is not None
    assert changed.updatedAt is not None


@pytest.mark.asyncio
async def test_a_change_on_an_unknown_id_returns_none(neo4j_session):
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")

    result = await SupplierRepository.update_supplier(
        "S-does-not-exist", SupplierUpdate(email="new@hydraulics.example"), neo4j_session
    )

    assert result is None


# ==========================================
# SUPPLY RANGE
# ==========================================

@pytest.mark.asyncio
async def test_taking_a_product_in_creates_exactly_one_edge_with_conditions(neo4j_session):
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_product(neo4j_session, "ACME-2002", label="O-Ring 10x2")

    await SupplierRepository.add_supplied_product(
        "S-001",
        SupplierProductCreate(
            productNumber="ACME-2002",
            leadTimeDays=6,
            purchasePrice=Decimal("5.80"),
            isPreferredSupplier=True,
        ),
        neo4j_session,
    )

    edges = await edges_between(neo4j_session, "S-001", "ACME-2002")
    assert len(edges) == 1
    assert edges[0]["leadTimeDays"] == 6
    assert edges[0]["isPreferredSupplier"] is True


@pytest.mark.asyncio
async def test_a_second_call_updates_instead_of_duplicating(neo4j_session):
    # Called twice with the same combination there is ONE edge with the conditions sent
    # last. Without MERGE the product would then have two prices at the same supplier, and
    # the reorder analysis would pick one of them at random.
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_product(neo4j_session, "ACME-2002")

    first = await SupplierRepository.add_supplied_product(
        "S-001",
        SupplierProductCreate(
            productNumber="ACME-2002",
            leadTimeDays=6,
            purchasePrice=Decimal("5.80"),
            isPreferredSupplier=True,
        ),
        neo4j_session,
    )
    second = await SupplierRepository.add_supplied_product(
        "S-001",
        SupplierProductCreate(
            productNumber="ACME-2002",
            leadTimeDays=10,
            purchasePrice=Decimal("6.20"),
            isPreferredSupplier=False,
        ),
        neo4j_session,
    )

    assert first["wasUpdated"] is False
    assert second["wasUpdated"] is True

    edges = await edges_between(neo4j_session, "S-001", "ACME-2002")
    assert len(edges) == 1
    assert edges[0]["leadTimeDays"] == 10
    assert edges[0]["purchasePriceCent"] == 620
    assert edges[0]["isPreferredSupplier"] is False


@pytest.mark.asyncio
async def test_an_imported_edge_without_createdat_counts_as_updated(neo4j_session):
    # The coalesce in the query: an edge from an import carries no createdAt. Taking the
    # product in again is an update of that edge, not a creation.
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_product(neo4j_session, "ACME-2002")
    await supplies(neo4j_session, "S-001", "ACME-2002", priceCent=500)

    result = await SupplierRepository.add_supplied_product(
        "S-001",
        SupplierProductCreate(
            productNumber="ACME-2002", leadTimeDays=6, purchasePrice=Decimal("5.80"),
        ),
        neo4j_session,
    )

    assert result["wasUpdated"] is True


@pytest.mark.asyncio
async def test_the_price_is_stored_as_integer_cents(neo4j_session):
    # 8.20 EUR is the classic: via a float detour the conversion yields 819.
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_product(neo4j_session, "ACME-2002")

    await SupplierRepository.add_supplied_product(
        "S-001",
        SupplierProductCreate(
            productNumber="ACME-2002",
            leadTimeDays=6,
            purchasePrice=Decimal("8.20"),
            isPreferredSupplier=False,
        ),
        neo4j_session,
    )

    edges = await edges_between(neo4j_session, "S-001", "ACME-2002")
    assert edges[0]["purchasePriceCent"] == 820
    assert isinstance(edges[0]["purchasePriceCent"], int)


@pytest.mark.asyncio
async def test_an_unknown_supplier_is_not_found(neo4j_session):
    await create_product(neo4j_session, "ACME-2002")

    with pytest.raises(NotFoundError) as excinfo:
        await SupplierRepository.add_supplied_product(
            "S-does-not-exist",
            SupplierProductCreate(
                productNumber="ACME-2002",
                leadTimeDays=6,
                purchasePrice=Decimal("5.80"),
                isPreferredSupplier=False,
            ),
            neo4j_session,
        )

    assert "supplier" in str(excinfo.value)
    # MATCH rather than MERGE across the path: no ghost supplier came about.
    result = await neo4j_session.run("MATCH (s:Supplier) RETURN count(s) AS n")
    assert (await result.single())["n"] == 0


@pytest.mark.asyncio
async def test_an_unknown_product_is_not_found(neo4j_session):
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")

    with pytest.raises(NotFoundError) as excinfo:
        await SupplierRepository.add_supplied_product(
            "S-001",
            SupplierProductCreate(
                productNumber="99999",
                leadTimeDays=6,
                purchasePrice=Decimal("5.80"),
                isPreferredSupplier=False,
            ),
            neo4j_session,
        )

    assert "product" in str(excinfo.value)
    result = await neo4j_session.run("MATCH (p:Product) RETURN count(p) AS n")
    assert (await result.single())["n"] == 0


# ==========================================
# REORDER ANALYSIS — SUPPLIER SELECTION
# ==========================================

@pytest.mark.asyncio
async def test_the_preferred_supplier_beats_the_cheaper_price(neo4j_session):
    # Alpha has to appear although Beta is cheaper at 79.50 EUR. The order is
    # isPreferredSupplier DESC, only then purchasePriceCent ASC — not the other way round.
    await create_product(
        neo4j_session, "ACME-2007", label="Vibration Sensor", minStock=100, targetStock=200
    )
    await create_stock(neo4j_session, "ACME-2007", "1", 64.0)
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_supplier(neo4j_session, "S-002", "Beta Supply", "Munich")
    await create_supplier(neo4j_session, "S-003", "Gamma Parts", "Berlin")
    await supplies(neo4j_session, "S-001", "ACME-2007", priceCent=8500, preferred=True)
    await supplies(neo4j_session, "S-002", "ACME-2007", priceCent=7950, preferred=False)
    await supplies(neo4j_session, "S-003", "ACME-2007", priceCent=9200, preferred=False)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    suggestion = find(suggestions, "ACME-2007")
    assert suggestion is not None
    assert suggestion.supplier == "Alpha Components"
    assert suggestion.unitPrice == Decimal("85.00")
    assert suggestion.suggestedQuantity == 136


@pytest.mark.asyncio
async def test_an_edge_without_the_flag_does_not_displace_the_preferred_supplier(neo4j_session):
    # null sorts BEFORE true on a descending sort in Cypher. Without the coalesce an
    # imported edge that carries no flag at all would win over the real preferred supplier.
    await create_product(neo4j_session, "ACME-2007", minStock=100, targetStock=200)
    await create_stock(neo4j_session, "ACME-2007", "1", 10.0)
    await create_supplier(neo4j_session, "S-001", "Alpha Components", "Stuttgart")
    await create_supplier(neo4j_session, "S-002", "Beta Supply", "Munich")
    await supplies(neo4j_session, "S-001", "ACME-2007", priceCent=8500, preferred=True)
    await supplies(neo4j_session, "S-002", "ACME-2007", priceCent=7000, preferred=None)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    suggestion = find(suggestions, "ACME-2007")
    assert suggestion is not None
    assert suggestion.supplier == "Alpha Components"


@pytest.mark.asyncio
async def test_with_the_same_preference_the_price_wins(neo4j_session):
    # isPreferredSupplier is not exclusive. If neither carries it (or both do), the
    # purchase price decides.
    await create_product(neo4j_session, "ACME-2001", minStock=100, targetStock=200)
    await create_stock(neo4j_session, "ACME-2001", "1", 10.0)
    await create_supplier(neo4j_session, "S-002", "Beta Supply", "Munich")
    await create_supplier(neo4j_session, "S-003", "Gamma Parts", "Berlin")
    await supplies(neo4j_session, "S-002", "ACME-2001", priceCent=7950, preferred=False)
    await supplies(neo4j_session, "S-003", "ACME-2001", priceCent=9200, preferred=False)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    suggestion = find(suggestions, "ACME-2001")
    assert suggestion is not None
    assert suggestion.supplier == "Beta Supply"


@pytest.mark.asyncio
async def test_a_product_without_a_supplier_appears_without_a_source(neo4j_session):
    # A product below its minimum stock without a SUPPLIES_PRODUCT edge has to appear —
    # otherwise exactly the unmaintained product drops out of the analysis, and nobody
    # notices that it is missing.
    await create_product(neo4j_session, "ACME-2003", minStock=500, targetStock=1500)
    await create_stock(neo4j_session, "ACME-2003", "1", 365.0)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    suggestion = find(suggestions, "ACME-2003")
    assert suggestion is not None
    assert suggestion.supplier is None
    assert suggestion.unitPrice is None
    assert suggestion.leadTimeDays is None
    assert suggestion.suggestedQuantity == 1135


# ==========================================
# REORDER ANALYSIS — WHO DROPS OUT
# ==========================================

@pytest.mark.asyncio
async def test_a_product_without_a_minimum_stock_drops_out(neo4j_session):
    # Without the filter `minStock IS NOT NULL` the comparison `stock < null` evaluates to
    # null. The row would vanish silently — the filter turns that into a deliberate
    # decision instead of a side effect.
    await create_product(neo4j_session, "NO-BOUND")
    await create_stock(neo4j_session, "NO-BOUND", "1", 0.0)
    await create_product(neo4j_session, "WITH-BOUND", minStock=10, targetStock=50)
    await create_stock(neo4j_session, "WITH-BOUND", "1", 0.0)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    assert find(suggestions, "NO-BOUND") is None
    assert find(suggestions, "WITH-BOUND") is not None


@pytest.mark.asyncio
async def test_a_product_above_its_minimum_stock_does_not_appear(neo4j_session):
    await create_product(neo4j_session, "ACME-2001", minStock=50, targetStock=200)
    await create_stock(neo4j_session, "ACME-2001", "1", 100.0)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    assert find(suggestions, "ACME-2001") is None


@pytest.mark.asyncio
async def test_an_empty_graph_returns_an_empty_list(neo4j_session):
    assert await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session) == []


# ==========================================
# REORDER ANALYSIS — STOCK AND QUANTITY
# ==========================================

@pytest.mark.asyncio
async def test_a_product_without_a_stock_record_counts_as_stock_zero(neo4j_session):
    # The OPTIONAL MATCH is mandatory: a product without any stock record has an effective
    # stock of 0 and urgently needs ordering. With a plain MATCH exactly this case drops
    # out — the most urgent of all.
    await create_product(neo4j_session, "NEW-124", minStock=50, targetStock=5000)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    suggestion = find(suggestions, "NEW-124")
    assert suggestion is not None
    assert suggestion.currentStock == 0
    assert suggestion.suggestedQuantity == 5000


@pytest.mark.asyncio
async def test_stock_is_summed_across_several_locations(neo4j_session):
    # There is a stock node of its own per product and location. Without the sum,
    # purchasing would get a suggestion based on a single location.
    await create_product(neo4j_session, "ACME-2001", minStock=100, targetStock=200)
    await create_stock(neo4j_session, "ACME-2001", "1", 30.0)
    await create_stock(neo4j_session, "ACME-2001", "2", 20.0)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    suggestion = find(suggestions, "ACME-2001")
    assert suggestion is not None
    assert suggestion.currentStock == 50
    assert suggestion.suggestedQuantity == 150


@pytest.mark.asyncio
async def test_reserved_goods_are_not_ordered_twice(neo4j_session):
    # The total stock is subtracted, not the available one. Reserved goods sit physically in
    # the warehouse and have been procured already — ordering them a second time would be a
    # double order. A reservation is made against an order that takes the goods out, not
    # against a demand that asks for more.
    await create_product(neo4j_session, "ACME-2001", minStock=100, targetStock=200)
    await create_stock(neo4j_session, "ACME-2001", "1", 60.0, reserved=50.0)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    suggestion = find(suggestions, "ACME-2001")
    assert suggestion is not None
    # 60, not 10 — the available stock would be 60 - 50 = 10.
    assert suggestion.currentStock == 60
    assert suggestion.suggestedQuantity == 140


@pytest.mark.asyncio
async def test_the_quantity_never_goes_negative(neo4j_session):
    # If the stock lies above the target stock, the difference would be negative — possible
    # with inconsistently maintained bounds. The product stays in the suggestion (it is
    # below its minimum), but with quantity 0.
    await create_product(neo4j_session, "SKEWED-1", minStock=2000, targetStock=1000)
    await create_stock(neo4j_session, "SKEWED-1", "1", 1500.0)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    assert all(suggestion.suggestedQuantity >= 0 for suggestion in suggestions)
    suggestion = find(suggestions, "SKEWED-1")
    assert suggestion is not None
    assert suggestion.suggestedQuantity == 0


@pytest.mark.asyncio
async def test_a_missing_target_stock_returns_quantity_zero(neo4j_session):
    # A reorder suggestion without a quantity is useless for purchasing. The product
    # therefore appears with quantity 0 and stands out as incompletely maintained, instead
    # of poisoning the analysis with a null.
    await create_product(neo4j_session, "HALF-MAINTAINED", minStock=10)
    await create_stock(neo4j_session, "HALF-MAINTAINED", "1", 2.0)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    suggestion = find(suggestions, "HALF-MAINTAINED")
    assert suggestion is not None
    assert suggestion.suggestedQuantity == 0


@pytest.mark.asyncio
async def test_suggestions_are_ordered_by_quantity_largest_first(neo4j_session):
    # Purchasing works the list from the top. The largest gap belongs there, not whatever
    # product the graph happens to return first.
    await create_product(neo4j_session, "SMALL", minStock=10, targetStock=20)
    await create_product(neo4j_session, "LARGE", minStock=10, targetStock=500)
    await create_product(neo4j_session, "MEDIUM", minStock=10, targetStock=100)

    suggestions = await ReorderSuggestionRepository.get_reorder_suggestions(neo4j_session)

    assert [s.productNumber for s in suggestions] == ["LARGE", "MEDIUM", "SMALL"]
