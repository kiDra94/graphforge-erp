"""The pure rules of the document chain: what a document does to the warehouse.

Which booking a document triggers, what a booking aims at, which counter a cancellation
corrects, how far a goods receipt covers its order. None of it needs a database, so every
rule can be read and tested as a truth table on its own.
"""

from typing import Literal

from domains.catalog.schemas_catalog import StockEffect
from domains.inventory.schemas_inventory import MovementType

from ..schemas_sales import (
    DeliveryStatus,
    DocumentType,
)

# Unit for which a fractional quantity is an input error rather than a valid value. The
# same constant as in the inventory domain — a piece cannot be delivered in halves,
# unlike kilograms or metres.
_WHOLE_NUMBER_UNIT = "pcs"


# Result of `_line_target`: what a stock booking of a line aims at — the product itself,
# the components of an asset built from it, or nothing.
LineTarget = Literal["product", "billOfMaterials", "none"]


# --- Pure business logic ------------------------------------------------------
# None of these functions needs a database, yet they sit here and not in the service:
# they are needed inside the write transaction the booking happens in. The same pattern
# as `_effect` in the inventory domain.

def _movement_type_for(type: DocumentType, predecessor_types: list[DocumentType]) -> MovementType | None:
    """Decides which stock booking the creation of a document triggers.

    A pure function without database access, so the truth table of all six document types
    can be played through in isolation.

    An order confirmation reserves, delivery note and invoice issue, a goods receipt books
    in. Quote and purchase order stay without effect — they only say something about an
    intention, not about a movement of goods.

    The invoice is the one special case: if a delivery note came before it, that one has
    already issued the goods, and a second issue would take the same goods out twice. So
    it is not the document type alone that decides, but the chain of predecessors as well.

    Args:
        type (DocumentType): The type of the document being created.
        predecessor_types (list[DocumentType]): The document types of the whole
            predecessor chain.

    Returns:
        MovementType | None: The movement type to trigger, or None when the document has
            no stock effect.
    """
    if type == "OrderConfirmation":
        return "Reservation"
    if type == "DeliveryNote":
        return "Issue"
    if type == "GoodsReceipt":
        return "Receipt"
    if type == "Invoice":
        return None if "DeliveryNote" in predecessor_types else "Issue"
    # Quote and purchase order
    return None


def _line_target(movement_type: MovementType, stock_effect: StockEffect) -> LineTarget:
    """Decides per line WHAT a stock booking hits — the product itself, the components of
    an asset built from it, or nothing.

    A pure function without database access, kept apart from `_movement_type_for`: that
    one decides from the **document type** whether anything is booked at all (6 cases),
    this one decides from the **line** what the already fixed movement type hits there
    (`Product.stockEffect`). A merged function would have 6 times 3 cases in one table.

    A goods receipt always books the product itself: purchasing knows no bill of
    materials to book against. On a reservation or an issue `stock_effect` decides:

    - `direct` → the product itself.
    - `billOfMaterials` → on a reservation (order confirmation) **nothing**: the asset
      does come into existence, but the reservation only happens on its own release, per
      asset and not per line. On an issue (delivery note, direct invoice) the
      **components** of the linked assets (`HAS_COMPONENT`).
    - `none` → nothing, regardless of the movement type (labour and flat fees).

    Args:
        movement_type (MovementType): The movement type already determined by
            `_movement_type_for`.
        stock_effect (StockEffect): The flag on the product of the line.

    Returns:
        LineTarget: `"product"`, `"billOfMaterials"` or `"none"`.
    """
    if movement_type == "Receipt":
        return "product"
    if stock_effect == "direct":
        return "product"
    if stock_effect == "none":
        return "none"
    # stock_effect == "billOfMaterials"
    return "billOfMaterials" if movement_type == "Issue" else "none"


def _reversal(movement_type: str, quantity: float) -> tuple[float, bool]:
    """Determines quantity and target of the correction that cancels a stock movement.

    A pure function without database access. Every reversal runs as a `Correction` — the
    only movement type allowed to carry a sign. What differs is only which target that
    correction hits and in which direction, depending on the original movement type:

    - `Reservation` only raised `reserved`, never `quantity` — the reversal therefore
      aims at `reserved` and lowers it by exactly that amount.
    - `Receipt` raised `quantity` — the reversal lowers it again.
    - `Issue` lowered `quantity` (and `reserved` with it, floored at 0) — the reversal
      raises **only** `quantity` again. `reserved` stays untouched: the operation the
      reservation was originally made for is void with the cancellation, so the goods
      come back freely available, not reserved again.

    Args:
        movement_type (str): The type of the movement to cancel — `Reservation`,
            `Receipt` or `Issue`. A `Correction` itself is never reversed.
        quantity (float): The quantity originally booked, always positive.

    Returns:
        tuple[float, bool]: The signed quantity of the correction, and whether it aims at
            `reserved` instead of `quantity`.
    """
    if movement_type == "Reservation":
        return -quantity, True
    if movement_type == "Receipt":
        return -quantity, False
    return quantity, False  # Issue


def _delivery_status(ordered: float, received: float) -> tuple[DeliveryStatus, float]:
    """Compares the ordered with the delivered quantity of a line.

    A pure function without database access. The open quantity is clamped at 0 on an
    over-delivery: a negative open quantity would not be an outstanding delivery but a
    question to the supplier.

    Args:
        ordered (float): The quantity ordered according to the purchase order line.
        received (float): The sum of all receipts booked against the purchase order.

    Returns:
        tuple[DeliveryStatus, float]: The delivery status and the quantity still open.
    """
    open_quantity = max(ordered - received, 0.0)

    if received > ordered:
        return "Over", open_quantity
    if received == ordered:
        return "Complete", open_quantity
    if received > 0:
        return "Partial", open_quantity
    return "Pending", open_quantity


def _fractional_quantity_violations(
    lines: list[dict], unit_per_product: dict[str, str | None]
) -> list[str]:
    """Checks document lines for fractional quantities on products measured in pieces.

    A pure function without database access, so the rule can be tested in isolation. A
    piece cannot be delivered in part — unlike 'kg', 'm' or 'l', a decimal is always an
    input error here, not a valid value.

    Args:
        lines (list[dict]): The bound line parameters (`productNumber`, `quantity`) of a
            document.
        unit_per_product (dict[str, str | None]): Product number -> `Product.unit`, from
            `_check_document_context`. A product without an entry is not checked.

    Returns:
        list[str]: One readable line per violation, empty when every line fits.
    """
    return [
        f"{line['productNumber']} (quantity {line['quantity']})"
        for line in lines
        if unit_per_product.get(line["productNumber"]) == _WHOLE_NUMBER_UNIT
        and line["quantity"] % 1 != 0
    ]
