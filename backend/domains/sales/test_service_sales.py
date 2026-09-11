"""Unit tests of the sales services — against mocks, without a database.

Two halves:

* **The calculation.** `_line_amount`, `_totals` and `_best_line_price` are pure functions
  and the centre of gravity of this layer. Every amount printed on a document comes out of
  them, and none of it is stored anywhere.
* **The pass-through.** The translation of a `None` from the repository into a
  `NotFoundError`, that filters reach the repository unchanged, that exceptions are not
  swallowed, and that exactly one event per business operation is sent.

Not here: the stock effect, the predecessor chain and the ordered/received comparison. They
need the stored state, run inside the write transaction and therefore sit in the repository.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from core.exceptions import BusinessLogicError, DatabaseError, NotFoundError
from domains.sales.repository_sales import DiscountCandidate, PricingBasis
from domains.sales.schemas_sales import (
    CancelledLine,
    ContractAssignment,
    ContractDetail,
    Customer,
    CustomerCreate,
    CustomerUpdate,
    DocumentDetail,
    DocumentLine,
    DocumentUpdate,
    GoodsReceiptCreate,
    GoodsReceiptLineCreate,
    GoodsReceiptLineResult,
    GoodsReceiptResult,
    PriceCalculationRequest,
)
from domains.sales.service_sales import (
    ContractService,
    CustomerService,
    DocumentService,
    PriceCalculationService,
    ReportService,
    _best_line_price,
    _line_amount,
    _totals,
    _with_amounts,
)

SERVICE_MODULE = "domains.sales.service_sales"
MANAGER_MODULE = "core.websocket"


def _line(**overrides) -> DocumentLine:
    data: dict = {
        "productNumber": "ACME-2003",
        "lineNumber": 1,
        "quantity": 2.0,
        "unitPrice": Decimal("24.50"),
        "discountPercent": 0.0,
    }
    data.update(overrides)
    return DocumentLine(**data)


def _document(**overrides) -> DocumentDetail:
    data: dict = {"number": "IN-2026-0001", "type": "Invoice", "lines": [_line()]}
    data.update(overrides)
    return DocumentDetail(**data)


def _customer(**overrides) -> Customer:
    data: dict = {"id": "C-1001", "name": "Example Industries GmbH"}
    data.update(overrides)
    return Customer(**data)


def _goods_receipt() -> GoodsReceiptCreate:
    return GoodsReceiptCreate(
        deliveryNoteNumber="DN-NS-99871",
        lines=[GoodsReceiptLineCreate(lineNumber=1, quantity=50.0)],
    )


# ==========================================
# _line_amount — the only place a line amount comes into existence
# ==========================================

def test_a_line_amount_is_quantity_times_price():
    assert _line_amount(2.0, Decimal("24.50"), 0.0) == Decimal("49.00")


def test_a_line_discount_lowers_the_line_amount():
    assert _line_amount(10.0, Decimal("24.50"), 10.0) == Decimal("220.50")


def test_the_line_amount_is_rounded_to_cents():
    """The printed document shows an amount per line, and their sum has to be the subtotal.

    Rounding only at the end makes the paper differ from the system by a cent or two.
    """
    assert _line_amount(3.0, Decimal("0.335"), 0.0) == Decimal("1.01")


def test_a_float_quantity_does_not_go_through_a_binary_approximation():
    """`Decimal(0.1)` would be the binary approximation and would shift the amount by cents
    on large quantities."""
    assert _line_amount(0.1, Decimal("100.00"), 0.0) == Decimal("10.00")


def test_a_missing_quantity_yields_a_line_amount_of_zero():
    assert _line_amount(None, Decimal("24.50"), 0.0) == Decimal("0.00")


def test_a_missing_price_yields_a_line_amount_of_zero():
    assert _line_amount(2.0, None, 0.0) == Decimal("0.00")


def test_a_missing_discount_counts_as_no_discount():
    """A line from the old stock without a maintained discount rate is not a line without
    an amount."""
    assert _line_amount(2.0, Decimal("24.50"), None) == Decimal("49.00")


# ==========================================
# _totals — from the inside out
# ==========================================

def test_the_subtotal_is_the_sum_of_the_line_amounts():
    totals = _totals([_line(), _line(quantity=1.0)], None, None)

    assert totals.subtotal == Decimal("73.50")


def test_a_document_without_lines_reports_two_decimal_places():
    """`sum([])` would be an int and the response would carry "0" instead of "0.00"."""
    totals = _totals([], None, None)

    assert totals.subtotal == Decimal("0.00")
    assert str(totals.totalNet) == "0.00"


def test_the_order_discount_lowers_the_net_amount():
    totals = _totals([_line()], 10.0, None)

    assert totals.subtotal == Decimal("49.00")
    assert totals.orderDiscountAmount == Decimal("4.90")
    assert totals.totalNet == Decimal("44.10")


def test_the_tax_is_calculated_on_the_net_amount():
    totals = _totals([_line()], None, 20.0)

    assert totals.totalNet == Decimal("49.00")
    assert totals.totalGross == Decimal("58.80")


def test_a_missing_order_discount_and_tax_rate_count_as_zero():
    """Without that handling every total of an imported document would be empty."""
    totals = _totals([_line()], None, None)

    assert totals.orderDiscountAmount == Decimal("0.00")
    assert totals.totalGross == Decimal("49.00")


def test_a_cancelled_line_no_longer_counts_towards_the_totals():
    """A cancelled line is no longer part of what the customer owes.

    It stays visible on the document; only the total passes over it.
    """
    totals = _totals([_line(), _line(lineNumber=2, cancelled=True)], None, None)

    assert totals.subtotal == Decimal("49.00")


def test_a_fixed_price_line_counts_towards_the_subtotal():
    totals = _totals([_line(hasFixedPrice=True)], None, None)

    assert totals.subtotal == Decimal("49.00")


def test_a_fixed_price_line_is_not_part_of_the_order_discount_basis():
    """The fixed price takes precedence — it is not discounted a second time.

    Line 1 carries a fixed price of 49.00 and stays untouched, line 2 carries 49.00 and
    takes the full 10 %.
    """
    totals = _totals(
        [_line(hasFixedPrice=True), _line(lineNumber=2)], 10.0, None
    )

    assert totals.subtotal == Decimal("98.00")
    assert totals.orderDiscountAmount == Decimal("4.90")
    assert totals.totalNet == Decimal("93.10")


def test_a_document_of_nothing_but_fixed_prices_gets_no_order_discount():
    totals = _totals([_line(hasFixedPrice=True)], 25.0, None)

    assert totals.orderDiscountAmount == Decimal("0.00")
    assert totals.totalNet == totals.subtotal


def test_a_cancelled_fixed_price_line_falls_out_of_both_sums():
    totals = _totals(
        [_line(hasFixedPrice=True, cancelled=True), _line(lineNumber=2)], 10.0, None
    )

    assert totals.subtotal == Decimal("49.00")
    assert totals.orderDiscountAmount == Decimal("4.90")


# ==========================================
# _with_amounts — the calculated fields land on the document
# ==========================================

def test_with_amounts_fills_the_line_amount_and_the_four_totals():
    document = _with_amounts(_document(orderDiscountPercent=10.0, taxPercent=20.0))

    assert document.lines[0].amount == Decimal("49.00")
    assert document.subtotal == Decimal("49.00")
    assert document.orderDiscountAmount == Decimal("4.90")
    assert document.totalNet == Decimal("44.10")
    assert document.totalGross == Decimal("52.92")


def test_with_amounts_does_not_trust_a_stored_line_amount():
    """In the read model `amount` may be empty, and a total silently too low would be worse
    than an error."""
    document = _with_amounts(_document(lines=[_line(amount=Decimal("999.99"))]))

    assert document.lines[0].amount == Decimal("49.00")
    assert document.subtotal == Decimal("49.00")


# ==========================================
# _best_line_price — the cheapest candidate wins, not the sum
# ==========================================

def _candidate(**overrides) -> DiscountCandidate:
    data: dict = {"type": "ContractDiscount", "source": "Standard (contract CTR-001)", "percent": 10.0}
    data.update(overrides)
    return DiscountCandidate(**data)


def test_without_candidates_the_base_price_applies():
    """A customer without a contract and without a relevant campaign is the normal case."""
    price, discount = _best_line_price(Decimal("100.00"), True, [])

    assert price == Decimal("100.00")
    assert discount is None


def test_a_product_that_cannot_be_discounted_beats_every_candidate():
    price, discount = _best_line_price(Decimal("100.00"), False, [_candidate(percent=50.0)])

    assert price == Decimal("100.00")
    assert discount is None


def test_a_single_candidate_lowers_the_price():
    price, discount = _best_line_price(Decimal("100.00"), True, [_candidate()])

    assert price == Decimal("90.00")
    assert discount is not None
    assert discount.amount == Decimal("10.00")
    assert discount.type == "ContractDiscount"


def test_the_cheapest_of_several_candidates_wins_and_they_do_not_add_up():
    """Two discounts harmless on their own could otherwise add up to a price below the cost
    price without anyone noticing."""
    price, discount = _best_line_price(
        Decimal("100.00"), True,
        [_candidate(percent=10.0), _candidate(type="GroupDiscount", percent=25.0)],
    )

    assert price == Decimal("75.00")
    assert discount is not None
    assert discount.type == "GroupDiscount"


def test_a_fixed_price_competes_on_its_absolute_value():
    price, discount = _best_line_price(
        Decimal("100.00"), True,
        [_candidate(percent=10.0), _candidate(type="ContractFixedPrice",
                                              fixedPrice=Decimal("70.00"), percent=0.0)],
    )

    assert price == Decimal("70.00")
    assert discount is not None
    assert discount.type == "ContractFixedPrice"


def test_a_fixed_price_above_the_percentage_candidate_loses():
    """The best price is the cheapest price, not the fixed price by rank."""
    price, discount = _best_line_price(
        Decimal("100.00"), True,
        [_candidate(percent=40.0), _candidate(type="ContractFixedPrice",
                                              fixedPrice=Decimal("90.00"), percent=0.0)],
    )

    assert price == Decimal("60.00")
    assert discount is not None
    assert discount.type == "ContractDiscount"


def test_on_a_tie_the_first_candidate_wins():
    price, discount = _best_line_price(
        Decimal("100.00"), True,
        [_candidate(type="ContractDiscount", percent=10.0),
         _candidate(type="GroupDiscount", percent=10.0)],
    )

    assert price == Decimal("90.00")
    assert discount is not None
    assert discount.type == "ContractDiscount"


def test_the_discount_amount_is_the_difference_to_the_base_price():
    _, discount = _best_line_price(Decimal("24.50"), True, [_candidate(percent=10.0)])

    assert discount is not None
    assert discount.amount == Decimal("2.45")


# ==========================================
# CustomerService
# ==========================================

@pytest.mark.asyncio
async def test_get_customer_translates_none_into_not_found(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.CustomerRepository.get_customer", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError) as excinfo:
        await CustomerService.get_customer("C-9999", AsyncMock())

    assert "C-9999" in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_customers_passes_the_search_on_as_a_keyword(monkeypatch):
    repo = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.CustomerRepository.get_customers", repo)
    session = AsyncMock()

    await CustomerService.get_customers(session, search="Example")

    repo.assert_awaited_once_with(session, search="Example")


@pytest.mark.asyncio
async def test_an_empty_customer_list_is_not_an_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.CustomerRepository.get_customers", AsyncMock(return_value=[])
    )

    assert await CustomerService.get_customers(AsyncMock(), search="nothing") == []


@pytest.mark.asyncio
async def test_create_customer_sends_an_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.CustomerRepository.create_customer",
        AsyncMock(return_value=_customer()),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await CustomerService.create_customer(
        CustomerCreate(name="Example Industries GmbH"), AsyncMock()
    )

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "customer", "trigger": "customer_created",
        "reference": "C-1001", "ids": ["C-1001"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_update_customer_does_not_pre_filter_the_model(monkeypatch):
    """Which fields were set is known only to the model itself (exclude_unset)."""
    repo = AsyncMock(return_value=_customer())
    monkeypatch.setattr(f"{SERVICE_MODULE}.CustomerRepository.update_customer", repo)
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    change = CustomerUpdate(city="1010 Vienna")
    session = AsyncMock()
    await CustomerService.update_customer("C-1001", change, session)

    repo.assert_awaited_once_with("C-1001", change, session)
    call = repo.await_args
    assert call is not None
    assert call.args[1].model_dump(exclude_unset=True) == {"city": "1010 Vienna"}


@pytest.mark.asyncio
async def test_update_customer_sends_no_event_for_an_unknown_customer(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.CustomerRepository.update_customer", AsyncMock(return_value=None)
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(NotFoundError):
        await CustomerService.update_customer("C-9999", CustomerUpdate(name="X"), AsyncMock())

    send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_contract_passes_only_the_id_on(monkeypatch):
    """The repository takes the id, not the request model — the wrapper is HTTP shape."""
    repo = AsyncMock(return_value=_customer())
    monkeypatch.setattr(f"{SERVICE_MODULE}.CustomerRepository.assign_contract", repo)
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    session = AsyncMock()
    await CustomerService.assign_contract(
        "C-1001", ContractAssignment(contractId="CTR-002"), session
    )

    repo.assert_awaited_once_with("C-1001", "CTR-002", session)


@pytest.mark.asyncio
async def test_assign_contract_lets_a_business_error_through(monkeypatch):
    """A global contract is rejected by the repository — the service must not swallow it."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.CustomerRepository.assign_contract",
        AsyncMock(side_effect=BusinessLogicError("contract is global")),
    )

    with pytest.raises(BusinessLogicError):
        await CustomerService.assign_contract(
            "C-1001", ContractAssignment(contractId="CTR-001"), AsyncMock()
        )


