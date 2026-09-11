"""Unit tests of the procurement repository — without a database.

Three things no other test level covers:

* **The conversion.** `_to_supplier` and `_to_reorder_suggestion` are pure functions on a
  dict. The service tests mock the repository away and therefore never run through the
  cent conversion and never through the quantity calculation.
* **The construction of the filter query.** `read_many` is intercepted and the generated
  Cypher inspected along with its parameters. What is checked is the parameter binding,
  not the wording of the query — the tests stay valid no matter what the WHERE clause
  ends up looking like. The two exceptions are marked as such: they check a piece of
  wording that carries meaning.
* **What gets written.** Id assignment, timestamps and the conversion to integer cents
  can be proven on the bound parameters without storing anything.

Not here: whether the MERGE really creates only one edge, and whether the supplier
ranking picks the right row out of real data. Both need a real database.

`_to_reorder_suggestion` expects the raw record of the query with these keys:
`productNumber`, `label`, `totalStock`, `minStock`, `targetStock`, `supplier`,
`purchasePriceCent`, `leadTimeDays`.

The mocks deliver the supplier node under the column name `s` — the way a map projection
`RETURN s{.*}` names it. Whoever renames the column in the query with `AS` has to pull
the key here along with it.
"""

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from neo4j.exceptions import Neo4jError
from neo4j.time import DateTime as Neo4jDateTime

from core.exceptions import BusinessLogicError, DatabaseError, NotFoundError
from domains.procurement.repository_procurement import (
    ReorderSuggestionRepository,
    SupplierRepository,
)
from domains.procurement.schemas_procurement import (
    SupplierCreate,
    SupplierProductCreate,
    SupplierUpdate,
)

REPOSITORY_MODULE = "domains.procurement.repository_procurement"


def _raw_suggestion(**overrides) -> dict:
    """Builds a complete record of the reorder analysis."""
    data: dict = {
        "productNumber": "ACME-2003",
        "label": "Filter Cartridge",
        "totalStock": 12.0,
        "minStock": 20,
        "targetStock": 80,
        "supplier": "Northern Supply GmbH",
        "purchasePriceCent": 1100,
        "leadTimeDays": 5,
    }
    data.update(overrides)
    return data


def _supply_range(**overrides) -> SupplierProductCreate:
    """Builds a valid addition to a supply range."""
    data: dict = {
        "productNumber": "ACME-2003",
        "leadTimeDays": 5,
        "purchasePrice": Decimal("11.00"),
        "isPreferredSupplier": True,
    }
    data.update(overrides)
    return SupplierProductCreate(**data)


def _bound_fields(params: dict) -> dict:
    """Pulls every bound value flat.

    That way it does not matter whether the implementation binds the fields one by one or
    hands them over bundled as `props={...}` — what is checked is WHAT gets bound, not
    how it is packaged.
    """
    flat: dict = {}
    for key, value in params.items():
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


# ==========================================
# Conversion: supplier
# ==========================================

def test_to_supplier_accepts_a_thin_node():
    """Suppliers taken over from a predecessor system carry little more than name and city.

    The remaining master data fields are maintained inside the system and may be missing.
    """
    supplier = SupplierRepository._to_supplier(
        {"id": "S-001", "name": "Northern Supply GmbH", "city": "Hamburg"}
    )

    assert supplier.id == "S-001"
    assert supplier.email is None
    assert supplier.phone is None
    assert supplier.street is None
    assert supplier.country is None
    assert supplier.vatId is None


def test_to_supplier_takes_over_every_field():
    supplier = SupplierRepository._to_supplier(
        {
            "id": "S-001",
            "name": "Northern Supply GmbH",
            "email": "sales@northern-supply.example",
            "phone": "+49 40 1000001",
            "street": "Hafenstrasse 1",
            "city": "Hamburg",
            "country": "DE",
            "vatId": "DE100000001",
        }
    )

    assert supplier.name == "Northern Supply GmbH"
    assert supplier.email == "sales@northern-supply.example"
    assert supplier.phone == "+49 40 1000001"
    assert supplier.street == "Hafenstrasse 1"
    assert supplier.city == "Hamburg"
    assert supplier.country == "DE"
    assert supplier.vatId == "DE100000001"


