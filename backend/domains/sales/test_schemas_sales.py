"""Unit tests of the sales schemas — the cross-field rules, without a database.

Every rule tested here is a `model_validator`. They are the only place in the domain where
a request is rejected before anything has been read or written, so each of them stands for
a state that must never reach the graph.
"""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from domains.sales.schemas_sales import (
    CancelledLine,
    ContractCreate,
    CustomerCreate,
    DeliveredLine,
    DocumentCreate,
    DocumentLineCreate,
    DocumentUpdate,
    GoodsReceiptCreate,
    GoodsReceiptLineCreate,
)


def _line(**overrides) -> DocumentLineCreate:
    data: dict = {"productNumber": "ACME-2003", "quantity": 2.0, "unitPrice": Decimal("24.50")}
    data.update(overrides)
    return DocumentLineCreate(**data)


def _document(**overrides) -> DocumentCreate:
    data: dict = {
        "type": "Invoice",
        "customerId": "C-1001",
        "lines": [_line()],
    }
    data.update(overrides)
    return DocumentCreate(**data)


# ==========================================
# DocumentLineCreate — reason and priceOverridden belong together
# ==========================================

def test_an_overridden_price_without_a_reason_is_rejected():
    """A price typed over by hand without an explanation leaves no trace anyone can read."""
    with pytest.raises(ValidationError):
        _line(priceOverridden=True)


def test_a_reason_without_an_overridden_price_is_rejected():
    """It would be an explanation for a change that never happened."""
    with pytest.raises(ValidationError):
        _line(reason="Goodwill.")


def test_an_overridden_price_with_a_reason_is_accepted():
    line = _line(priceOverridden=True, reason="Damaged unit, replaced free of charge.")

    assert line.priceOverridden is True


def test_a_line_quantity_of_zero_is_rejected():
    with pytest.raises(ValidationError):
        _line(quantity=0.0)


def test_a_negative_unit_price_is_rejected():
    with pytest.raises(ValidationError):
        _line(unitPrice=Decimal("-1.00"))


# ==========================================
# DocumentCreate — exactly one business partner
# ==========================================

@pytest.mark.parametrize(
    "document_type", ["Quote", "OrderConfirmation", "DeliveryNote", "Invoice"]
)
def test_a_sales_document_without_a_customer_is_rejected(document_type):
    """The list would show a row without a recipient, and the customer filter would never
    find it again."""
    extra = _extra_for(document_type)
    with pytest.raises(ValidationError):
        _document(type=document_type, customerId=None, **extra)


@pytest.mark.parametrize("document_type", ["PurchaseOrder", "GoodsReceipt"])
def test_a_purchasing_document_without_a_supplier_is_rejected(document_type):
    with pytest.raises(ValidationError):
        _document(type=document_type, customerId=None, supplierId=None)


@pytest.mark.parametrize("document_type", ["PurchaseOrder", "GoodsReceipt"])
def test_a_purchasing_document_with_a_customer_is_rejected(document_type):
    """The reports would not know whether it is a purchase or a sale."""
    with pytest.raises(ValidationError):
        _document(type=document_type, customerId="C-1001", supplierId="S-001")


@pytest.mark.parametrize(
    "document_type", ["Quote", "OrderConfirmation", "DeliveryNote", "Invoice"]
)
def test_a_sales_document_with_a_supplier_is_rejected(document_type):
    extra = _extra_for(document_type)
    with pytest.raises(ValidationError):
        _document(type=document_type, customerId="C-1001", supplierId="S-001", **extra)


def _extra_for(document_type: str) -> dict:
    """The fields the remaining validators demand for this document type."""
    if document_type == "Quote":
        return {"assetPurpose": "newAsset"}
    if document_type == "OrderConfirmation":
        return {"deliveryDate": "2026-06-01"}
    return {}


def test_a_purchase_order_with_a_supplier_is_accepted():
    document = _document(type="PurchaseOrder", customerId=None, supplierId="S-001")

    assert document.supplierId == "S-001"
    assert document.customerId is None


# ==========================================
# DocumentCreate — the two mandatory fields per document type
# ==========================================

def test_a_quote_without_an_asset_purpose_is_rejected():
    """A forgotten field would silently keep the quote out of both draft lists."""
    with pytest.raises(ValidationError):
        _document(type="Quote")