# ==========================================
# DocumentService
# ==========================================

@pytest.mark.asyncio
async def test_get_document_translates_none_into_not_found(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.get_document", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await DocumentService.get_document("IN-9999", AsyncMock())


@pytest.mark.asyncio
async def test_get_document_fills_in_the_amounts(monkeypatch):
    """The repository delivers only what is stored — the totals come from this layer."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.get_document",
        AsyncMock(return_value=_document()),
    )

    document = await DocumentService.get_document("IN-2026-0001", AsyncMock())

    assert document.totalNet == Decimal("49.00")


@pytest.mark.asyncio
async def test_get_documents_passes_every_filter_on_by_name(monkeypatch):
    repo = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.DocumentRepository.get_documents", repo)
    session = AsyncMock()

    await DocumentService.get_documents(
        session, type="Invoice", status="open", customer_id="C-1001",
        supplier_id=None, search="IN-", from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
    )

    repo.assert_awaited_once_with(
        session, type="Invoice", status="open", customer_id="C-1001",
        supplier_id=None, search="IN-", from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
    )


@pytest.mark.asyncio
async def test_get_price_overrides_translates_none_into_not_found(monkeypatch):
    """`None` stands for an unknown document, an empty list for a document without changes."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.get_price_overrides",
        AsyncMock(return_value=None),
    )

    with pytest.raises(NotFoundError):
        await DocumentService.get_price_overrides("IN-9999", AsyncMock())