def test_to_supplier_converts_a_neo4j_datetime_into_a_python_datetime():
    """The driver delivers neo4j.time.DateTime, which is not a subclass of datetime.

    Without the conversion Pydantic rejects the value.
    """
    supplier = SupplierRepository._to_supplier(
        {"id": "S-001", "name": "Northern Supply GmbH",
         "createdAt": Neo4jDateTime(2026, 8, 11, 9, 30, 0)}
    )

    assert supplier.createdAt is not None
    assert supplier.createdAt.year == 2026
    assert supplier.createdAt.hour == 9


def test_to_supplier_accepts_missing_timestamps():
    """Imported suppliers have neither createdAt nor updatedAt.

    An import fills in master data fields; it does not invent timestamps.
    """
    supplier = SupplierRepository._to_supplier({"id": "S-001", "name": "Northern Supply GmbH"})

    assert supplier.createdAt is None
    assert supplier.updatedAt is None


def test_to_supplier_ignores_an_unknown_property():
    """A node from an import may carry properties no current schema knows about."""
    supplier = SupplierRepository._to_supplier(
        {"id": "S-001", "name": "Northern Supply GmbH", "legacyAccountNumber": "42"}
    )

    assert supplier.id == "S-001"


def test_to_supplier_reports_a_missing_id_as_a_database_error():
    """A supplier without an id is a data problem in the graph, not a client error: 500."""
    with pytest.raises(DatabaseError):
        SupplierRepository._to_supplier({"name": "Northern Supply GmbH"})


# ==========================================
# Conversion: reorder suggestion
# ==========================================

def test_to_reorder_suggestion_converts_cents_to_euro():
    suggestion = ReorderSuggestionRepository._to_reorder_suggestion(_raw_suggestion())

    assert suggestion.unitPrice == Decimal("11.00")


def test_to_reorder_suggestion_keeps_a_price_of_zero():
    """0 cents is a recorded price of 0.00 EUR and must not become None like a missing value."""
    suggestion = ReorderSuggestionRepository._to_reorder_suggestion(
        _raw_suggestion(purchasePriceCent=0)
    )

    assert suggestion.unitPrice == Decimal("0")


def test_to_reorder_suggestion_without_a_supplier_leaves_three_fields_empty():
    """A product without a SUPPLIES_PRODUCT edge still appears in the suggestion.

    It is below its minimum stock, and that is the more important information. Only the
    source of supply is missing.
    """
    suggestion = ReorderSuggestionRepository._to_reorder_suggestion(
        _raw_suggestion(
            productNumber="ACME-2008",
            supplier=None,
            purchasePriceCent=None,
            leadTimeDays=None,
        )
    )

    assert suggestion.productNumber == "ACME-2008"
    assert suggestion.supplier is None
    assert suggestion.unitPrice is None
    assert suggestion.leadTimeDays is None


def test_to_reorder_suggestion_quantity_is_the_difference_to_the_target_stock():
    """Ordering up to the minimum stock would mean standing at the boundary again at once."""
    suggestion = ReorderSuggestionRepository._to_reorder_suggestion(
        _raw_suggestion(totalStock=64.0, minStock=100, targetStock=200)
    )

    assert suggestion.currentStock == 64
    assert suggestion.suggestedQuantity == 136


def test_to_reorder_suggestion_quantity_is_clamped_at_zero():
    """Above the target stock the difference would be negative.

    A negative order quantity is not an order but a maintenance error on the bounds — the
    suggestion is clamped at 0 instead of running into the minus.
    """
    suggestion = ReorderSuggestionRepository._to_reorder_suggestion(
        _raw_suggestion(totalStock=1500.0, minStock=2000, targetStock=1000)
    )

    assert suggestion.suggestedQuantity == 0


def test_to_reorder_suggestion_without_a_target_stock_yields_quantity_zero():
    """null - 5 gives null in Cypher, and a suggestion without a quantity is useless.

    The product appears with quantity 0 and stands out as incompletely maintained,
    instead of disappearing from the analysis.
    """
    suggestion = ReorderSuggestionRepository._to_reorder_suggestion(
        _raw_suggestion(targetStock=None)
    )

    assert suggestion.suggestedQuantity == 0