def test_a_quote_with_an_asset_purpose_is_accepted():
    document = _document(type="Quote", assetPurpose="spareParts")

    assert document.assetPurpose == "spareParts"


def test_an_order_confirmation_without_a_delivery_date_is_rejected():
    """Without the date no serial number can be formed when an asset is created."""
    with pytest.raises(ValidationError):
        _document(type="OrderConfirmation")


def test_an_order_confirmation_with_a_delivery_date_is_accepted():
    document = _document(type="OrderConfirmation", deliveryDate="2026-06-01")

    assert document.deliveryDate is not None


def test_an_invoice_needs_neither_of_the_two():
    """The two rules are bound to their document type and must not spill over."""
    document = _document(type="Invoice")

    assert document.assetPurpose is None
    assert document.deliveryDate is None


# ==========================================
# DocumentCreate — the same product twice
# ==========================================

def test_the_same_product_twice_on_a_sales_document_is_allowed():
    """Once regular, once as a goodwill line at a different price.

    The line is identified by document number and line number, not by product number.
    """
    document = _document(
        lines=[_line(), _line(unitPrice=Decimal("0.00"))]
    )

    assert len(document.lines) == 2


def test_the_same_product_twice_on_a_purchase_order_is_rejected():
    """The ordered/received comparison needs one unambiguous line per product."""
    with pytest.raises(ValidationError):
        _document(
            type="PurchaseOrder", customerId=None, supplierId="S-001",
            lines=[_line(), _line(quantity=5.0)],
        )


def test_the_message_names_the_duplicated_product():
    with pytest.raises(ValidationError) as excinfo:
        _document(
            type="PurchaseOrder", customerId=None, supplierId="S-001",
            lines=[_line(), _line()],
        )

    assert "ACME-2003" in str(excinfo.value)


def test_a_document_without_a_line_is_rejected():
    with pytest.raises(ValidationError):
        _document(lines=[])


# ==========================================
# DocumentUpdate — cancelledLines is bound to the status
# ==========================================

def test_a_partial_cancellation_without_lines_is_rejected():
    with pytest.raises(ValidationError):
        DocumentUpdate(status="partiallyCancelled")


def test_cancelled_lines_without_the_matching_status_are_rejected():
    """Without a status change it would stay open what the list means."""
    with pytest.raises(ValidationError):
        DocumentUpdate(status="cancelled", cancelledLines=[CancelledLine(lineNumber=1)])


def test_a_line_named_twice_in_a_cancellation_is_rejected():
    """The repository would reverse it twice, and with two quantities it would stay open
    which one holds."""
    with pytest.raises(ValidationError):
        DocumentUpdate(
            status="partiallyCancelled",
            cancelledLines=[CancelledLine(lineNumber=1), CancelledLine(lineNumber=1, quantity=Decimal(2))],
        )


def test_a_partial_cancellation_with_a_quantity_is_accepted():
    update = DocumentUpdate(
        status="partiallyCancelled",
        cancelledLines=[CancelledLine(lineNumber=2, quantity=Decimal(3))],
    )

    assert update.cancelledLines is not None
    assert update.cancelledLines[0].quantity == Decimal("3")


def test_a_cancellation_quantity_of_zero_is_rejected():
    with pytest.raises(ValidationError):
        DocumentUpdate(
            status="partiallyCancelled",
            cancelledLines=[CancelledLine.model_validate({"lineNumber": 1, "quantity": 0})],
        )


# ==========================================
# DocumentUpdate — deliveredQuantities needs a delivery status
# ==========================================

@pytest.mark.parametrize(
    "status", ["partiallyDelivered", "backorder", "partiallyCancelled", "completed"]
)
def test_delivered_quantities_are_allowed_with_a_delivery_status(status):
    extra: dict = (
        {"cancelledLines": [CancelledLine(lineNumber=9)]}
        if status == "partiallyCancelled" else {}
    )
    update = DocumentUpdate(
        status=status, deliveredQuantities=[DeliveredLine(lineNumber=1, quantity=Decimal(2))], **extra
    )

    assert update.deliveredQuantities is not None


def test_delivered_quantities_without_a_status_are_rejected():
    """The invoice arising from them would come into existence without a cause."""
    with pytest.raises(ValidationError):
        DocumentUpdate(deliveredQuantities=[DeliveredLine(lineNumber=1, quantity=Decimal(2))])


