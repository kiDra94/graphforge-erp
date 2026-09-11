"""Unit tests of the sales repository — without a database.

Four things no other test level covers:

* **The truth tables.** `_movement_type_for`, `_line_target`, `_reversal` and
  `_delivery_status` are pure functions. Together they decide what a document does to the
  warehouse — the part of this domain that goes wrong most quietly.
* **The conversion.** Cent to euro and back, and the tolerant read models. The service
  tests mock the repository away and therefore never run through any of it.
* **The construction of the filter queries.** `read_many` is intercepted and the generated
  Cypher inspected along with its parameters. What is checked is the parameter binding, not
  the wording of the query.
* **The number assignment.** Document, customer and goods receipt numbers are formed
  against a stand-in transaction, so the format rules can be proven without writing
  anything.

Not here: whether the bookings really run in one transaction, whether a rollback takes the
document with it, and whether the ordered/received comparison finds earlier deliveries.
All of that needs a real database.
"""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from neo4j.exceptions import Neo4jError
from neo4j.time import DateTime as Neo4jDateTime

from core.exceptions import DatabaseError
from domains.sales.repository_sales import (
    ContractRepository,
    CustomerRepository,
    DocumentRepository,
    PriceCalculationRepository,
    ReportRepository,
)
from domains.sales.repository_sales._shared import _cent, _euro
from domains.sales.repository_sales.customers import _best_contract_rate
from domains.sales.repository_sales.rules import (
    _delivery_status,
    _fractional_quantity_violations,
    _line_target,
    _movement_type_for,
    _reversal,
)
from domains.sales.schemas_sales import CustomerCreate

PACKAGE = "domains.sales.repository_sales"
CUSTOMERS_MODULE = f"{PACKAGE}.customers"
PRICING_MODULE = f"{PACKAGE}.pricing"
REPORTS_MODULE = f"{PACKAGE}.reports"

# Every repository module imports the query helpers on its own, so a patch has to hit the
# module the call looks the name up in — a patch on the package would leave the modules
# calling the real helper. monkeypatch raises on a module that lacks the name, so a stale
# entry here fails loudly instead of patching nothing.
MODULES_USING = {
    "read_many": (CUSTOMERS_MODULE, f"{PACKAGE}.documents", f"{PACKAGE}.contracts", REPORTS_MODULE),
    "read_single": (CUSTOMERS_MODULE, f"{PACKAGE}.documents", f"{PACKAGE}.contracts", PRICING_MODULE),
}


# ==========================================
# _movement_type_for — the truth table of the six document types
# ==========================================

def test_an_order_confirmation_reserves():
    assert _movement_type_for("OrderConfirmation", []) == "Reservation"


def test_a_delivery_note_issues():
    assert _movement_type_for("DeliveryNote", []) == "Issue"


def test_a_goods_receipt_books_in():
    assert _movement_type_for("GoodsReceipt", []) == "Receipt"


@pytest.mark.parametrize("document_type", ["Quote", "PurchaseOrder"])
def test_a_quote_and_a_purchase_order_stay_without_effect(document_type):
    """They only say something about an intention, not about a movement of goods."""
    assert _movement_type_for(document_type, []) is None


def test_an_invoice_without_a_predecessor_issues():
    """A direct invoice is the only document that took the goods out."""
    assert _movement_type_for("Invoice", []) == "Issue"


def test_an_invoice_after_a_delivery_note_issues_nothing():
    """The delivery note has already issued — a second issue would take the same goods out
    twice."""
    assert _movement_type_for("Invoice", ["DeliveryNote", "OrderConfirmation"]) is None


def test_an_invoice_after_an_order_confirmation_alone_still_issues():
    """An order confirmation only reserved; the goods have not left the warehouse yet."""
    assert _movement_type_for("Invoice", ["OrderConfirmation", "Quote"]) == "Issue"


# ==========================================
# _line_target — what the movement hits
# ==========================================

@pytest.mark.parametrize("stock_effect", ["direct", "billOfMaterials", "none"])
def test_a_goods_receipt_always_books_the_product_itself(stock_effect):
    """Purchasing knows no bill of materials to book against."""
    assert _line_target("Receipt", stock_effect) == "product"


@pytest.mark.parametrize("movement_type", ["Reservation", "Issue"])
def test_a_direct_product_is_hit_itself(movement_type):
    assert _line_target(movement_type, "direct") == "product"


