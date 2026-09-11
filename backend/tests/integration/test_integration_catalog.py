"""Integration tests of the catalog domain against a real Neo4j.

These tests answer one question: **does the Cypher do what the code claims?**
Everything that can be checked without a database — status codes, error translation,
schema validation — lives in the API and unit tests and is deliberately not repeated here.

Covered are the areas that cannot be checked with mocks in principle:

* the **map projection**, which derives `type`, `hasBom` and `active` from the graph
  structure
* the **bill-of-materials resolution** including the depth control
* the **cycle guard**, which is a path query inside the Cypher
* the **label invariant** `:Assembly` / `:Part` across its whole life cycle
* the **category assignment**, which hangs off an edge rather than a property
"""

from decimal import Decimal

import pytest

from core.exceptions import BusinessLogicError, DuplicateKeyError, NotFoundError
from domains.catalog.repository_catalog import (
    BomRepository,
    CategoryRepository,
    ProductRepository,
)
from domains.catalog.schemas_catalog import (
    BomLineCreate,
    GroupRef,
    ProductCreate,
    ProductUpdate,
)

# ==========================================
# HELPERS
# ==========================================

async def create_product(session, number: str, label: str = "Test product"):
    """Creates a product through the regular repository path."""
    return await ProductRepository.create_product(
        ProductCreate(
            number=number,
            label=label,
            unit="pcs",
            minStock=1,
            targetStock=10,
            listPrice=Decimal("9.99"),
        ),
        session,
    )


async def add_component(session, parent: str, child: str, quantity: int = 1):
    """Links two products with a CONTAINS edge."""
    return await BomRepository.add_component(
        parent, BomLineCreate(componentNumber=child, quantity=quantity), session
    )


async def labels_of(session, number: str) -> set[str]:
    """Reads the actual labels of a node directly from the graph."""
    result = await session.run(
        "MATCH (p:Product {number: $number}) RETURN labels(p) AS labels", number=number
    )
    record = await result.single()
    return set(record["labels"])


async def create_group_chain(
    session,
    number: str,
    subcategoryId: int,
    productGroupId: int,
    categoryId: int,
    categoryName: str | None,
) -> None:
    """Links a product with a category over subcategory and product group.

    The same shape the seed builds: (:Product)-[:BELONGS_TO]->(:Subcategory)-[:PART_OF]
    ->(:ProductGroup)-[:BELONGS_TO_CATEGORY]->(:Category). `categoryName=None` leaves the
    property unset (SET with null removes it) — the case of a category whose name has not
    been maintained yet.
    """
    await session.run(
        """
        MATCH (p:Product {number: $number})
        MERGE (s:Subcategory {id: $subcategoryId})
        MERGE (g:ProductGroup {id: $productGroupId})
        MERGE (c:Category {id: $categoryId})
        SET c.name = $categoryName
        MERGE (p)-[:BELONGS_TO]->(s)
        MERGE (s)-[:PART_OF]->(g)
        MERGE (g)-[:BELONGS_TO_CATEGORY]->(c)
        """,
        number=number, subcategoryId=subcategoryId, productGroupId=productGroupId,
        categoryId=categoryId, categoryName=categoryName,
    )


# ==========================================
# MAP PROJECTION: type, hasBom, active
# ==========================================

@pytest.mark.asyncio
async def test_product_without_edges_is_a_part(neo4j_session):
    await create_product(neo4j_session, "P-1")

    product = await ProductRepository.get_product("P-1", neo4j_session)

    assert product is not None
    assert product.type == "Part"
    assert product.hasBom is False


