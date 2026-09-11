"""API tests of the sales routes — the domain with the most decisions at the HTTP level.

Two of them cannot be tested anywhere but here:

* **The role depends on the document type**, not on the endpoint. A quote belongs to sales,
  an invoice to accounting — and the type is only known once the body has been parsed, so
  there is no plain `Depends` for it.
* **The field masking** strips purchase prices, margins and contract rates out of an answer
  that is otherwise complete. It sits in the router, because it depends on the caller and not
  on the data.
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from core.exceptions import BusinessLogicError, NotFoundError
from domains.sales.schemas_sales import (
    Condition,
    Contract,
    ContractDetail,
    Customer,
    Discount,
    Document,
    DocumentDetail,
    DocumentLine,
    PriceCalculationResponse,
    PriceOverrideLine,
    RevenueLine,
)

CUSTOMER_SERVICE = "domains.sales.router_sales.CustomerService"
DOCUMENT_SERVICE = "domains.sales.router_sales.DocumentService"
CONTRACT_SERVICE = "domains.sales.router_sales.ContractService"
REPORT_SERVICE = "domains.sales.router_sales.ReportService"
PRICING_SERVICE = "domains.sales.router_sales.PriceCalculationService"


def _quote() -> dict:
    return {
        "type": "Quote", "customerId": "C-1001", "taxPercent": 20.0,
        "assetPurpose": "newAsset",
        "lines": [{"productNumber": "ACME-2001", "quantity": 2.0, "unitPrice": "1.00"}],
    }


def _purchase_order_detail() -> DocumentDetail:
    return DocumentDetail(
        number="PO-2026-0001", type="PurchaseOrder", subtotal=Decimal("100.00"),
        totalNet=Decimal("100.00"), totalGross=Decimal("120.00"),
        lines=[DocumentLine(
            lineNumber=1, productNumber="ACME-2001", quantity=100.0,
            unitPrice=Decimal("0.40"), amount=Decimal("40.00"),
        )],
    )


# ==========================================
# Customers
# ==========================================

@pytest.mark.asyncio
async def test_the_customer_list_passes_the_search_on(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{CUSTOMER_SERVICE}.get_customers", service)

    await async_client.get("/api/customers?search=vienna", headers=auth("Sales"))

    assert service.await_args is not None
    assert service.await_args.kwargs["search"] == "vienna"


@pytest.mark.asyncio
async def test_creating_a_customer_needs_a_role(async_client, auth):
    response = await async_client.post(
        "/api/customers", json={"name": "New Customer GmbH"}, headers=auth("Warehouse")
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_sales_may_create_a_customer(async_client, auth, monkeypatch):
    """The counterpart of the role test: without it, a green suite would not tell a working
    gate from a route that rejects everybody."""
    monkeypatch.setattr(
        f"{CUSTOMER_SERVICE}.create_customer",
        AsyncMock(return_value=Customer(id="C-new", name="New Customer GmbH")),
    )

    response = await async_client.post(
        "/api/customers", json={"name": "New Customer GmbH"}, headers=auth("Sales")
    )

    assert response.status_code == 201
    assert response.json()["id"] == "C-new"


@pytest.mark.asyncio
async def test_updating_a_customer_passes_the_id_on(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=Customer(id="C-1001", name="Renamed GmbH"))
    monkeypatch.setattr(f"{CUSTOMER_SERVICE}.update_customer", service)

    response = await async_client.patch(
        "/api/customers/C-1001", json={"name": "Renamed GmbH"}, headers=auth("Purchasing")
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed GmbH"
    assert service.await_args is not None
    assert service.await_args.args[0] == "C-1001"


@pytest.mark.asyncio
async def test_assigning_a_global_contract_answers_400(async_client, auth, monkeypatch):
    """A global contract applies to everybody; assigning it to one customer would be a
    contradiction the repository rejects."""
    monkeypatch.setattr(
        f"{CUSTOMER_SERVICE}.assign_contract",
        AsyncMock(side_effect=BusinessLogicError("contract is global")),
    )

    response = await async_client.post(
        "/api/customers/C-1001/contract", json={"contractId": "CT-GLOBAL"},
        headers=auth("BackOffice"),
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_an_unknown_customer_answers_404(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{CUSTOMER_SERVICE}.get_customer", AsyncMock(side_effect=NotFoundError("no such customer"))
    )

    response = await async_client.get("/api/customers/C-9999", headers=auth("Sales"))

    assert response.status_code == 404


# ==========================================
# The role per document type
# ==========================================

@pytest.mark.asyncio
async def test_sales_may_create_a_quote(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{DOCUMENT_SERVICE}.create_document",
        AsyncMock(return_value=DocumentDetail(number="QU-2026-0001", type="Quote")),
    )

    response = await async_client.post("/api/documents", json=_quote(), headers=auth("Sales"))

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_sales_may_not_create_an_invoice(async_client, auth):
    """The type decides, not the endpoint — and the message names the role that is missing."""
    invoice = _quote() | {"type": "Invoice", "assetPurpose": None}

    response = await async_client.post("/api/documents", json=invoice, headers=auth("Sales"))

    assert response.status_code == 403
    assert "Accounting" in response.json()["message"]


@pytest.mark.asyncio
async def test_each_document_type_names_its_own_role(async_client, auth):
    """One table, five answers. A second place holding the same mapping would be the bug
    this test exists to prevent."""
    expected = {
        "OrderConfirmation": "BackOffice",
        "DeliveryNote": "BackOffice",
        "Invoice": "Accounting",
        "PurchaseOrder": "Purchasing",
    }
    for document_type, role in expected.items():
        body = _quote() | {"type": document_type, "assetPurpose": None}
        if document_type == "PurchaseOrder":
            body = body | {"supplierId": "S-001"}
            del body["customerId"]
        if document_type in ("OrderConfirmation", "DeliveryNote"):
            body = body | {"deliveryDate": "2026-11-02"}

        response = await async_client.post("/api/documents", json=body, headers=auth("Sales"))

        assert response.status_code == 403
        assert role in response.json()["message"]


@pytest.mark.asyncio
async def test_a_quote_without_an_asset_purpose_answers_422(async_client, auth):
    """Sales has to say whether the quote is about a new asset or about spare parts for an
    existing one. A forgotten value would have to be guessed later."""
    body = _quote()
    del body["assetPurpose"]

    response = await async_client.post("/api/documents", json=body, headers=auth("Sales"))

    assert response.status_code == 422
    assert "assetPurpose" in response.json()["message"]


@pytest.mark.asyncio
async def test_a_document_with_both_partners_answers_422(async_client, auth):
    """Exactly one business partner. Both would leave it open who the document is for."""
    body = _quote() | {"supplierId": "S-001"}

    response = await async_client.post("/api/documents", json=body, headers=auth("Sales"))

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_the_document_list_passes_every_filter_on(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{DOCUMENT_SERVICE}.get_documents", service)

    await async_client.get(
        "/api/documents?type=Quote&status=open&customerId=C-1001"
        "&fromDate=2026-01-01&toDate=2026-12-31",
        headers=auth("Sales"),
    )

    assert service.await_args is not None
    kwargs = service.await_args.kwargs
    # camel case at the API boundary, snake case towards the service — the router
    # translates, and a mix-up here would hand the service an unfiltered call.
    assert kwargs["type"] == "Quote"
    assert kwargs["status"] == "open"
    assert kwargs["customer_id"] == "C-1001"
    assert str(kwargs["from_date"]) == "2026-01-01"


@pytest.mark.asyncio
async def test_an_unknown_document_status_answers_422(async_client, auth):
    response = await async_client.get("/api/documents?status=almost-done", headers=auth("Sales"))

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_change_is_checked_against_the_stored_type(async_client, auth, monkeypatch):
    """On PATCH the body carries no type — the role is checked against the document as it is
    stored. Sales passes the outer gate, but an invoice belongs to accounting."""
    monkeypatch.setattr(
        f"{DOCUMENT_SERVICE}.get_document",
        AsyncMock(return_value=DocumentDetail(number="IN-2026-0001", type="Invoice")),
    )
    update = AsyncMock()
    monkeypatch.setattr(f"{DOCUMENT_SERVICE}.update_document", update)

    response = await async_client.patch(
        "/api/documents/IN-2026-0001", json={"status": "cancelled"}, headers=auth("Sales")
    )

    assert response.status_code == 403
    assert "Accounting" in response.json()["message"]
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_change_takes_the_employee_from_the_token(async_client, auth, monkeypatch):
    """Who changed a document ends up on it — and that comes from `sub`, never from the body."""
    monkeypatch.setattr(
        f"{DOCUMENT_SERVICE}.get_document",
        AsyncMock(return_value=DocumentDetail(number="IN-2026-0001", type="Invoice")),
    )
    update = AsyncMock(return_value=DocumentDetail(
        number="IN-2026-0001", type="Invoice", status="cancelled",
    ))
    monkeypatch.setattr(f"{DOCUMENT_SERVICE}.update_document", update)

    response = await async_client.patch(
        "/api/documents/IN-2026-0001", json={"status": "cancelled"},
        headers=auth("Accounting", sub="7"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert update.await_args is not None
    assert update.await_args.args[0] == "IN-2026-0001"
    assert update.await_args.args[2] == "7"


@pytest.mark.asyncio
async def test_the_price_overrides_of_a_document_are_listed(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{DOCUMENT_SERVICE}.get_price_overrides",
        AsyncMock(return_value=[PriceOverrideLine(
            lineNumber=1, productNumber="ACME-2003", oldPrice=Decimal("24.50"),
            newPrice=Decimal("19.90"), reason="Loyal customer",
        )]),
    )

    response = await async_client.get(
        "/api/documents/QU-2026-0003/price-overrides", headers=auth("Sales")
    )

    assert response.status_code == 200
    assert response.json()[0]["newPrice"] == "19.90"
    assert response.json()[0]["reason"] == "Loyal customer"


# ==========================================
# Field masking
# ==========================================

@pytest.mark.asyncio
async def test_the_purchase_prices_of_a_purchase_order_are_masked(async_client, auth, monkeypatch):
    """A purchase order is at heart a list of purchase prices. For a role without that
    permission the document stays readable, but the amounts are gone — per line as well as
    aggregated."""
    monkeypatch.setattr(
        f"{DOCUMENT_SERVICE}.get_document", AsyncMock(return_value=_purchase_order_detail())
    )

    response = await async_client.get("/api/documents/PO-2026-0001", headers=auth("Sales"))

    body = response.json()
    assert body["number"] == "PO-2026-0001"
    assert body["totalNet"] is None
    assert body["lines"][0]["unitPrice"] is None


@pytest.mark.asyncio
async def test_purchasing_sees_the_purchase_prices(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{DOCUMENT_SERVICE}.get_document", AsyncMock(return_value=_purchase_order_detail())
    )

    response = await async_client.get("/api/documents/PO-2026-0001", headers=auth("Purchasing"))

    assert response.json()["totalNet"] == "100.00"


@pytest.mark.asyncio
async def test_a_sales_document_keeps_its_amounts(async_client, auth, monkeypatch):
    """Only purchase orders are masked — every other type carries sales prices, and those
    are what sales works with."""
    monkeypatch.setattr(
        f"{DOCUMENT_SERVICE}.get_documents",
        AsyncMock(return_value=[Document(
            number="QU-2026-0001", type="Quote", totalNet=Decimal("100.00")
        )]),
    )

    response = await async_client.get("/api/documents", headers=auth("Sales"))

    assert response.json()[0]["totalNet"] == "100.00"


@pytest.mark.asyncio
async def test_the_document_list_masks_the_purchase_orders_only(async_client, auth, monkeypatch):
    """The list carries the net amount per document. For Sales the purchase order loses it,
    the quote next to it keeps its own — and Purchasing sees both."""
    # Fresh objects per call: the masking mutates what the service hands over (see
    # test_the_contract_rate_is_masked).
    monkeypatch.setattr(
        f"{DOCUMENT_SERVICE}.get_documents",
        AsyncMock(side_effect=lambda *_, **__: [
            Document(number="PO-2026-0001", type="PurchaseOrder", totalNet=Decimal("100.00")),
            Document(number="QU-2026-0001", type="Quote", totalNet=Decimal("250.00")),
        ]),
    )

    hidden = (await async_client.get("/api/documents", headers=auth("Sales"))).json()
    visible = (await async_client.get("/api/documents", headers=auth("Purchasing"))).json()

    assert hidden[0]["totalNet"] is None
    assert hidden[1]["totalNet"] == "250.00"
    assert visible[0]["totalNet"] == "100.00"


@pytest.mark.asyncio
async def test_the_contract_rate_is_masked(async_client, auth, monkeypatch):
    """BackOffice maintains the fixed prices and contract discounts. Sales negotiates
    without them, so the rate stays hidden there — deliberately not the other way round."""
    # A fresh object per call, not one shared instance: the masking helpers strip the
    # fields on the objects the service handed over. Per request those are freshly built
    # from the query result, so mutating them is safe there — a mock reusing one instance
    # across both calls would carry the first answer's masking into the second and make the
    # test lie.
    monkeypatch.setattr(
        f"{CONTRACT_SERVICE}.get_contracts",
        AsyncMock(side_effect=lambda *_: [Contract(id="CT-1", discountPercent=10.0)]),
    )

    hidden = await async_client.get("/api/contracts", headers=auth("Sales"))
    visible = await async_client.get("/api/contracts", headers=auth("BackOffice"))

    assert hidden.json()[0]["discountPercent"] is None
    assert visible.json()[0]["discountPercent"] == 10.0


@pytest.mark.asyncio
async def test_the_fixed_prices_of_a_contract_are_masked(async_client, auth, monkeypatch):
    """The detail view carries the fixed price per product on top of the rate. Both have to
    go for Sales — the products themselves stay, so sales still knows a condition exists."""
    monkeypatch.setattr(
        f"{CONTRACT_SERVICE}.get_contract",
        AsyncMock(side_effect=lambda *_: ContractDetail(
            id="CT-1", discountPercent=10.0,
            conditions=[Condition(productNumber="ACME-2003", fixedPrice=Decimal("19.90"))],
        )),
    )

    hidden = (await async_client.get("/api/contracts/CT-1", headers=auth("Sales"))).json()
    visible = (await async_client.get("/api/contracts/CT-1", headers=auth("BackOffice"))).json()

    assert hidden["discountPercent"] is None
    assert hidden["conditions"][0]["fixedPrice"] is None
    assert hidden["conditions"][0]["productNumber"] == "ACME-2003"
    assert visible["discountPercent"] == 10.0
    assert visible["conditions"][0]["fixedPrice"] == "19.90"


@pytest.mark.asyncio
async def test_the_margin_of_the_revenue_report_is_masked(async_client, auth, monkeypatch):
    """Revenue is one thing, margin another: whoever may not see cost prices may not see the
    margin computed from them either."""
    monkeypatch.setattr(
        f"{REPORT_SERVICE}.revenue",
        AsyncMock(return_value=[RevenueLine(
            group="C-1001", revenue=Decimal("2134.50"), totalCost=Decimal("1200.00"),
            margin=Decimal("934.50"), marginPercent=43.8,
        )]),
    )

    response = await async_client.get(
        "/api/reports/revenue?fromDate=2026-01-01&toDate=2026-12-31&groupBy=customer",
        headers=auth("Sales"),
    )

    body = response.json()[0]
    assert body["revenue"] == "2134.50"
    assert body["margin"] is None
    assert body["totalCost"] is None


@pytest.mark.asyncio
async def test_purchasing_sees_the_margin(async_client, auth, monkeypatch):
    """The counterpart of the masking test: without it, a green suite would not tell a
    working mask from a route that hides the margin from everybody."""
    monkeypatch.setattr(
        f"{REPORT_SERVICE}.revenue",
        AsyncMock(return_value=[RevenueLine(
            group="C-1001", revenue=Decimal("2134.50"), totalCost=Decimal("1200.00"),
            margin=Decimal("934.50"), marginPercent=43.8,
        )]),
    )

    response = await async_client.get(
        "/api/reports/revenue?fromDate=2026-01-01&toDate=2026-12-31&groupBy=customer",
        headers=auth("Purchasing"),
    )

    assert response.json()[0]["margin"] == "934.50"


@pytest.mark.asyncio
async def test_the_revenue_report_demands_its_parameters(async_client, auth):
    """All three are mandatory: a report over an unbounded period without a grouping would be
    a different question than the one this endpoint answers."""
    response = await async_client.get("/api/reports/revenue", headers=auth("Sales"))

    assert response.status_code == 422


# ==========================================
# Goods receipt
# ==========================================

@pytest.mark.asyncio
async def test_the_goods_receipt_is_open_to_the_warehouse(async_client, auth, monkeypatch):
    """Whoever unloads the lorry books it — and the check against what was ordered happens
    in the same transaction."""
    from domains.sales.schemas_sales import GoodsReceiptResult

    monkeypatch.setattr(
        f"{DOCUMENT_SERVICE}.post_goods_receipt",
        AsyncMock(return_value=GoodsReceiptResult(
            purchaseOrderNumber="PO-2026-0001", purchaseOrderStatus="completed",
            goodsReceiptNumber="GR-2026-0001", deliveryNoteNumber="DN-SUP-0001",
        )),
    )

    response = await async_client.post(
        "/api/documents/PO-2026-0001/goods-receipt",
        json={"deliveryNoteNumber": "DN-SUP-0001", "lines": [{"lineNumber": 1, "quantity": 80.0}]},
        headers=auth("Warehouse"),
    )

    assert response.status_code in (200, 201)
    assert response.json()["goodsReceiptNumber"] == "GR-2026-0001"


@pytest.mark.asyncio
async def test_a_goods_receipt_without_a_delivery_note_number_answers_422(async_client, auth):
    """The number is what makes the call idempotent. Without it a second click would book
    the same delivery twice."""
    response = await async_client.post(
        "/api/documents/PO-2026-0001/goods-receipt",
        json={"lines": [{"lineNumber": 1, "quantity": 80.0}]},
        headers=auth("Warehouse"),
    )

    assert response.status_code == 422


# ==========================================
# Contracts
# ==========================================

def _contract_body() -> dict:
    return {"name": "Frame Agreement 2027", "validFrom": "2027-01-01",
            "validTo": "2027-12-31", "isGlobal": False}


@pytest.mark.asyncio
async def test_the_back_office_creates_a_contract(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{CONTRACT_SERVICE}.create_contract",
        AsyncMock(return_value=ContractDetail(id="CT-9", name="Frame Agreement 2027")),
    )

    response = await async_client.post(
        "/api/contracts", json=_contract_body(), headers=auth("BackOffice")
    )

    assert response.status_code == 201
    assert response.json()["id"] == "CT-9"


@pytest.mark.asyncio
async def test_sales_may_not_create_a_contract(async_client, auth, monkeypatch):
    """Sales negotiates within contracts, it does not write them — a contract sets the prices
    sales would otherwise grant itself."""
    service = AsyncMock()
    monkeypatch.setattr(f"{CONTRACT_SERVICE}.create_contract", service)

    response = await async_client.post(
        "/api/contracts", json=_contract_body(), headers=auth("Sales")
    )

    assert response.status_code == 403
    service.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_back_office_stores_a_fixed_price(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=ContractDetail(
        id="CT-1", conditions=[Condition(productNumber="ACME-2003", fixedPrice=Decimal("19.90"))],
    ))
    monkeypatch.setattr(f"{CONTRACT_SERVICE}.add_condition", service)

    response = await async_client.post(
        "/api/contracts/CT-1/conditions",
        json={"productNumber": "ACME-2003", "fixedPrice": "19.90"},
        headers=auth("BackOffice"),
    )

    assert response.status_code == 201
    assert response.json()["conditions"][0]["fixedPrice"] == "19.90"
    assert service.await_args is not None
    assert service.await_args.args[0] == "CT-1"


# ==========================================
# Price calculation
# ==========================================

@pytest.mark.asyncio
async def test_the_price_calculation_answers_with_the_line_level(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=PriceCalculationResponse(
        productNumber="ACME-2003", basePrice=Decimal("24.50"), finalPrice=Decimal("19.90"),
        discountable=True,
        discount=Discount(
            type="ContractFixedPrice", percent=0.0, amount=Decimal("4.60"),
            source="Key Account Agreement (contract CTR-002)",
        ),
    ))
    monkeypatch.setattr(f"{PRICING_SERVICE}.calculate", service)

    response = await async_client.post(
        "/api/pricing/calculate",
        json={"customerId": "C-1001", "productNumber": "ACME-2003", "date": "2026-06-01"},
        headers=auth("Sales"),
    )

    assert response.status_code == 200
    assert response.json()["finalPrice"] == "19.90"
    assert response.json()["discount"]["type"] == "ContractFixedPrice"
    assert service.await_args is not None
    assert service.await_args.args[0].customerId == "C-1001"


@pytest.mark.asyncio
async def test_the_price_calculation_needs_a_reference_date(async_client, auth):
    """Contracts and discounts are valid for a period — without a date there is no answer
    to which of them applies."""
    response = await async_client.post(
        "/api/pricing/calculate",
        json={"customerId": "C-1001", "productNumber": "ACME-2003"},
        headers=auth("Sales"),
    )

    assert response.status_code == 422