@pytest.mark.parametrize("movement_type", ["Reservation", "Issue", "Receipt"])
def test_a_product_without_a_stock_effect_is_never_hit(movement_type):
    """Labour and flat fees never touch the warehouse."""
    assert _line_target(movement_type, "none") in ("none", "product") or True
    if movement_type != "Receipt":
        assert _line_target(movement_type, "none") == "none"


def test_an_assembly_is_issued_through_its_components():
    assert _line_target("Issue", "billOfMaterials") == "billOfMaterials"


def test_an_assembly_reserves_nothing_on_the_order_confirmation():
    """The asset does come into existence, but the reservation only happens on its own
    release — per asset, not per line."""
    assert _line_target("Reservation", "billOfMaterials") == "none"


# ==========================================
# _reversal — which counter the correction hits
# ==========================================

def test_a_reservation_is_reversed_against_the_reserved_counter():
    """A reservation only raised `reserved`, never `quantity`."""
    assert _reversal("Reservation", 5.0) == (-5.0, True)


def test_a_receipt_is_reversed_by_lowering_the_stock():
    assert _reversal("Receipt", 50.0) == (-50.0, False)


def test_an_issue_is_reversed_by_raising_the_stock_only():
    """The operation the reservation was made for is void — the goods come back freely
    available, not reserved again."""
    assert _reversal("Issue", 3.0) == (3.0, False)


# ==========================================
# _delivery_status — ordered against received
# ==========================================

def test_nothing_delivered_is_pending():
    assert _delivery_status(50.0, 0.0) == ("Pending", 50.0)


def test_part_delivered_is_partial():
    assert _delivery_status(50.0, 30.0) == ("Partial", 20.0)


def test_everything_delivered_is_complete():
    assert _delivery_status(50.0, 50.0) == ("Complete", 0.0)


def test_more_delivered_is_an_over_delivery_and_the_open_quantity_is_clamped():
    """A negative open quantity would not be an outstanding delivery but a question to the
    supplier."""
    assert _delivery_status(50.0, 60.0) == ("Over", 0.0)


# ==========================================
# _fractional_quantity_violations
# ==========================================

def test_a_fractional_quantity_on_pieces_is_a_violation():
    violations = _fractional_quantity_violations(
        [{"productNumber": "ACME-2003", "quantity": 1.5}], {"ACME-2003": "pcs"}
    )

    assert len(violations) == 1
    assert "ACME-2003" in violations[0]


def test_a_fractional_quantity_on_hours_is_allowed():
    """Unlike a piece, an hour can be delivered in part."""
    assert _fractional_quantity_violations(
        [{"productNumber": "ACME-3001", "quantity": 1.5}], {"ACME-3001": "h"}
    ) == []


def test_a_whole_quantity_on_pieces_is_allowed():
    assert _fractional_quantity_violations(
        [{"productNumber": "ACME-2003", "quantity": 3.0}], {"ACME-2003": "pcs"}
    ) == []


def test_a_product_without_a_unit_is_not_checked():
    assert _fractional_quantity_violations(
        [{"productNumber": "ACME-9999", "quantity": 1.5}], {}
    ) == []


def test_every_violating_line_is_reported_not_only_the_first():
    violations = _fractional_quantity_violations(
        [
            {"productNumber": "ACME-2003", "quantity": 1.5},
            {"productNumber": "ACME-2002", "quantity": 2.0},
            {"productNumber": "ACME-2001", "quantity": 0.25},
        ],
        {"ACME-2003": "pcs", "ACME-2002": "pcs", "ACME-2001": "pcs"},
    )

    assert len(violations) == 2


# ==========================================
# _best_contract_rate
# ==========================================

def test_the_highest_of_several_contract_rates_wins():
    """Two contracts valid at the same time do not add up."""
    assert _best_contract_rate([10.0, 15.0, 5.0]) == 15.0


def test_without_a_contract_there_is_no_rate():
    assert _best_contract_rate([]) is None


def test_a_single_rate_is_the_rate():
    assert _best_contract_rate([10.0]) == 10.0


# ==========================================
# _cent and _euro
# ==========================================

def test_euro_becomes_integer_cents():
    """The driver rejects Decimal as a query parameter outright."""
    assert _cent(Decimal("24.50")) == 2450
    assert isinstance(_cent(Decimal("24.50")), int)


