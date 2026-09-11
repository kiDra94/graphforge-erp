"""Unit tests of the catalog repository — without a database.

Two areas are covered here that no integration test reaches as precisely: the cent/euro
conversion in `_to_product`, and the WHERE clause `get_products` assembles. The latter is
the one place in this domain where a query string is built dynamically, which makes it the
one place where a value from the client could end up as text instead of as a parameter.
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from core.exceptions import DatabaseError
from domains.catalog.repository_catalog import (
    BomRepository,
    ProductRepository,
    product_projection,
)
from domains.catalog.schemas_catalog import ProductCreate

REPOSITORY_MODULE = "domains.catalog.repository_catalog"


def _projected(**overrides) -> dict:
    """A product as the map projection delivers it — computed fields already present."""
    props = {
        "number": "ACME-2003",
        "label": "Filter Cartridge",
        "unit": "pcs",
        "listPriceCent": 2450,
        "type": "Part",
        "hasBom": False,
        "active": True,
        "stockEffect": "direct",
        "category": None,
        "productGroup": None,
        "subcategory": None,
        "suppliers": [],
    }
    props.update(overrides)
    return props


# ==========================================
# _to_product — cent/euro conversion
# ==========================================

def test_to_product_converts_cents_to_euro():
    product = ProductRepository._to_product(_projected(listPriceCent=2450))

    assert product.listPrice == Decimal("24.50")


def test_to_product_without_a_cent_property_yields_none_instead_of_a_keyerror():
    props = _projected()
    del props["listPriceCent"]

    assert ProductRepository._to_product(props).listPrice is None


def test_to_product_accepts_a_price_of_zero():
    """0 cents is a real price, not a missing one — it must not become None."""
    assert ProductRepository._to_product(_projected(listPriceCent=0)).listPrice == Decimal("0")


def test_to_product_ignores_a_stale_euro_property():
    """A leftover euro property from before the cent migration must not win.

    Two competing storage formats for the same value would make a migration that never ran
    invisible. A missing price is visible, a quietly wrong one is not.
    """
    props = _projected(listPrice=99.99)
    del props["listPriceCent"]

    assert ProductRepository._to_product(props).listPrice is None


def test_to_product_converts_the_labor_rate():
    product = ProductRepository._to_product(_projected(laborRateCent=9500))

    assert product.laborRate == Decimal("95.00")


def test_to_product_converts_the_cost_price():
    product = ProductRepository._to_product(_projected(costPriceCent=1100))

    assert product.costPrice == Decimal("11.00")


def test_to_product_converts_the_purchase_price_of_every_supplier():
    product = ProductRepository._to_product(_projected(suppliers=[
        {"supplierId": "S-001", "leadTimeDays": 5, "purchasePriceCent": 1100, "isPreferredSupplier": True},
        {"supplierId": "S-002", "leadTimeDays": 3, "purchasePriceCent": None, "isPreferredSupplier": False},
    ]))

    assert product.suppliers[0].purchasePrice == Decimal("11.00")
    assert product.suppliers[1].purchasePrice is None


# ==========================================
# _to_product — tolerance towards thin nodes
# ==========================================

def test_to_product_accepts_a_thin_node():
    """Imported nodes carry neither unit nor stock limits — the read model must not break."""
    product = ProductRepository._to_product({
        "number": "ACME-9999", "label": "Thin node", "type": "Part", "hasBom": False,
    })

    assert product.unit is None
    assert product.minStock is None
    assert product.active is True


def test_to_product_converts_a_neo4j_datetime():
    class _Stub:
        def to_native(self):
            return datetime(2026, 1, 15, 10, 15, 30)

    product = ProductRepository._to_product(_projected(createdAt=_Stub()))

    assert product.createdAt == datetime(2026, 1, 15, 10, 15, 30)


# ==========================================
# _to_product — broken node becomes a DatabaseError
# ==========================================

def test_to_product_turns_a_validation_error_into_a_database_error():
    """A node the read model cannot map is a server-side problem: 500, not 422."""
    with pytest.raises(DatabaseError):
        ProductRepository._to_product(_projected(type="NoSuchType"))


def test_to_product_reports_a_missing_required_field_as_a_database_error():
    with pytest.raises(DatabaseError):
        ProductRepository._to_product({"number": "ACME-2003"})  # label missing


# ==========================================
# ProductCreate — validation at the system boundary
# ==========================================

def test_product_create_still_requires_every_mandatory_field():
    with pytest.raises(ValidationError):
        ProductCreate(number="ACME-1", label="X")  # type: ignore[call-arg]


def test_product_create_rejects_a_negative_stock_level():
    with pytest.raises(ValidationError):
        ProductCreate(
            number="ACME-1", label="X", unit="pcs", minStock=-1, targetStock=10,
            listPrice=Decimal("1.00"),
        )


def test_product_create_requires_a_number():
    with pytest.raises(ValidationError):
        ProductCreate(  # type: ignore[call-arg]
            label="X", unit="pcs", minStock=1, targetStock=10, listPrice=Decimal("1.00"),
        )


# ==========================================
# product_projection — the shared query fragment
# ==========================================

def test_projection_substitutes_the_variable():
    assert product_projection("child").startswith("child{")
    assert "(child)-[:CONTAINS]->()" in product_projection("child")


def test_projection_puts_the_computed_fields_after_the_star():
    """An explicit key wins over `.*` only when it comes later — the order is the point."""
    projection = product_projection()

    assert projection.index(".*") < projection.index("hasBom:")


# ==========================================
# get_products — dynamically assembled WHERE clause
# ==========================================

@pytest.fixture
def captured_query(monkeypatch):
    """Captures the query and parameters `get_products` hands to read_many."""
    captured: dict = {}

    async def fake_read_many(session, query, **params):
        captured["query"] = query
        captured["params"] = params
        return []

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_many", fake_read_many)
    return captured


@pytest.mark.asyncio
async def test_without_filters_only_the_active_comparison_remains(captured_query):
    await ProductRepository.get_products(AsyncMock())

    assert "coalesce(p.active, true) = $active" in captured_query["query"]
    assert captured_query["params"] == {"active": True}


@pytest.mark.asyncio
async def test_the_search_term_is_bound_as_a_parameter_not_inlined(captured_query):
    """The one place in this domain where a client value could reach the query as text."""
    await ProductRepository.get_products(AsyncMock(), search="'; MATCH (n) DETACH DELETE n //")

    assert "DETACH DELETE" not in captured_query["query"]
    assert captured_query["params"]["search"] == "'; MATCH (n) DETACH DELETE n //"


@pytest.mark.asyncio
async def test_the_search_condition_is_parenthesised(captured_query):
    """Without the parentheses the AND of the other filters would bind tighter than this OR."""
    await ProductRepository.get_products(AsyncMock(), search="filter")

    assert "(toLower(p.number) CONTAINS toLower($search)" in captured_query["query"]
    assert "toLower($search))" in captured_query["query"]


@pytest.mark.asyncio
async def test_an_empty_search_produces_no_filter(captured_query):
    await ProductRepository.get_products(AsyncMock(), search="")

    assert "$search" not in captured_query["query"]


@pytest.mark.parametrize(
    ("type_filter", "expected_fragment"),
    [("Assembly", "p:Assembly"), ("Part", "p:Part")],
)
@pytest.mark.asyncio
async def test_the_type_filter_sets_the_matching_label(
    captured_query, type_filter, expected_fragment
):
    """Labels cannot be parameterised — one of two fixed fragments is chosen instead."""
    await ProductRepository.get_products(AsyncMock(), type=type_filter)

    assert expected_fragment in captured_query["query"]


@pytest.mark.asyncio
async def test_every_filter_is_joined_with_and(captured_query):
    await ProductRepository.get_products(AsyncMock(), search="filter", type="Part", active=False)

    assert captured_query["query"].count(" AND ") == 2
    assert captured_query["params"] == {"search": "filter", "active": False}


# ==========================================
# get_bom — tree building from a flat edge list
# ==========================================

@pytest.mark.asyncio
async def test_get_bom_nests_the_edges(monkeypatch):
    """A flat edge list from Cypher has to come back as a tree."""
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_many",
        AsyncMock(return_value=[
            {"parentNumber": "ACME-1000", "quantity": 1.0,
             "childProps": _projected(number="ACME-2004", label="Pump Unit")},
            {"parentNumber": "ACME-2004", "quantity": 2.0,
             "childProps": _projected(number="ACME-2002", label="O-Ring")},
        ]),
    )

    tree = await BomRepository.get_bom("ACME-1000", -1, AsyncMock())

    assert len(tree) == 1
    assert tree[0]["component"].number == "ACME-2004"
    assert tree[0]["subComponents"][0]["component"].number == "ACME-2002"


@pytest.mark.asyncio
async def test_get_bom_without_edges_yields_an_empty_tree(monkeypatch):
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_many", AsyncMock(return_value=[]))

    assert await BomRepository.get_bom("ACME-2001", 1, AsyncMock()) == []


@pytest.mark.asyncio
async def test_get_bom_survives_a_cycle_in_the_data(monkeypatch):
    """A self-containing assembly would otherwise recurse until the stack runs out."""
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_many",
        AsyncMock(return_value=[
            {"parentNumber": "A", "quantity": 1.0, "childProps": _projected(number="B")},
            {"parentNumber": "B", "quantity": 1.0, "childProps": _projected(number="A")},
        ]),
    )

    tree = await BomRepository.get_bom("A", -1, AsyncMock())

    assert tree[0]["component"].number == "B"
    assert tree[0]["subComponents"][0]["component"].number == "A"
    assert tree[0]["subComponents"][0]["subComponents"] == []


# ==========================================
# delete_product — success hangs off the deleted node
# ==========================================

@pytest.mark.asyncio
async def test_delete_product_reports_success_via_the_deleted_nodes(monkeypatch):
    summary = AsyncMock()
    summary.counters.nodes_deleted = 1
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.write_summary", AsyncMock(return_value=summary))

    assert await ProductRepository.delete_product("ACME-2003", AsyncMock()) is True


@pytest.mark.asyncio
async def test_delete_product_without_a_hit_returns_false(monkeypatch):
    summary = AsyncMock()
    summary.counters.nodes_deleted = 0
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.write_summary", AsyncMock(return_value=summary))

    assert await ProductRepository.delete_product("nope", AsyncMock()) is False