def test_to_reorder_suggestion_without_a_stock_record_reads_as_zero():
    """A product without any StockLevel node arrives as null and is the most urgent case."""
    suggestion = ReorderSuggestionRepository._to_reorder_suggestion(
        _raw_suggestion(totalStock=None, minStock=20, targetStock=80)
    )

    assert suggestion.currentStock == 0.0
    assert suggestion.suggestedQuantity == 80


def test_to_reorder_suggestion_reports_an_unusable_record_as_a_database_error():
    """Without the product number the row cannot be validated.

    That is a server-side data problem, not a client error.
    """
    with pytest.raises(DatabaseError):
        ReorderSuggestionRepository._to_reorder_suggestion(_raw_suggestion(productNumber=None))


# ==========================================
# Filter construction: GET /api/suppliers
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
async def test_get_suppliers_without_a_search_binds_no_parameter(captured_read):
    await SupplierRepository.get_suppliers(AsyncMock())

    assert captured_read["params"] == {}


@pytest.mark.asyncio
async def test_get_suppliers_binds_the_search_term(captured_read):
    await SupplierRepository.get_suppliers(AsyncMock(), search="Northern")

    assert "Northern" in captured_read["params"].values()


@pytest.mark.asyncio
async def test_get_suppliers_does_not_put_the_search_value_into_the_query(captured_read):
    """The value must never land in the query string.

    Otherwise the search would be an open door for Cypher injection.
    """
    malicious = "'; MATCH (n) DETACH DELETE n //"
    await SupplierRepository.get_suppliers(AsyncMock(), search=malicious)

    assert "DETACH DELETE" not in captured_read["query"]
    assert malicious in captured_read["params"].values()


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_value", ["", "   "])
async def test_get_suppliers_an_empty_search_value_produces_no_filter(
    captured_read, empty_value
):
    """A cleared search field in the interface sends an empty string.

    Read as a filter that would be a "contains nothing" — it has to deliver the complete
    list.
    """
    await SupplierRepository.get_suppliers(AsyncMock(), search=empty_value)

    assert captured_read["params"] == {}


@pytest.mark.asyncio
async def test_get_suppliers_an_empty_result_is_an_empty_list(captured_read):
    captured_read["records"] = []

    assert await SupplierRepository.get_suppliers(AsyncMock()) == []


# ==========================================
# GET /api/suppliers/{id}
# ==========================================

@pytest.mark.asyncio
async def test_get_supplier_binds_the_id(captured_read):
    captured_read["record"] = {"s": {"id": "S-001", "name": "Northern Supply GmbH"}}

    await SupplierRepository.get_supplier("S-001", AsyncMock())

    assert "S-001" in captured_read["params"].values()


@pytest.mark.asyncio
async def test_get_supplier_unknown_returns_none_instead_of_an_exception(captured_read):
    """Whether a missing supplier is an error is decided by the service.

    Were it raised here, no caller could handle the case differently.
    """
    captured_read["record"] = None

    assert await SupplierRepository.get_supplier("S-nope", AsyncMock()) is None


# ==========================================
# GET /api/reorder-suggestions — the two rules that live in the query wording
# ==========================================

@pytest.mark.asyncio
async def test_the_ranking_puts_the_preferred_flag_before_the_price(captured_read):
    """The preferred supplier beats the cheaper offer, and the coalesce is not decoration.

    `null` sorts BEFORE `true` on a descending sort in Cypher — an edge without the flag
    set would otherwise displace the actual preferred supplier.
    """
    await ReorderSuggestionRepository.get_reorder_suggestions(AsyncMock())

    query = captured_read["query"]
    flag = query.index("coalesce(sp.isPreferredSupplier, false) DESC")
    price = query.index("sp.purchasePriceCent ASC")
    assert flag < price