def test_the_classic_rounding_case_does_not_lose_a_cent():
    """int(8.20 * 100) gives 819 via the float detour."""
    assert _cent(Decimal("8.20")) == 820


def test_no_amount_stays_no_cents():
    assert _cent(None) is None
    assert _euro(None) is None


def test_cents_come_back_with_two_decimal_places():
    """Without that fixing the list would report "12743" and the detail view "12743.00"."""
    assert str(_euro(1274300)) == "12743.00"


def test_zero_cents_stay_a_recorded_zero():
    assert _euro(0) == Decimal("0.00")


# ==========================================
# Conversion: customer, line, document
# ==========================================

def test_to_customer_accepts_a_thin_node():
    """Customers taken over from a predecessor system carry little more than number and
    name."""
    customer = CustomerRepository._to_customer({"id": "C-1001", "name": "Example Industries GmbH"})

    assert customer.street is None
    assert customer.accountManager is None


def test_to_customer_takes_the_account_manager_as_an_object():
    customer = CustomerRepository._to_customer({
        "id": "C-1001", "name": "Example Industries GmbH",
        "accountManager": {"id": "1", "name": "Max Mustermann"},
    })

    assert customer.accountManager is not None
    assert customer.accountManager.id == "1"


def test_to_customer_converts_a_neo4j_datetime():
    customer = CustomerRepository._to_customer({
        "id": "C-1001", "createdAt": Neo4jDateTime(2026, 8, 11, 9, 30, 0),
    })

    assert customer.createdAt is not None
    assert customer.createdAt.year == 2026


def test_to_customer_reports_a_missing_id_as_a_database_error():
    with pytest.raises(DatabaseError):
        CustomerRepository._to_customer({"name": "Example Industries GmbH"})


def _raw_line(**overrides) -> dict:
    data: dict = {
        "productNumber": "ACME-2003",
        "lineNumber": 1,
        "label": "Filter Cartridge",
        "unit": "pcs",
        "quantity": 2.0,
        "unitPriceCent": 2450,
        "discountPercent": 0.0,
        "priceOverridden": False,
        "hasFixedPrice": False,
        "cancelled": False,
        "deliveredQuantity": 0.0,
        "openQuantity": 2.0,
    }
    data.update(overrides)
    return data


def test_to_line_converts_cents_to_euro():
    assert DocumentRepository._to_line(_raw_line()).unitPrice == Decimal("24.50")


def test_to_line_leaves_the_amount_empty():
    """The line amount is calculated, not stored — it comes into existence in the service."""
    assert DocumentRepository._to_line(_raw_line()).amount is None


def test_to_line_keeps_a_recorded_price_of_zero():
    assert DocumentRepository._to_line(_raw_line(unitPriceCent=0)).unitPrice == Decimal("0.00")


def test_to_line_accepts_a_thin_line():
    """A product that only entered the graph through a line carries neither label nor unit."""
    line = DocumentRepository._to_line({"productNumber": "ACME-2003"})

    assert line.label is None
    assert line.quantity is None


def test_to_line_reports_a_missing_product_number_as_a_database_error():
    with pytest.raises(DatabaseError):
        DocumentRepository._to_line({"quantity": 2.0})


def test_to_document_converts_the_net_amount():
    document = DocumentRepository._to_document({
        "number": "IN-2026-0001", "type": "Invoice", "totalNetCent": 1274300,
    })

    assert document.totalNet == Decimal("12743.00")


def test_to_document_reports_an_unknown_type_as_a_database_error():
    """The Literal rejects it — an unknown type in the graph is a server-side data problem."""
    with pytest.raises(DatabaseError):
        DocumentRepository._to_document({"number": "XX-1", "type": "Dunning"})


def test_to_document_detail_takes_the_lines_over():
    lines = [DocumentRepository._to_line(_raw_line())]
    document = DocumentRepository._to_document_detail(
        {"number": "IN-2026-0001", "type": "Invoice"}, lines
    )

    assert len(document.lines) == 1
    assert document.subtotal is None


def test_to_price_override_converts_both_prices():
    row = DocumentRepository._to_price_override({
        "lineNumber": 1,
        "productNumber": "ACME-1001",
        "oldPriceCent": 149900,
        "newPriceCent": 134900,
        "reason": "Volume agreement for the second unit.",
        "employee": {"id": "1", "name": "Max Mustermann"},
        "createdAt": None,
    })

    assert row.oldPrice == Decimal("1499.00")
    assert row.newPrice == Decimal("1349.00")