def test_delivered_quantities_with_an_unrelated_status_are_rejected():
    with pytest.raises(ValidationError):
        DocumentUpdate(
            status="cancelled",
            deliveredQuantities=[DeliveredLine(lineNumber=1, quantity=Decimal(2))],
        )


def test_a_line_delivered_twice_in_one_step_is_rejected():
    """Two rows for the same line would produce two invoice lines against one delivery
    note line."""
    with pytest.raises(ValidationError):
        DocumentUpdate(
            status="partiallyDelivered",
            deliveredQuantities=[
                DeliveredLine(lineNumber=1, quantity=Decimal(2)),
                DeliveredLine(lineNumber=1, quantity=Decimal(3)),
            ],
        )


def test_an_empty_update_is_a_valid_model():
    """The "no fields handed over" rule belongs to the repository, not to the schema.

    It needs `exclude_unset`, and only the repository sees that.
    """
    assert DocumentUpdate().model_dump(exclude_unset=True) == {}


# ==========================================
# GoodsReceiptCreate
# ==========================================

def test_the_same_purchase_order_line_twice_is_rejected():
    """Two rows of the same line could not be told apart in the response."""
    with pytest.raises(ValidationError):
        GoodsReceiptCreate(
            deliveryNoteNumber="DN-NS-99871",
            lines=[
                GoodsReceiptLineCreate(lineNumber=1, quantity=10.0),
                GoodsReceiptLineCreate(lineNumber=1, quantity=5.0),
            ],
        )


def test_a_goods_receipt_without_a_delivery_note_number_is_rejected():
    """Without it there would be calls the idempotency does not hold for."""
    with pytest.raises(ValidationError):
        GoodsReceiptCreate.model_validate({"lines": [{"lineNumber": 1, "quantity": 10.0}]})


def test_an_empty_delivery_note_number_is_rejected():
    with pytest.raises(ValidationError):
        GoodsReceiptCreate(
            deliveryNoteNumber="", lines=[GoodsReceiptLineCreate(lineNumber=1, quantity=1.0)]
        )


def test_two_different_lines_are_accepted():
    receipt = GoodsReceiptCreate(
        deliveryNoteNumber="DN-NS-99871",
        lines=[
            GoodsReceiptLineCreate(lineNumber=1, quantity=50.0),
            GoodsReceiptLineCreate(lineNumber=2, quantity=180.0),
        ],
    )

    assert len(receipt.lines) == 2


# ==========================================
# ContractCreate
# ==========================================

def test_a_term_running_backwards_is_rejected():
    """The contract would come into existence, could be assigned, appear in every list —
    and never take effect."""
    with pytest.raises(ValidationError):
        ContractCreate(
            name="Reversed", validFrom=date(2026, 12, 31), validTo=date(2026, 1, 1),
            isGlobal=False,
        )


def test_a_term_of_a_single_day_is_accepted():
    """Start equal to end is one valid day, not an error."""
    contract = ContractCreate(
        name="One day", validFrom=date(2026, 6, 1), validTo=date(2026, 6, 1), isGlobal=False
    )

    assert contract.validFrom == contract.validTo


def test_is_global_has_no_default():
    """A contract global by accident would take effect at every customer immediately."""
    with pytest.raises(ValidationError):
        ContractCreate.model_validate({
            "name": "No flag", "validFrom": "2026-01-01", "validTo": "2026-12-31",
        })


def test_a_contract_without_a_discount_rate_is_accepted():
    """A contract can carry fixed prices only."""
    contract = ContractCreate(
        name="Fixed prices only", validFrom=date(2026, 1, 1), validTo=date(2026, 12, 31),
        isGlobal=False,
    )

    assert contract.discountPercent is None


# ==========================================
# The shared input base — an unknown key is a 422, not a silent loss
# ==========================================

def test_an_unknown_field_is_rejected():
    """Pydantic would drop it by default, and the value would be lost on save without
    anyone noticing."""
    with pytest.raises(ValidationError):
        CustomerCreate.model_validate({"name": "Example Industries GmbH", "stret": "typo"})


def test_an_empty_customer_name_is_rejected():
    with pytest.raises(ValidationError):
        CustomerCreate(name="")