@pytest.mark.asyncio
async def test_the_stock_is_joined_optionally(captured_read):
    """A product without any stock record effectively has a stock of 0.

    It is the most urgent case of all — a plain MATCH would drop exactly that one.
    """
    await ReorderSuggestionRepository.get_reorder_suggestions(AsyncMock())

    assert "OPTIONAL MATCH (p)-[:HAS_STOCK]->(s:StockLevel)" in captured_read["query"]


# ==========================================
# Writing: create and update
# ==========================================

@pytest.fixture
def captured_write(monkeypatch):
    """Intercepts the parameters of the next write_single call."""
    record: dict = {"params": {}, "record": None}

    async def fake_write_single(session, query, **params):
        record["query"] = " ".join(query.split())
        record["params"] = params
        record["fields"] = _bound_fields(params)
        return record["record"]

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.write_single", fake_write_single)
    return record


@pytest.mark.asyncio
async def test_create_supplier_assigns_an_id_with_a_prefix(captured_write):
    """The prefix makes it visible in the graph which nodes came in through the API."""
    captured_write["record"] = {"s": {"id": "S-x", "name": "Eastern Trading Ltd"}}

    await SupplierRepository.create_supplier(
        SupplierCreate(name="Eastern Trading Ltd"), AsyncMock()
    )

    assert str(captured_write["fields"]["id"]).startswith("S-")


@pytest.mark.asyncio
async def test_create_supplier_assigns_a_new_id_on_every_call(captured_write):
    """There is a uniqueness constraint on id.

    A reused id would not be a duplicate but a rejected second creation.
    """
    captured_write["record"] = {"s": {"id": "S-x", "name": "Eastern Trading Ltd"}}

    await SupplierRepository.create_supplier(SupplierCreate(name="First"), AsyncMock())
    first = captured_write["fields"]["id"]
    await SupplierRepository.create_supplier(SupplierCreate(name="Second"), AsyncMock())
    second = captured_write["fields"]["id"]

    assert first != second


@pytest.mark.asyncio
async def test_create_supplier_sets_createdat_server_side(captured_write):
    """SupplierCreate has no createdAt — the value can only come from the server."""
    captured_write["record"] = {"s": {"id": "S-x", "name": "Eastern Trading Ltd"}}

    await SupplierRepository.create_supplier(
        SupplierCreate(name="Eastern Trading Ltd"), AsyncMock()
    )

    assert captured_write["fields"].get("createdAt") is not None


@pytest.mark.asyncio
async def test_create_supplier_sets_no_updatedat(captured_write):
    """An updatedAt on creation would claim a change that never happened.

    The field stays empty until the first PATCH arrives.
    """
    captured_write["record"] = {"s": {"id": "S-x", "name": "Eastern Trading Ltd"}}

    await SupplierRepository.create_supplier(
        SupplierCreate(name="Eastern Trading Ltd"), AsyncMock()
    )

    assert captured_write["fields"].get("updatedAt") is None


@pytest.mark.asyncio
async def test_create_supplier_assigns_a_uuid_id(captured_write):
    """The id is a uuid behind its prefix, not a counter.

    A consecutive number would have to read the previous maximum before every write and
    would be stale by the time the write arrives — two concurrent creations would compute
    the same value and one of them would fail for a reason the caller cannot act on.
    """
    captured_write["record"] = {"s": {"id": "S-x", "name": "Eastern Trading Ltd"}}

    await SupplierRepository.create_supplier(
        SupplierCreate(name="Eastern Trading Ltd"), AsyncMock()
    )

    assigned = captured_write["fields"]["id"]
    assert assigned.startswith("S-")
    UUID(assigned.removeprefix("S-"))


@pytest.mark.asyncio
async def test_create_supplier_reads_no_maximum(captured_write):
    """The whole point: the write path no longer queries the existing numbers.

    This test checks a piece of wording, because the absence of that query IS the rule.
    """
    captured_write["record"] = {"s": {"id": "S-x", "name": "Eastern Trading Ltd"}}

    await SupplierRepository.create_supplier(
        SupplierCreate(name="Eastern Trading Ltd"), AsyncMock()
    )

    assert "max(" not in captured_write["query"]
    assert "prefix" not in captured_write["params"]