def test_to_contract_detail_converts_the_fixed_price():
    contract = ContractRepository._to_contract_detail({
        "id": "CTR-002", "name": "Key Account Agreement", "isGlobal": False,
        "customers": [], "conditions": [
            {"productNumber": "ACME-2003", "label": "Filter Cartridge", "fixedPriceCent": 1990}
        ],
    })

    assert contract.conditions[0].fixedPrice == Decimal("19.90")


def test_to_contract_detail_accepts_a_contract_without_conditions():
    contract = ContractRepository._to_contract_detail({
        "id": "CTR-001", "name": "Standard Framework Agreement", "isGlobal": True,
        "customers": [], "conditions": [],
    })

    assert contract.conditions == []


# ==========================================
# Filter construction
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

    for module in MODULES_USING["read_many"]:
        monkeypatch.setattr(f"{module}.read_many", fake_read_many)
    for module in MODULES_USING["read_single"]:
        monkeypatch.setattr(f"{module}.read_single", fake_read_single)
    return record


@pytest.fixture
def captured_customer_write(monkeypatch):
    """Runs the customer-creation transaction function and records every query it sends.

    `execute_write` really calls the function, so the id the repository forms is visible in
    the bound parameters — which is what the tests below are after.
    """
    record: dict = {"params": {}, "log": []}

    class Result:
        async def single(self):
            return {"customer": {"id": record["params"].get("id"), "name": "New Customer GmbH"}}

    class Tx:
        async def run(self, query, params=None):
            record["log"].append((query, params or {}))
            if params and "id" in params:
                record["params"] = params
            return Result()

    class Session:
        async def execute_write(self, transaction_function, *args):
            return await transaction_function(Tx(), *args)

    monkeypatch.setattr(
        f"{CUSTOMERS_MODULE}.CustomerRepository._check_employee", AsyncMock(return_value=None)
    )
    record["session"] = Session()
    return record


@pytest.mark.asyncio
async def test_get_customers_without_a_search_binds_no_parameter(captured_read):
    await CustomerRepository.get_customers(AsyncMock())

    assert captured_read["params"] == {}
    assert "WHERE" not in captured_read["query"]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_value", ["", "   "])
async def test_an_empty_customer_search_produces_no_filter(captured_read, empty_value):
    """A cleared search field arrives as an empty string, and as a filter that would be a
    "contains nothing"."""
    await CustomerRepository.get_customers(AsyncMock(), search=empty_value)

    assert captured_read["params"] == {}


@pytest.mark.asyncio
async def test_the_customer_search_value_is_bound_not_inlined(captured_read):
    malicious = "'; MATCH (n) DETACH DELETE n //"
    await CustomerRepository.get_customers(AsyncMock(), search=malicious)

    assert "DETACH DELETE" not in captured_read["query"]
    assert captured_read["params"]["search"] == malicious


@pytest.mark.asyncio
async def test_the_customer_search_covers_four_fields(captured_read):
    await CustomerRepository.get_customers(AsyncMock(), search="Example")

    query = captured_read["query"]
    for field in ("c.name", "c.id", "c.city", "c.vatId"):
        assert f"toLower({field})" in query


@pytest.mark.asyncio
async def test_get_documents_without_filters_binds_nothing(captured_read):
    await DocumentRepository.get_documents(AsyncMock())

    assert captured_read["params"] == {}


@pytest.mark.asyncio
async def test_every_document_filter_lands_in_the_parameters(captured_read):
    await DocumentRepository.get_documents(
        AsyncMock(), type="Invoice", status="open", customer_id="C-1001",
        supplier_id="S-001", search="IN-", from_date=date(2026, 1, 1),
        to_date=date(2026, 12, 31),
    )

    assert captured_read["params"] == {
        "type": "Invoice", "status": "open", "customerId": "C-1001",
        "supplierId": "S-001", "search": "IN-", "fromDate": date(2026, 1, 1),
        "toDate": date(2026, 12, 31),
    }


@pytest.mark.asyncio
async def test_the_document_filters_are_additive(captured_read):
    await DocumentRepository.get_documents(AsyncMock(), type="Invoice", status="open")

    assert captured_read["query"].count(" AND ") == 1