@pytest.mark.asyncio
async def test_a_document_without_price_overrides_is_not_an_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.get_price_overrides", AsyncMock(return_value=[])
    )

    assert await DocumentService.get_price_overrides("IN-2026-0001", AsyncMock()) == []


@pytest.mark.asyncio
async def test_create_document_takes_the_employee_from_the_token(monkeypatch):
    """Who created the document comes from the auth token, not from the request body."""
    repo = AsyncMock(return_value=_document())
    monkeypatch.setattr(f"{SERVICE_MODULE}.DocumentRepository.create_document", repo)
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", AsyncMock())

    data = object()
    session = AsyncMock()
    await DocumentService.create_document(data, "1", session)  # type: ignore[arg-type]

    repo.assert_awaited_once_with(data, "1", session)


@pytest.mark.asyncio
async def test_creating_an_invoice_sends_a_document_and_a_stock_event(monkeypatch):
    """An invoice can issue goods, so the stock views have to reload as well."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.create_document",
        AsyncMock(return_value=_document()),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await DocumentService.create_document(object(), "1", AsyncMock())  # type: ignore[arg-type]

    assert send_event.await_count == 2
    entities = [call.args[0]["entity"] for call in send_event.await_args_list]
    assert entities == ["document", "stock"]


@pytest.mark.asyncio
async def test_creating_a_quote_sends_no_stock_event(monkeypatch):
    """A quote says something about an intention, not about a movement of goods."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.create_document",
        AsyncMock(return_value=_document(number="QU-2026-0004", type="Quote")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await DocumentService.create_document(object(), "1", AsyncMock())  # type: ignore[arg-type]

    assert send_event.await_count == 1
    assert send_event.await_args_list[0].args[0]["entity"] == "document"


@pytest.mark.asyncio
async def test_a_failed_document_creation_sends_no_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.create_document",
        AsyncMock(side_effect=BusinessLogicError("stock would go negative")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(BusinessLogicError):
        await DocumentService.create_document(object(), "1", AsyncMock())  # type: ignore[arg-type]

    send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_full_cancellation_reports_every_product(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.update_document",
        AsyncMock(return_value=_document(
            lines=[_line(), _line(lineNumber=2, productNumber="ACME-2002")]
        )),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await DocumentService.update_document(
        "IN-2026-0001", DocumentUpdate(status="cancelled"), "1", AsyncMock()
    )

    stock_event = send_event.await_args_list[1].args[0]
    assert stock_event["trigger"] == "document_cancelled"
    assert sorted(stock_event["ids"]) == ["ACME-2002", "ACME-2003"]


@pytest.mark.asyncio
async def test_a_partial_cancellation_reports_only_the_lines_named(monkeypatch):
    """The websocket payload stays product-based, so the line numbers are translated back."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.update_document",
        AsyncMock(return_value=_document(
            lines=[_line(), _line(lineNumber=2, productNumber="ACME-2002")]
        )),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await DocumentService.update_document(
        "IN-2026-0001",
        DocumentUpdate(
            status="partiallyCancelled", cancelledLines=[CancelledLine(lineNumber=2)]
        ),
        "1", AsyncMock(),
    )

    stock_event = send_event.await_args_list[1].args[0]
    assert stock_event["ids"] == ["ACME-2002"]


@pytest.mark.asyncio
async def test_an_ordinary_update_sends_no_stock_event(monkeypatch):
    """Nothing was booked, so no stock view has to reload."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.update_document",
        AsyncMock(return_value=_document()),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await DocumentService.update_document(
        "IN-2026-0001", DocumentUpdate(taxPercent=20.0), "1", AsyncMock()
    )

    assert send_event.await_count == 1


@pytest.mark.asyncio
async def test_update_document_translates_none_into_not_found(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.update_document", AsyncMock(return_value=None)
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(NotFoundError):
        await DocumentService.update_document(
            "IN-9999", DocumentUpdate(taxPercent=20.0), "1", AsyncMock()
        )

    send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_goods_receipt_sends_one_stock_event(monkeypatch):
    result = GoodsReceiptResult(
        purchaseOrderNumber="PO-2026-0001",
        purchaseOrderStatus="open",
        goodsReceiptNumber="GR-2026-0002",
        deliveryNoteNumber="DN-NS-99871",
        lines=[GoodsReceiptLineResult(
            productNumber="ACME-2003", orderedQuantity=50.0, receivedQuantity=50.0,
            openQuantity=0.0, deliveryStatus="Complete",
        )],
    )
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.post_goods_receipt",
        AsyncMock(return_value=result),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    await DocumentService.post_goods_receipt(
        "PO-2026-0001", _goods_receipt(), "5", AsyncMock()
    )

    send_event.assert_awaited_once_with({
        "type": "event", "entity": "stock", "trigger": "goods_receipt",
        "reference": "GR-2026-0002", "ids": ["ACME-2003"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_a_failed_goods_receipt_sends_no_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.DocumentRepository.post_goods_receipt",
        AsyncMock(side_effect=BusinessLogicError("not a purchase order")),
    )
    send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", send_event)

    with pytest.raises(BusinessLogicError):
        await DocumentService.post_goods_receipt(
            "QU-2026-0001", _goods_receipt(), "5", AsyncMock()
        )

    send_event.assert_not_awaited()


# ==========================================
# ContractService and PriceCalculationService
# ==========================================

@pytest.mark.asyncio
async def test_get_contract_translates_none_into_not_found(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ContractRepository.get_contract", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError) as excinfo:
        await ContractService.get_contract("CTR-999", AsyncMock())

    assert "CTR-999" in str(excinfo.value)


@pytest.mark.asyncio
async def test_add_condition_translates_none_into_not_found(monkeypatch):
    """`None` is the unknown contract; an unknown product raises in the repository."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ContractRepository.add_condition", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await ContractService.add_condition("CTR-999", object(), AsyncMock())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_add_condition_returns_the_contract(monkeypatch):
    expected = ContractDetail(id="CTR-002", name="Key Account Agreement")
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.ContractRepository.add_condition", AsyncMock(return_value=expected)
    )

    assert await ContractService.add_condition("CTR-002", object(), AsyncMock()) is expected  # type: ignore[arg-type]


def _request() -> PriceCalculationRequest:
    return PriceCalculationRequest(
        customerId="C-1001", productNumber="ACME-2003", date=date(2026, 6, 1)
    )


@pytest.mark.asyncio
async def test_the_price_calculation_translates_none_into_not_found(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.PriceCalculationRepository.read_pricing_basis",
        AsyncMock(return_value=None),
    )

    with pytest.raises(NotFoundError):
        await PriceCalculationService.calculate(_request(), AsyncMock())


@pytest.mark.asyncio
async def test_the_price_calculation_resolves_the_best_price(monkeypatch):
    basis = PricingBasis(
        productNumber="ACME-2003",
        basePrice=Decimal("24.50"),
        discountable=True,
        candidates=[
            DiscountCandidate(type="ContractDiscount", source="Standard (contract CTR-001)",
                              percent=10.0),
            DiscountCandidate(type="ContractFixedPrice", source="Key Account (contract CTR-002)",
                              fixedPrice=Decimal("19.90")),
        ],
    )
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.PriceCalculationRepository.read_pricing_basis",
        AsyncMock(return_value=basis),
    )

    response = await PriceCalculationService.calculate(_request(), AsyncMock())

    assert response.basePrice == Decimal("24.50")
    assert response.finalPrice == Decimal("19.90")
    assert response.discount is not None
    assert response.discount.type == "ContractFixedPrice"


@pytest.mark.asyncio
async def test_the_price_calculation_reports_no_discount_without_candidates(monkeypatch):
    basis = PricingBasis(
        productNumber="ACME-2003", basePrice=Decimal("24.50"), discountable=True, candidates=[]
    )
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.PriceCalculationRepository.read_pricing_basis",
        AsyncMock(return_value=basis),
    )

    response = await PriceCalculationService.calculate(_request(), AsyncMock())

    assert response.finalPrice == response.basePrice
    assert response.discount is None


# ==========================================
# ReportService
# ==========================================

@pytest.mark.asyncio
async def test_the_revenue_report_passes_its_arguments_on(monkeypatch):
    repo = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE_MODULE}.ReportRepository.revenue", repo)
    session = AsyncMock()

    await ReportService.revenue(date(2026, 1, 1), date(2026, 12, 31), "customer", session)

    repo.assert_awaited_once_with(date(2026, 1, 1), date(2026, 12, 31), "customer", session)


# ==========================================
# Transparency for errors
# ==========================================

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repository_path", "call"),
    [
        (
            "CustomerRepository.get_customers",
            lambda session: CustomerService.get_customers(session),
        ),
        (
            "DocumentRepository.get_documents",
            lambda session: DocumentService.get_documents(session),
        ),
        (
            "ContractRepository.get_contracts",
            lambda session: ContractService.get_contracts(session),
        ),
        (
            "ReportRepository.revenue",
            lambda session: ReportService.revenue(
                date(2026, 1, 1), date(2026, 12, 31), "month", session
            ),
        ),
    ],
)
async def test_a_database_error_is_not_swallowed(monkeypatch, repository_path, call):
    """A caught DatabaseError would pass as an empty result.

    The client would see "no data" instead of "the query failed".
    """
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.{repository_path}",
        AsyncMock(side_effect=DatabaseError("connection lost")),
    )

    with pytest.raises(DatabaseError):
        await call(AsyncMock())