@pytest.mark.asyncio
async def test_create_supplier_writes_no_second_number(captured_write):
    """A display number beside the id used to be a consecutive `SUP-004`. Once it became a
    uuid it said nothing the id did not already say, so it is gone — and nothing may write
    it back silently."""
    captured_write["record"] = {"s": {"id": "S-x", "name": "Eastern Trading Ltd"}}

    await SupplierRepository.create_supplier(
        SupplierCreate(name="Eastern Trading Ltd"), AsyncMock()
    )

    assert "supplierNumber" not in captured_write["fields"]
    assert "supplierNumber" not in captured_write["query"]


@pytest.mark.asyncio
async def test_two_suppliers_never_get_the_same_id(captured_write):
    captured_write["record"] = {"s": {"id": "S-x", "name": "Eastern Trading Ltd"}}

    await SupplierRepository.create_supplier(SupplierCreate(name="First Ltd"), AsyncMock())
    first = captured_write["fields"]["id"]
    await SupplierRepository.create_supplier(SupplierCreate(name="Second Ltd"), AsyncMock())
    second = captured_write["fields"]["id"]

    assert first != second


@pytest.mark.asyncio
async def test_update_supplier_binds_only_the_fields_that_were_set(captured_write):
    """Fields that were not sent must not be written as null.

    Otherwise a PATCH on the email deletes the entire remaining address.
    """
    captured_write["record"] = {"s": {"id": "S-001", "name": "Northern Supply GmbH"}}

    await SupplierRepository.update_supplier(
        "S-001", SupplierUpdate(email="new@northern-supply.example"), AsyncMock()
    )

    bound = captured_write["fields"]
    assert bound["email"] == "new@northern-supply.example"
    assert "name" not in bound
    assert "street" not in bound
    assert "vatId" not in bound


@pytest.mark.asyncio
async def test_update_supplier_sets_updatedat_server_side(captured_write):
    captured_write["record"] = {"s": {"id": "S-001", "name": "Northern Supply GmbH"}}

    await SupplierRepository.update_supplier(
        "S-001", SupplierUpdate(email="new@northern-supply.example"), AsyncMock()
    )

    assert captured_write["fields"].get("updatedAt") is not None


@pytest.mark.asyncio
async def test_update_supplier_without_a_single_field_is_a_business_error(captured_write):
    """An empty PATCH would otherwise write nothing but a fresh updatedAt.

    The supplier would then look changed although nothing about it changed.
    """
    with pytest.raises(BusinessLogicError):
        await SupplierRepository.update_supplier("S-001", SupplierUpdate(), AsyncMock())

    assert captured_write["params"] == {}


@pytest.mark.asyncio
async def test_update_supplier_unknown_returns_none(captured_write):
    captured_write["record"] = None

    result = await SupplierRepository.update_supplier(
        "S-nope", SupplierUpdate(email="new@northern-supply.example"), AsyncMock()
    )

    assert result is None


# ==========================================
# Supply range: POST /api/suppliers/{id}/products
# ==========================================
# The addition runs through session.execute_write, because the check of both nodes and
# the MERGE of the edge have to sit in one transaction.

@pytest.fixture
def captured_edge():
    """Runs the transaction function and feeds it the results of both queries.

    Unlike the read fixtures this does not merely intercept `execute_write` — the
    transaction function that is handed over actually gets called. It contains the
    existence check and the conversion of the price back into euro; a mock that just
    returns a finished record would skip both, and the tests would be checking themselves
    instead of the code.

    Through `exists` and `record` the test steers what the two queries deliver. They are
    told apart by the keyword MERGE.
    """
    captured: dict = {
        # Result of the existence query — both nodes present.
        "exists": {"supplier": True, "product": True},
        # Result of the MERGE query, in the shape the query returns.
        "record": {
            "supplierId": "S-001",
            "productNumber": "ACME-2003",
            "leadTimeDays": 5,
            "purchasePriceCent": 1100,
            "isPreferredSupplier": True,
            "wasUpdated": False,
        },
    }

    class _Result:
        """Stand-in for the driver's result object — can do exactly `single()`."""

        def __init__(self, data):
            self._data = data

        async def single(self):
            return self._data

    class _Transaction:
        """Stand-in for the tx object inside the managed transaction."""

        async def run(self, query, params=None, **kwargs):
            bound = params if isinstance(params, dict) else (kwargs or {})
            if "MERGE" in query.upper():
                captured["params"] = bound
                captured["fields"] = _bound_fields(bound)
                return _Result(captured["record"])
            return _Result(captured["exists"])

    async def fake_execute_write(transaction_function, params=None, *args, **kwargs):
        return await transaction_function(_Transaction(), params)

    session = AsyncMock()
    session.execute_write = fake_execute_write
    captured["session"] = session
    return captured