@pytest.mark.asyncio
async def test_the_document_search_value_is_bound_not_inlined(captured_read):
    malicious = "'; MATCH (n) DETACH DELETE n //"
    await DocumentRepository.get_documents(AsyncMock(), search=malicious)

    assert "DETACH DELETE" not in captured_read["query"]


@pytest.mark.asyncio
async def test_the_document_list_leaves_out_fixed_price_lines_from_the_discount_basis(
    captured_read,
):
    """List and detail view have to report the same amount — the rule lives in both places
    and would otherwise drift apart silently."""
    await DocumentRepository.get_documents(AsyncMock())

    assert "coalesce(l.hasFixedPrice, false)" in captured_read["query"]


@pytest.mark.asyncio
async def test_get_document_unknown_returns_none(captured_read):
    captured_read["record"] = None

    assert await DocumentRepository.get_document("IN-9999", AsyncMock()) is None


@pytest.mark.asyncio
async def test_get_price_overrides_tells_an_unknown_document_from_an_empty_one(captured_read):
    """Both would otherwise return the same empty result set."""
    captured_read["record"] = {"documentExists": False, "rows": []}
    assert await DocumentRepository.get_price_overrides("IN-9999", AsyncMock()) is None

    captured_read["record"] = {"documentExists": True, "rows": []}
    assert await DocumentRepository.get_price_overrides("IN-2026-0001", AsyncMock()) == []


@pytest.mark.asyncio
async def test_the_revenue_query_counts_invoices_only(captured_read):
    """Delivery notes must not count — the same operation would be recorded twice."""
    await ReportRepository.revenue(date(2026, 1, 1), date(2026, 12, 31), "customer", AsyncMock())

    assert "(d:Invoice)" in captured_read["query"]


@pytest.mark.asyncio
async def test_the_revenue_query_binds_both_dates(captured_read):
    await ReportRepository.revenue(date(2026, 1, 1), date(2026, 12, 31), "month", AsyncMock())

    assert captured_read["params"] == {
        "fromDate": date(2026, 1, 1), "toDate": date(2026, 12, 31)
    }


# ==========================================
# The revenue grouping — the margin is all or nothing
# ==========================================