@pytest.mark.asyncio
async def test_product_with_an_edge_is_an_assembly(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")
    await add_component(neo4j_session, "P-1", "P-2")

    parent = await ProductRepository.get_product("P-1", neo4j_session)
    child = await ProductRepository.get_product("P-2", neo4j_session)

    assert parent is not None and child is not None
    assert (parent.type, parent.hasBom) == ("Assembly", True)
    # The child has an INCOMING edge — that does not count. Only outgoing CONTAINS edges
    # make a product an assembly.
    assert (child.type, child.hasBom) == ("Part", False)


@pytest.mark.asyncio
async def test_a_stored_hasbom_property_is_ignored(neo4j_session):
    # A node an import could leave behind: flagged as having a bill of materials, but
    # without any component. The graph structure wins.
    await neo4j_session.run(
        "CREATE (:Product:Part {number: 'P-1', label: 'Wrongly flagged', hasBom: true})"
    )

    product = await ProductRepository.get_product("P-1", neo4j_session)

    assert product is not None
    assert product.hasBom is False
    assert product.type == "Part"


@pytest.mark.asyncio
async def test_a_missing_active_property_counts_as_active(neo4j_session):
    # A thin node without an active property, as a MERGE during an import creates it.
    await neo4j_session.run(
        "CREATE (:Product:Part {number: 'P-1', label: 'Without status'})"
    )

    product = await ProductRepository.get_product("P-1", neo4j_session)

    assert product is not None
    assert product.active is True


@pytest.mark.asyncio
async def test_a_set_active_property_is_taken_over(neo4j_session):
    await neo4j_session.run(
        "CREATE (:Product:Part {number: 'P-1', label: 'Inactive', active: false})"
    )

    product = await ProductRepository.get_product("P-1", neo4j_session)

    assert product is not None
    assert product.active is False


@pytest.mark.asyncio
async def test_a_new_product_gets_active_set(neo4j_session):
    # create_product writes active=true explicitly, so new nodes hold the same state as
    # imported ones and do not depend on the coalesce when read.
    await create_product(neo4j_session, "P-1")

    result = await neo4j_session.run(
        "MATCH (p:Product {number: 'P-1'}) RETURN p.active AS active"
    )
    assert (await result.single())["active"] is True


# ==========================================
# MAP PROJECTION: category, productGroup, subcategory
# ==========================================

@pytest.mark.asyncio
async def test_product_without_a_group_has_empty_group_fields(neo4j_session):
    await create_product(neo4j_session, "P-1")

    product = await ProductRepository.get_product("P-1", neo4j_session)

    assert product is not None
    assert product.category is None
    assert product.productGroup is None
    assert product.subcategory is None


@pytest.mark.asyncio
async def test_product_with_a_group_chain_carries_all_three_levels(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_group_chain(
        neo4j_session, "P-1", subcategoryId=38, productGroupId=5, categoryId=1,
        categoryName="Hardware",
    )

    product = await ProductRepository.get_product("P-1", neo4j_session)

    assert product is not None
    assert product.category == GroupRef(id=1, name="Hardware")
    # Product group and subcategory carry no name here — the placeholder built from the id
    # keeps the selection usable anyway.
    assert product.productGroup == GroupRef(id=5, name="Group 5")
    assert product.subcategory == GroupRef(id=38, name="Subcategory 38")


@pytest.mark.asyncio
async def test_a_category_without_a_maintained_name_gets_a_placeholder(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_group_chain(
        neo4j_session, "P-1", subcategoryId=38, productGroupId=5, categoryId=1,
        categoryName=None,
    )

    product = await ProductRepository.get_product("P-1", neo4j_session)

    assert product is not None
    assert product.category == GroupRef(id=1, name="Category 1")


# ==========================================
# PRODUCT NUMBER CHANGES
# ==========================================

@pytest.mark.asyncio
async def test_the_product_number_can_be_corrected(neo4j_session):
    # A typo made at creation should be correctable without deleting the product and
    # rebuilding its bill of materials.
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")
    await add_component(neo4j_session, "P-1", "P-2")

    await ProductRepository.update_product("P-1", ProductUpdate(number="P-42"), neo4j_session)

    # The bill of materials hangs off an edge and survives the renaming.
    bom = await BomRepository.get_bom("P-42", 1, neo4j_session)
    assert [line["component"].number for line in bom] == ["P-2"]
    assert await ProductRepository.get_product("P-1", neo4j_session) is None


@pytest.mark.asyncio
async def test_renaming_to_an_existing_number_is_a_duplicate_key_error(neo4j_session):
    # Without catching the ConstraintError this would surface as a DatabaseError and thus
    # an HTTP 500, although the client caused the error.
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")

    with pytest.raises(DuplicateKeyError) as excinfo:
        await ProductRepository.update_product("P-1", ProductUpdate(number="P-2"), neo4j_session)

    assert "P-2" in str(excinfo.value)


# ==========================================
# QUERY FILTERS AGAINST THE REAL GRAPH
# ==========================================

@pytest.mark.asyncio
async def test_the_type_filter_separates_by_labels(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")
    await create_product(neo4j_session, "P-3")
    await add_component(neo4j_session, "P-1", "P-2")

    assemblies = await ProductRepository.get_products(neo4j_session, type="Assembly")
    parts = await ProductRepository.get_products(neo4j_session, type="Part")

    assert {p.number for p in assemblies} == {"P-1"}
    assert {p.number for p in parts} == {"P-2", "P-3"}


@pytest.mark.asyncio
async def test_the_search_is_case_insensitive_and_hits_both_fields(neo4j_session):
    await create_product(neo4j_session, "P-1", label="Drum pump complete")
    await create_product(neo4j_session, "PUMP-2", label="Cap screw")
    await create_product(neo4j_session, "P-3", label="Cable")

    hits = await ProductRepository.get_products(neo4j_session, search="pUmP")

    # Once through the label, once through the number.
    assert {p.number for p in hits} == {"P-1", "PUMP-2"}


@pytest.mark.asyncio
async def test_the_active_filter_applies(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await neo4j_session.run(
        "CREATE (:Product:Part {number: 'P-2', label: 'Inactive', active: false})"
    )

    active = await ProductRepository.get_products(neo4j_session, active=True)
    inactive = await ProductRepository.get_products(neo4j_session, active=False)

    assert {p.number for p in active} == {"P-1"}
    assert {p.number for p in inactive} == {"P-2"}


@pytest.mark.asyncio
async def test_filters_add_up(neo4j_session):
    await create_product(neo4j_session, "P-1", label="Pump large")
    await create_product(neo4j_session, "P-2", label="Pump small")
    await create_product(neo4j_session, "PUMP-3", label="Gasket")
    await add_component(neo4j_session, "P-1", "P-2")

    hits = await ProductRepository.get_products(
        neo4j_session, search="pump", type="Assembly"
    )

    # PUMP-3 is the actual touchstone. In Cypher AND binds tighter than OR: without the
    # parentheses around the search fragment the condition would read as
    #     number CONTAINS $search  OR  (label CONTAINS $search AND p:Assembly)
    # and PUMP-3 would get through on its number alone, although it is a part. A product
    # whose NUMBER matches and which is not an assembly has to be in the test data for
    # that — with hits in the label only, the bug would stay invisible.
    assert {p.number for p in hits} == {"P-1"}


# ==========================================
# BILL-OF-MATERIALS RESOLUTION
# ==========================================

@pytest.mark.asyncio
async def test_bom_default_depth_returns_only_direct_components(neo4j_session):
    # A chain over three levels: P-1 -> P-2 -> P-3
    for number in ("P-1", "P-2", "P-3"):
        await create_product(neo4j_session, number)
    await add_component(neo4j_session, "P-1", "P-2", quantity=4)
    await add_component(neo4j_session, "P-2", "P-3", quantity=2)

    bom = await BomRepository.get_bom("P-1", 1, neo4j_session)

    assert len(bom) == 1
    assert bom[0]["component"].number == "P-2"
    assert bom[0]["quantity"] == 4
    # Depth 1 means: the level below is not loaded at all.
    assert bom[0]["subComponents"] == []


@pytest.mark.asyncio
async def test_bom_depth_two_nests_the_second_level(neo4j_session):
    for number in ("P-1", "P-2", "P-3"):
        await create_product(neo4j_session, number)
    await add_component(neo4j_session, "P-1", "P-2")
    await add_component(neo4j_session, "P-2", "P-3", quantity=7)

    bom = await BomRepository.get_bom("P-1", 2, neo4j_session)

    assert len(bom) == 1
    sub_components = bom[0]["subComponents"]
    assert len(sub_components) == 1
    assert sub_components[0]["component"].number == "P-3"
    assert sub_components[0]["quantity"] == 7


@pytest.mark.asyncio
async def test_bom_depth_minus_one_resolves_the_whole_tree(neo4j_session):
    # Four levels — deeper than anything in the demo data.
    for number in ("P-1", "P-2", "P-3", "P-4"):
        await create_product(neo4j_session, number)
    await add_component(neo4j_session, "P-1", "P-2")
    await add_component(neo4j_session, "P-2", "P-3")
    await add_component(neo4j_session, "P-3", "P-4")

    bom = await BomRepository.get_bom("P-1", -1, neo4j_session)

    level2 = bom[0]["subComponents"]
    level3 = level2[0]["subComponents"]
    assert bom[0]["component"].number == "P-2"
    assert level2[0]["component"].number == "P-3"
    assert level3[0]["component"].number == "P-4"
    assert level3[0]["subComponents"] == []


@pytest.mark.asyncio
async def test_bom_returns_several_components_of_the_same_level(neo4j_session):
    for number in ("P-1", "P-2", "P-3"):
        await create_product(neo4j_session, number)
    await add_component(neo4j_session, "P-1", "P-2")
    await add_component(neo4j_session, "P-1", "P-3")

    bom = await BomRepository.get_bom("P-1", 1, neo4j_session)

    assert {line["component"].number for line in bom} == {"P-2", "P-3"}


@pytest.mark.asyncio
async def test_bom_carries_fractional_quantities(neo4j_session):
    # Lines of goods sold by the metre carry fractional quantities. The read model has to be
    # able to represent them — which is why quantity in BomLine is a Decimal.
    for number in ("P-1", "P-2"):
        await create_product(neo4j_session, number)
    await neo4j_session.run(
        """
        MATCH (p:Product {number: 'P-1'}), (c:Product {number: 'P-2'})
        CREATE (p)-[:CONTAINS {quantity: 1.4, unit: 'm'}]->(c)
        SET p:Assembly REMOVE p:Part
        """
    )

    bom = await BomRepository.get_bom("P-1", 1, neo4j_session)

    assert bom[0]["quantity"] == pytest.approx(1.4)


@pytest.mark.asyncio
async def test_bom_of_a_part_is_empty(neo4j_session):
    await create_product(neo4j_session, "P-1")

    bom = await BomRepository.get_bom("P-1", -1, neo4j_session)

    assert bom == []


@pytest.mark.asyncio
async def test_bom_components_carry_the_computed_fields(neo4j_session):
    # The components run through the map projection too, not only the entry node.
    for number in ("P-1", "P-2", "P-3"):
        await create_product(neo4j_session, number)
    await add_component(neo4j_session, "P-1", "P-2")
    await add_component(neo4j_session, "P-2", "P-3")

    bom = await BomRepository.get_bom("P-1", 1, neo4j_session)

    # P-2 has a bill of materials of its own and therefore has to appear as an assembly.
    assert bom[0]["component"].type == "Assembly"
    assert bom[0]["component"].hasBom is True
    assert bom[0]["component"].active is True


@pytest.mark.asyncio
async def test_bom_shows_the_same_part_in_two_branches(neo4j_session):
    # The cycle guard in build_tree forbids repetition within one path only. A screw that
    # sits in the kit directly and in the pump below it has to appear in both places.
    for number in ("KIT", "PUMP", "SCREW"):
        await create_product(neo4j_session, number)
    await add_component(neo4j_session, "KIT", "PUMP")
    await add_component(neo4j_session, "KIT", "SCREW", quantity=12)
    await add_component(neo4j_session, "PUMP", "SCREW", quantity=4)

    bom = await BomRepository.get_bom("KIT", -1, neo4j_session)

    by_number = {line["component"].number: line for line in bom}
    assert by_number["SCREW"]["quantity"] == 12
    pump_children = by_number["PUMP"]["subComponents"]
    assert [(c["component"].number, c["quantity"]) for c in pump_children] == [("SCREW", 4)]


# ==========================================
# CYCLES
# ==========================================

@pytest.mark.asyncio
async def test_a_direct_cycle_is_rejected(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")
    await add_component(neo4j_session, "P-1", "P-2")

    # P-2 must not contain P-1, otherwise the two would contain each other.
    with pytest.raises(BusinessLogicError):
        await add_component(neo4j_session, "P-2", "P-1")


@pytest.mark.asyncio
async def test_an_indirect_cycle_is_rejected(neo4j_session):
    for number in ("P-1", "P-2", "P-3"):
        await create_product(neo4j_session, number)
    await add_component(neo4j_session, "P-1", "P-2")
    await add_component(neo4j_session, "P-2", "P-3")

    # The circle only closes over two corners — the path query in Cypher has to search at
    # any depth, not only check the direct opposite direction.
    with pytest.raises(BusinessLogicError):
        await add_component(neo4j_session, "P-3", "P-1")


@pytest.mark.asyncio
async def test_a_self_reference_is_rejected(neo4j_session):
    await create_product(neo4j_session, "P-1")

    with pytest.raises(BusinessLogicError):
        await add_component(neo4j_session, "P-1", "P-1")


@pytest.mark.asyncio
async def test_a_rejected_cycle_leaves_no_edge_behind(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")
    await add_component(neo4j_session, "P-1", "P-2")

    with pytest.raises(BusinessLogicError):
        await add_component(neo4j_session, "P-2", "P-1")

    # The transaction must not leave anything half-finished behind.
    result = await neo4j_session.run("MATCH ()-[e:CONTAINS]->() RETURN count(e) AS count")
    assert (await result.single())["count"] == 1
    assert "Assembly" not in await labels_of(neo4j_session, "P-2")


@pytest.mark.asyncio
async def test_adding_to_an_unknown_parent_is_not_found(neo4j_session):
    await create_product(neo4j_session, "P-2")

    with pytest.raises(NotFoundError):
        await add_component(neo4j_session, "UNKNOWN", "P-2")


# ==========================================
# LABEL INVARIANT
# ==========================================

@pytest.mark.asyncio
async def test_a_new_product_is_a_part(neo4j_session):
    await create_product(neo4j_session, "P-1")

    assert await labels_of(neo4j_session, "P-1") == {"Product", "Part"}


@pytest.mark.asyncio
async def test_the_first_component_makes_the_parent_an_assembly(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")

    await add_component(neo4j_session, "P-1", "P-2")

    # Exactly one of the two labels — not both.
    assert await labels_of(neo4j_session, "P-1") == {"Product", "Assembly"}


@pytest.mark.asyncio
async def test_removing_the_last_component_makes_a_part_again(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")
    await add_component(neo4j_session, "P-1", "P-2")

    result = await BomRepository.delete_component("P-1", "P-2", neo4j_session)

    assert result is not None
    assert result.remainingLines == 0
    assert await labels_of(neo4j_session, "P-1") == {"Product", "Part"}


@pytest.mark.asyncio
async def test_removing_the_second_to_last_component_keeps_the_assembly(neo4j_session):
    for number in ("P-1", "P-2", "P-3"):
        await create_product(neo4j_session, number)
    await add_component(neo4j_session, "P-1", "P-2")
    await add_component(neo4j_session, "P-1", "P-3")

    result = await BomRepository.delete_component("P-1", "P-2", neo4j_session)

    assert result is not None
    assert result.remainingLines == 1
    assert await labels_of(neo4j_session, "P-1") == {"Product", "Assembly"}


@pytest.mark.asyncio
async def test_the_component_remains_after_removal(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")
    await add_component(neo4j_session, "P-1", "P-2")

    await BomRepository.delete_component("P-1", "P-2", neo4j_session)

    # The edge is deleted, not the product. A component usually sits in several assemblies.
    assert await ProductRepository.get_product("P-2", neo4j_session) is not None


@pytest.mark.asyncio
async def test_removing_a_missing_edge_returns_none(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")

    result = await BomRepository.delete_component("P-1", "P-2", neo4j_session)

    # The repository reports None; the NotFoundError is raised by the service.
    assert result is None


@pytest.mark.asyncio
async def test_the_quantity_is_updated_on_an_existing_edge(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")
    await add_component(neo4j_session, "P-1", "P-2", quantity=3)

    result = await add_component(neo4j_session, "P-1", "P-2", quantity=8)

    assert result["wasUpdated"] is True
    assert result["quantity"] == 8
    # No second edge between the same nodes.
    count = await neo4j_session.run("MATCH ()-[e:CONTAINS]->() RETURN count(e) AS count")
    assert (await count.single())["count"] == 1


@pytest.mark.asyncio
async def test_a_new_edge_reports_was_updated_false(neo4j_session):
    # The router tells 201 from 200 by this flag, so it has to be False on the first call.
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")

    result = await add_component(neo4j_session, "P-1", "P-2", quantity=3)

    assert result["wasUpdated"] is False


@pytest.mark.asyncio
async def test_the_unit_is_kept_when_it_is_not_sent(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_product(neo4j_session, "P-2")
    await BomRepository.add_component(
        "P-1", BomLineCreate(componentNumber="P-2", quantity=2, unit="m"), neo4j_session
    )

    # Second call without a unit — the coalesce in the query must not overwrite the
    # existing unit with null.
    await BomRepository.add_component(
        "P-1", BomLineCreate(componentNumber="P-2", quantity=5), neo4j_session
    )

    result = await neo4j_session.run(
        "MATCH ()-[e:CONTAINS]->() RETURN e.unit AS unit, e.quantity AS quantity"
    )
    record = await result.single()
    assert record["unit"] == "m"
    assert record["quantity"] == 5


# ==========================================
# CategoryRepository.get_hierarchy
# ==========================================

@pytest.mark.asyncio
async def test_the_hierarchy_without_data_is_an_empty_list(neo4j_session):
    assert await CategoryRepository.get_hierarchy(neo4j_session) == []


@pytest.mark.asyncio
async def test_the_hierarchy_returns_the_nested_structure(neo4j_session):
    await create_product(neo4j_session, "P-1")
    await create_group_chain(
        neo4j_session, "P-1", subcategoryId=38, productGroupId=5, categoryId=1,
        categoryName="Hardware",
    )
    # A second subcategory under the same product group — the two-stage collect() must
    # neither lose nor duplicate it.
    await neo4j_session.run(
        """
        MATCH (g:ProductGroup {id: 5})
        MERGE (s:Subcategory {id: 39})
        MERGE (s)-[:PART_OF]->(g)
        """
    )

    hierarchy = await CategoryRepository.get_hierarchy(neo4j_session)

    assert [c.id for c in hierarchy] == [1]
    category = hierarchy[0]
    assert category.name == "Hardware"
    assert [g.id for g in category.productGroups] == [5]
    group = category.productGroups[0]
    assert group.name == "Group 5"
    assert {s.id for s in group.subcategories} == {38, 39}


@pytest.mark.asyncio
async def test_a_product_group_without_subcategories_has_an_empty_list(neo4j_session):
    await neo4j_session.run(
        """
        MERGE (c:Category {id: 2}) SET c.name = 'Accessories'
        MERGE (g:ProductGroup {id: 6})
        MERGE (g)-[:BELONGS_TO_CATEGORY]->(c)
        """
    )

    hierarchy = await CategoryRepository.get_hierarchy(neo4j_session)

    hit = next(c for c in hierarchy if c.id == 2)
    group = next(g for g in hit.productGroups if g.id == 6)
    assert group.subcategories == []


@pytest.mark.asyncio
async def test_the_hierarchy_is_ordered_by_category_id(neo4j_session):
    # Deliberately created in reverse order.
    await neo4j_session.run("MERGE (c2:Category {id: 2}) SET c2.name = 'Second'")
    await neo4j_session.run("MERGE (c1:Category {id: 1}) SET c1.name = 'First'")

    hierarchy = await CategoryRepository.get_hierarchy(neo4j_session)

    assert [c.id for c in hierarchy] == [1, 2]


# ==========================================
# SUBCATEGORY: the BELONGS_TO edge as the write path
# ==========================================
# The assignment hangs off an edge, not a property. That is exactly why these tests live
# here: a mock could not show whether the edge comes about, whether it replaces an existing
# one instead of adding to it, and whether the derived levels product group and category
# are right afterwards.


async def create_group_structure(session, subcategoryId: int, categoryName: str) -> None:
    """Creates subcategory, product group and category — without a product on them."""
    await session.run(
        """
        MERGE (s:Subcategory {id: $subcategoryId})
        MERGE (g:ProductGroup {id: $subcategoryId * 10})
        MERGE (c:Category {id: $subcategoryId * 100})
        SET c.name = $categoryName
        MERGE (s)-[:PART_OF]->(g)
        MERGE (g)-[:BELONGS_TO_CATEGORY]->(c)
        """,
        subcategoryId=subcategoryId, categoryName=categoryName,
    )


def _product_with_group(number: str, label: str, subcategoryId: int | None = None) -> ProductCreate:
    """Builds a create request for a product, optionally assigned to a subcategory."""
    return ProductCreate(
        number=number, label=label, shortText=label, unit="pcs",
        listPrice=Decimal("10.00"), minStock=1, targetStock=5,
        subcategoryId=subcategoryId,
    )


@pytest.mark.asyncio
async def test_creating_with_a_subcategory_draws_the_edge(neo4j_session):
    await create_group_structure(neo4j_session, 38, "Hardware")

    created = await ProductRepository.create_product(
        _product_with_group("P-100", "With group", subcategoryId=38), neo4j_session
    )

    assert created.subcategory is not None
    assert created.subcategory.id == 38
    # The projection derives the two upper levels from the same edge.
    assert created.category is not None
    assert created.category.name == "Hardware"


@pytest.mark.asyncio
async def test_creating_without_a_subcategory_stays_unassigned(neo4j_session):
    created = await ProductRepository.create_product(
        _product_with_group("P-101", "Without group"), neo4j_session
    )

    assert created.subcategory is None
    assert created.category is None


@pytest.mark.asyncio
async def test_an_unknown_subcategory_does_not_prevent_the_product(neo4j_session):
    # An unknown id should not make the whole creation fail — the product comes about but
    # stays unassigned. OPTIONAL MATCH rather than MATCH in the query.
    created = await ProductRepository.create_product(
        _product_with_group("P-102", "Unknown group", subcategoryId=99999), neo4j_session
    )

    assert created.subcategory is None


@pytest.mark.asyncio
async def test_the_subcategory_is_not_written_as_a_property_on_creation(neo4j_session):
    # `_product_props` excludes the field from model_dump(). Without the exclusion the value
    # would sit on the node as a loose property that no query reads.
    await create_group_structure(neo4j_session, 38, "Hardware")
    await ProductRepository.create_product(
        _product_with_group("P-103", "Without property", subcategoryId=38), neo4j_session
    )

    result = await neo4j_session.run(
        "MATCH (p:Product {number: 'P-103'}) RETURN p.subcategoryId AS value"
    )
    assert (await result.single())["value"] is None


@pytest.mark.asyncio
async def test_update_replaces_the_previous_subcategory(neo4j_session):
    await create_group_structure(neo4j_session, 38, "Hardware")
    await create_group_structure(neo4j_session, 41, "Accessories")
    await ProductRepository.create_product(
        _product_with_group("P-104", "Moving", subcategoryId=38), neo4j_session
    )

    updated = await ProductRepository.update_product(
        "P-104", ProductUpdate(subcategoryId=41), neo4j_session
    )

    assert updated is not None
    assert updated.subcategory is not None
    assert updated.subcategory.id == 41
    # Exactly ONE edge: without the DELETE before the MERGE they would add up, and `head()`
    # in the projection would then pick an arbitrary one of them.
    result = await neo4j_session.run(
        "MATCH (:Product {number: 'P-104'})-[r:BELONGS_TO]->() RETURN count(r) AS n"
    )
    assert (await result.single())["n"] == 1


@pytest.mark.asyncio
async def test_update_without_a_subcategory_leaves_the_assignment_in_place(neo4j_session):
    await create_group_structure(neo4j_session, 38, "Hardware")
    await ProductRepository.create_product(
        _product_with_group("P-105", "Stays", subcategoryId=38), neo4j_session
    )

    updated = await ProductRepository.update_product(
        "P-105", ProductUpdate(label="New name"), neo4j_session
    )

    assert updated is not None
    assert updated.label == "New name"
    assert updated.subcategory is not None
    assert updated.subcategory.id == 38