@pytest.mark.asyncio
async def test_supply_range_converts_euro_into_integer_cents(captured_edge):
    """8.20 EUR is the classic: int(8.20 * 100) gives 819 via the float detour.

    That is why the rounding has to happen before the integer conversion.
    """
    await SupplierRepository.add_supplied_product(
        "S-001", _supply_range(purchasePrice=Decimal("8.20")), captured_edge["session"]
    )

    assert captured_edge["fields"]["purchasePriceCent"] == 820


@pytest.mark.asyncio
async def test_supply_range_hands_no_decimal_to_the_driver(captured_edge):
    """The Neo4j driver rejects Decimal as a query parameter outright."""
    await SupplierRepository.add_supplied_product(
        "S-001", _supply_range(), captured_edge["session"]
    )

    assert not any(isinstance(value, Decimal) for value in captured_edge["fields"].values())


@pytest.mark.asyncio
async def test_supply_range_returns_the_price_in_euro(captured_edge):
    """What went in as euro has to come back as euro — cents stay internal."""
    result = await SupplierRepository.add_supplied_product(
        "S-001", _supply_range(), captured_edge["session"]
    )

    assert result["purchasePrice"] == Decimal("11.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("updated", [True, False])
async def test_supply_range_passes_the_update_flag_through(captured_edge, updated):
    """The router picks between 200 and 201 on it.

    If the flag falls away, every second call falsely reports "newly created".
    """
    captured_edge["record"] = dict(captured_edge["record"], wasUpdated=updated)

    result = await SupplierRepository.add_supplied_product(
        "S-001", _supply_range(), captured_edge["session"]
    )

    assert result["wasUpdated"] is updated


@pytest.mark.asyncio
async def test_supply_range_names_the_supplier_id_from_the_path(captured_edge):
    result = await SupplierRepository.add_supplied_product(
        "S-001", _supply_range(), captured_edge["session"]
    )

    assert result["supplierId"] == "S-001"
    assert result["productNumber"] == "ACME-2003"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exists", "expected"),
    [
        ({"supplier": True, "product": False}, "ACME-9999"),
        ({"supplier": False, "product": True}, "S-nope"),
    ],
)
async def test_supply_range_without_a_hit_is_not_found(captured_edge, exists, expected):
    """If one of the two nodes is missing, the message has to say WHICH.

    With two keys in the request, "something is missing" is not a usable answer.
    """
    captured_edge["exists"] = exists

    with pytest.raises(NotFoundError) as excinfo:
        await SupplierRepository.add_supplied_product(
            "S-nope", _supply_range(productNumber="ACME-9999"), captured_edge["session"]
        )

    assert expected in str(excinfo.value)
    # The edge must not have been written in the first place.
    assert "params" not in captured_edge


# ==========================================
# Error translation
# ==========================================

@pytest.fixture
def read_error(monkeypatch):
    """Lets every reading query fail with a driver error."""
    async def fake_read(session, query, **params):
        raise Neo4jError("connection lost")

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_many", fake_read)
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_single", fake_read)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda session: SupplierRepository.get_suppliers(session),
        lambda session: SupplierRepository.get_supplier("S-001", session),
        lambda session: ReorderSuggestionRepository.get_reorder_suggestions(session),
    ],
)
async def test_a_read_error_becomes_a_database_error(read_error, call):
    """A Neo4jError passed on would end as an unhandled exception.

    Translated it becomes a DatabaseError, out of which the global handler makes a clean
    500.
    """
    with pytest.raises(DatabaseError):
        await call(AsyncMock())