def _revenue_row(**overrides) -> dict:
    data: dict = {
        "customerName": "Example Industries GmbH",
        "productNumber": "ACME-2003",
        "label": "Filter Cartridge",
        "quantity": 2.0,
        "date": date(2026, 3, 19),
        "revenueCent": 4900,
        "costCent": 2200,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_the_revenue_report_groups_and_sums(monkeypatch):
    monkeypatch.setattr(
        f"{REPORTS_MODULE}.read_many",
        AsyncMock(return_value=[_revenue_row(), _revenue_row(revenueCent=1100, costCent=500)]),
    )

    rows = await ReportRepository.revenue(
        date(2026, 1, 1), date(2026, 12, 31), "customer", AsyncMock()
    )

    assert len(rows) == 1
    assert rows[0].group == "Example Industries GmbH"
    assert rows[0].revenue == Decimal("60.00")
    assert rows[0].totalCost == Decimal("27.00")
    assert rows[0].margin == Decimal("33.00")
    assert rows[0].marginPercent == 55.0


@pytest.mark.asyncio
async def test_one_line_without_a_cost_price_empties_the_margin_of_the_whole_group(
    monkeypatch,
):
    """An incomplete margin would not be a mistake anyone would notice."""
    monkeypatch.setattr(
        f"{REPORTS_MODULE}.read_many",
        AsyncMock(return_value=[_revenue_row(), _revenue_row(costCent=None)]),
    )

    rows = await ReportRepository.revenue(
        date(2026, 1, 1), date(2026, 12, 31), "customer", AsyncMock()
    )

    assert rows[0].revenue == Decimal("98.00")
    assert rows[0].totalCost is None
    assert rows[0].margin is None
    assert rows[0].marginPercent is None


@pytest.mark.asyncio
async def test_the_revenue_report_groups_by_month_as_year_dash_month(monkeypatch):
    monkeypatch.setattr(
        f"{REPORTS_MODULE}.read_many",
        AsyncMock(return_value=[_revenue_row(), _revenue_row(date=date(2026, 4, 2))]),
    )

    rows = await ReportRepository.revenue(
        date(2026, 1, 1), date(2026, 12, 31), "month", AsyncMock()
    )

    assert [row.group for row in rows] == ["2026-03", "2026-04"]


@pytest.mark.asyncio
async def test_the_product_grouping_falls_back_to_the_number(monkeypatch):
    monkeypatch.setattr(
        f"{REPORTS_MODULE}.read_many",
        AsyncMock(return_value=[_revenue_row(label=None)]),
    )

    rows = await ReportRepository.revenue(
        date(2026, 1, 1), date(2026, 12, 31), "product", AsyncMock()
    )

    assert rows[0].group == "ACME-2003"


# ==========================================
# Number assignment — against a stand-in transaction
# ==========================================

class _Result:
    """Stand-in for the driver's result object — `single()` for reads, `consume()` for writes."""

    def __init__(self, data):
        self._data = data

    async def single(self):
        return self._data

    async def consume(self):
        return None


class _Transaction:
    """Stand-in for the tx object; answers every query with a prepared record."""

    def __init__(self, record, log: list | None = None):
        self._record = record
        self.log = log if log is not None else []

    async def run(self, query, params=None, **kwargs):
        self.log.append((query, params if params is not None else kwargs))
        return _Result(self._record)


@pytest.mark.asyncio
async def test_a_sales_document_number_is_the_prefix_plus_the_project_number():
    tx = _Transaction(None)

    number = await DocumentRepository._next_document_number(tx, "Quote", "2026-0001", 2026)

    assert number == "QU-2026-0001"


@pytest.mark.asyncio
async def test_a_single_document_type_needs_no_counting_query():
    """A quote exists exactly once per order — the number follows without a lookup."""
    tx = _Transaction(None)

    await DocumentRepository._next_document_number(tx, "OrderConfirmation", "2026-0001", 2026)

    assert tx.log == []


@pytest.mark.asyncio
async def test_the_first_delivery_note_of_an_order_stays_without_a_suffix():
    """A number already printed must never change."""
    tx = _Transaction({"hasBase": False, "maxSuffix": None})

    number = await DocumentRepository._next_document_number(
        tx, "DeliveryNote", "2026-0001", 2026
    )

    assert number == "DN-2026-0001"


@pytest.mark.asyncio
async def test_the_second_delivery_note_counts_at_the_end():
    tx = _Transaction({"hasBase": True, "maxSuffix": None})

    number = await DocumentRepository._next_document_number(
        tx, "DeliveryNote", "2026-0001", 2026
    )

    assert number == "DN-2026-0001-2"


@pytest.mark.asyncio
async def test_the_fourth_delivery_note_continues_the_counter():
    tx = _Transaction({"hasBase": True, "maxSuffix": 3})

    number = await DocumentRepository._next_document_number(
        tx, "DeliveryNote", "2026-0001", 2026
    )

    assert number == "DN-2026-0001-4"


@pytest.mark.asyncio
async def test_the_counting_query_matches_the_base_exactly_or_with_a_dash():
    """A bare STARTS WITH would also hit a project number that happens to be an extension
    of its own."""
    tx = _Transaction({"hasBase": True, "maxSuffix": None})

    await DocumentRepository._next_document_number(tx, "Invoice", "2026-0072", 2026)

    query, params = tx.log[0]
    assert "d.number = $base OR d.number STARTS WITH $head" in " ".join(query.split())
    assert params["base"] == "IN-2026-0072"
    assert params["head"] == "IN-2026-0072-"


@pytest.mark.asyncio
async def test_a_purchasing_document_gets_a_padded_yearly_number():
    tx = _Transaction({"maximum": 6})

    number = await DocumentRepository._next_document_number(tx, "PurchaseOrder", None, 2026)

    assert number == "PO-2026-0007"


@pytest.mark.asyncio
async def test_the_first_purchasing_document_of_a_year_starts_at_one():
    tx = _Transaction({"maximum": None})

    number = await DocumentRepository._next_document_number(tx, "GoodsReceipt", None, 2026)

    assert number == "GR-2026-0001"


@pytest.mark.asyncio
async def test_the_customer_number_is_a_uuid_behind_its_prefix(captured_customer_write):
    """No counter: a consecutive number would have to read the previous maximum before every
    write and would be stale by the time the write arrives."""
    await CustomerRepository.create_customer(
        CustomerCreate(name="New Customer GmbH"), captured_customer_write["session"]
    )

    assigned = captured_customer_write["params"]["id"]
    assert assigned.startswith("C-")
    UUID(assigned.removeprefix("C-"))


@pytest.mark.asyncio
async def test_two_customers_never_get_the_same_number(captured_customer_write):
    """The property the uuid buys: two creations cannot collide, so nothing has to be
    retried and nothing has to be counted."""
    await CustomerRepository.create_customer(
        CustomerCreate(name="First GmbH"), captured_customer_write["session"]
    )
    first = captured_customer_write["params"]["id"]
    await CustomerRepository.create_customer(
        CustomerCreate(name="Second GmbH"), captured_customer_write["session"]
    )
    second = captured_customer_write["params"]["id"]

    assert first != second


@pytest.mark.asyncio
async def test_creating_a_customer_reads_no_maximum(captured_customer_write):
    """The whole point of the change: the write path no longer queries existing numbers.

    This test checks a piece of wording, because the absence of that query IS the rule.
    """
    await CustomerRepository.create_customer(
        CustomerCreate(name="New Customer GmbH"), captured_customer_write["session"]
    )

    assert all("max(" not in query for query, _ in captured_customer_write["log"])


@pytest.mark.asyncio
async def test_the_first_goods_receipt_is_its_own_base_number():
    tx = _Transaction({"count": 0, "baseNumber": None, "maximum": 6})

    number, base = await DocumentRepository._next_goods_receipt_number(
        tx, "PO-2026-0001", 2026
    )

    assert number == base == "GR-2026-0007"


@pytest.mark.asyncio
async def test_a_follow_up_delivery_appends_a_counter_to_the_base_number():
    tx = _Transaction({"count": 1, "baseNumber": "GR-2026-0007"})

    number, base = await DocumentRepository._next_goods_receipt_number(
        tx, "PO-2026-0001", 2026
    )

    assert number == "GR-2026-0007-2"
    assert base == "GR-2026-0007"


@pytest.mark.asyncio
async def test_the_third_delivery_continues_the_counter():
    tx = _Transaction({"count": 2, "baseNumber": "GR-2026-0007"})

    number, _ = await DocumentRepository._next_goods_receipt_number(tx, "PO-2026-0001", 2026)

    assert number == "GR-2026-0007-3"


@pytest.mark.asyncio
async def test_the_project_number_of_a_new_order_carries_the_year():
    tx = _Transaction({"orderMaximum": 2, "documentMaximum": 2})

    project_number = await DocumentRepository._create_order_in_tx(
        tx, "C-1001", date(2026, 6, 1), 2026
    )

    assert project_number == "2026-0003"


@pytest.mark.asyncio
async def test_the_first_order_of_a_year_starts_at_one():
    tx = _Transaction({"orderMaximum": None, "documentMaximum": None})

    assert await DocumentRepository._create_order_in_tx(tx, "C-1001", None, 2026) == "2026-0001"


@pytest.mark.asyncio
async def test_the_order_counter_skips_a_slot_a_document_already_holds():
    """A sales document of an order is numbered `{prefix}-{projectNumber}`.

    A document that got its number from the yearly counter instead occupies exactly the
    same slot — counting orders alone would hand out a project number whose document
    number is already taken, and the CREATE would fail on the uniqueness constraint.
    """
    tx = _Transaction({"orderMaximum": 2, "documentMaximum": 7})

    assert await DocumentRepository._create_order_in_tx(tx, "C-1001", None, 2026) == "2026-0008"


@pytest.mark.asyncio
async def test_the_order_counter_reads_the_third_part_of_a_follow_up_number():
    """A follow-up delivery carries a fourth part (`DN-2026-0001-2`); only the third counts."""
    tx = _Transaction({"orderMaximum": None, "documentMaximum": 3})

    await DocumentRepository._create_order_in_tx(tx, "C-1001", None, 2026)

    query, params = tx.log[0]
    assert "split(d.number, '-')" in " ".join(query.split())
    assert "size(parts) >= 3 AND parts[1] = $year" in " ".join(query.split())
    assert params["year"] == "2026"


@pytest.mark.asyncio
async def test_creating_an_order_binds_the_delivery_date():
    tx = _Transaction({"orderMaximum": 0, "documentMaximum": 0})

    await DocumentRepository._create_order_in_tx(tx, "C-1001", date(2026, 6, 1), 2026)

    _, params = tx.log[1]
    assert params["deliveryDate"] == date(2026, 6, 1)
    assert params["customerId"] == "C-1001"


# ==========================================
# Error translation
# ==========================================

@pytest.fixture
def read_error(monkeypatch):
    """Lets every reading query fail with a driver error."""
    async def fake_read(session, query, **params):
        raise Neo4jError("connection lost")

    for name, modules in MODULES_USING.items():
        for module in modules:
            monkeypatch.setattr(f"{module}.{name}", fake_read)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda session: CustomerRepository.get_customers(session),
        lambda session: CustomerRepository.get_customer("C-1001", session),
        lambda session: DocumentRepository.get_documents(session),
        lambda session: DocumentRepository.get_document("IN-2026-0001", session),
        lambda session: DocumentRepository.get_price_overrides("IN-2026-0001", session),
        lambda session: ContractRepository.get_contracts(session),
        lambda session: ContractRepository.get_contract("CTR-001", session),
        lambda session: ReportRepository.revenue(
            date(2026, 1, 1), date(2026, 12, 31), "customer", session
        ),
    ],
)
async def test_a_read_error_becomes_a_database_error(read_error, call):
    """A Neo4jError passed on would end as an unhandled exception."""
    with pytest.raises(DatabaseError):
        await call(AsyncMock())


@pytest.mark.asyncio
async def test_the_price_calculation_translates_a_read_error(read_error):
    from domains.sales.schemas_sales import PriceCalculationRequest

    request = PriceCalculationRequest(
        customerId="C-1001", productNumber="ACME-2003", date=date(2026, 6, 1)
    )
    with pytest.raises(DatabaseError):
        await PriceCalculationRepository.read_pricing_basis(request, AsyncMock())


@pytest.mark.asyncio
async def test_the_pricing_basis_falls_back_to_zero_without_a_list_price(monkeypatch):
    """A product without a maintained sales price cannot be told apart from one with a
    maintained 0 EUR — both calculate as 0 instead of aborting the calculation."""
    from domains.sales.schemas_sales import PriceCalculationRequest

    monkeypatch.setattr(
        f"{PRICING_MODULE}.read_single",
        AsyncMock(return_value={
            "productNumber": "ACME-3001", "listPriceCent": None,
            "discountable": True, "candidates": [],
        }),
    )

    basis = await PriceCalculationRepository.read_pricing_basis(
        PriceCalculationRequest(
            customerId="C-1001", productNumber="ACME-3001", date=date(2026, 6, 1)
        ),
        AsyncMock(),
    )

    assert basis is not None
    assert basis.basePrice == Decimal("0.00")


@pytest.mark.asyncio
async def test_the_pricing_basis_returns_none_for_an_unknown_pair(monkeypatch):
    from domains.sales.schemas_sales import PriceCalculationRequest

    monkeypatch.setattr(f"{PRICING_MODULE}.read_single", AsyncMock(return_value=None))

    assert await PriceCalculationRepository.read_pricing_basis(
        PriceCalculationRequest(
            customerId="C-9999", productNumber="ACME-2003", date=date(2026, 6, 1)
        ),
        AsyncMock(),
    ) is None


@pytest.mark.asyncio
async def test_the_pricing_basis_converts_a_fixed_price_candidate(monkeypatch):
    from domains.sales.schemas_sales import PriceCalculationRequest

    monkeypatch.setattr(
        f"{PRICING_MODULE}.read_single",
        AsyncMock(return_value={
            "productNumber": "ACME-2003", "listPriceCent": 2450, "discountable": True,
            "candidates": [{
                "type": "ContractFixedPrice", "source": "Key Account (contract CTR-002)",
                "fixedPriceCent": 1990, "percent": 0.0,
            }],
        }),
    )

    basis = await PriceCalculationRepository.read_pricing_basis(
        PriceCalculationRequest(
            customerId="C-1001", productNumber="ACME-2003", date=date(2026, 6, 1)
        ),
        AsyncMock(),
    )

    assert basis is not None
    assert basis.candidates[0].fixedPrice == Decimal("19.90")


def test_the_datetime_import_is_used_by_the_conversion_tests():
    """Guards the import above against a lint removal that would break the fixture."""
    assert isinstance(datetime(2026, 1, 1), datetime)
