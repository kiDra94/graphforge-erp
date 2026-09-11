"""Pydantic schemas of the sales domain: read, write and response models."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from core.neo4j_types import Neo4jDate, Neo4jDatetime
from core.schemas import InputModel

# The six document types. They live twice in the graph — as the property `type` and as a
# second label on the node — and are typed as a Literal here so an unknown value is
# rejected with a 422 while it is still a request.
DocumentType = Literal[
    "Quote",
    "OrderConfirmation",
    "DeliveryNote",
    "Invoice",
    "PurchaseOrder",
    "GoodsReceipt",
]

# Sales hangs off the customer, purchasing off the supplier. The split decides which of
# the two business partners is mandatory in the request.
SALES_DOCUMENTS: frozenset[str] = frozenset(
    {"Quote", "OrderConfirmation", "DeliveryNote", "Invoice"}
)
PURCHASING_DOCUMENTS: frozenset[str] = frozenset({"PurchaseOrder", "GoodsReceipt"})

DocumentStatus = Literal[
    "open", "completed", "cancelled", "partiallyCancelled", "partiallyDelivered", "backorder"
]

# The interface stays in one language, the document goes to the customer in theirs.
Language = Literal["DE", "EN"]

# Result of the ordered/received comparison per line. Recalculated on every call and
# stored nowhere.
DeliveryStatus = Literal["Complete", "Partial", "Over", "Pending"]

# What sales judges a quote to stand for — the basis of the asset drafts. Passed
# explicitly from the frontend to every follow-up document rather than propagated by the
# server, see `DocumentCreate.assetPurpose`.
AssetPurpose = Literal["newAsset", "spareParts"]


# --- Customer ----------------------------------------------------------------

class AccountManager(BaseModel):
    """The employee looking after a customer, as it appears in the customer response.

    Sits in the graph as a relationship, not as a property on the customer. That is why
    it is an object of id and name on read, but only the id on write (`accountManagerId`
    in `CustomerCreate` and `CustomerUpdate`).
    """
    id: str = Field(description="Id of the employee looking after the customer.")
    name: str | None = Field(default=None, description="Name of the employee.")


class Customer(BaseModel):
    """Schema for reading (GET) a customer.

    Deliberately tolerant: apart from `id` every field is optional. Customers taken over
    from a predecessor system carry little more than a number and a name; the remaining
    master data fields are maintained inside the system. A constraint here would protect
    nothing — it would merely turn an already stored database state into an HTTP 500.

    The `id` is the customer number and at the same time the business key — there is no
    second field carrying the same value.

    `accountManager` may be `null`: not every customer has an employee assigned.
    """
    id: str = Field(description="Customer number, at the same time the business key.")
    name: str | None = Field(default=None, description="Name of the customer.")
    street: str | None = Field(default=None, description="Street of the billing address.")
    city: str | None = Field(default=None, description="Postcode and city of the billing address.")
    country: str | None = Field(default=None, description="Country of the billing address.")
    vatId: str | None = Field(default=None, description="VAT identification number of the customer.")
    language: Language | None = Field(default=None, description="Language the documents of this customer are written in.")
    effectiveDiscountPercent: float | None = Field(default=None, description="The highest discountPercent among all contracts of the customer valid today, own and global alike.")
    accountManager: AccountManager | None = Field(default=None, description="Employee in charge in sales. Empty when nobody is assigned.")
    createdAt: Neo4jDatetime | None = Field(default=None, description="Time of creation, server-side in UTC. Empty on imported customers.")
    updatedAt: Neo4jDatetime | None = Field(default=None, description="Time of the last change, server-side in UTC.")


class CustomerCreate(InputModel):
    """Schema for creating (POST) a new customer.

    Declares its fields independently instead of inheriting from `Customer`: tightening
    `str | None` to `str` on the name would be a violation of the substitution principle
    if it came through inheritance.

    Only the name is mandatory. A customer often comes into existence from a first
    enquiry, at a point where the address and the VAT id are not known yet — were they
    mandatory, sales would have to invent placeholders, and those would then sit in the
    master record permanently.

    The customer number is deliberately absent: the server assigns it as `C-` plus a uuid4.
    A number set by the client could collide with one already given out, and it is printed
    on documents.

    `accountManagerId` is an employee id and creates the relationship — not a property on
    the customer.
    """
    name: str = Field(min_length=1, description="Name of the customer.")
    street: str | None = Field(default=None, description="Street of the billing address.")
    city: str | None = Field(default=None, description="Postcode and city of the billing address.")
    country: str | None = Field(default=None, description="Country of the billing address.")
    vatId: str | None = Field(default=None, description="VAT identification number of the customer.")
    language: Language | None = Field(default=None, description="Language the documents of this customer are written in.")
    accountManagerId: str | None = Field(default=None, description="Id of the employee in charge in sales.")


class CustomerUpdate(InputModel):
    """Schema for updating (PATCH) a customer.

    Every field is optional, because a PATCH request only has to carry the fields that
    actually change (partial update).

    The customer number cannot be changed and is therefore not a field of this model:
    every document, order and asset hangs off it. An `id` sent anyway is silently
    ignored — the call is not an error, it merely does not change the number.

    `accountManagerId` is included: a change of the person in charge is the normal case.
    """
    name: str | None = Field(default=None, min_length=1, description="Name of the customer.")
    street: str | None = Field(default=None, description="Street of the billing address.")
    city: str | None = Field(default=None, description="Postcode and city of the billing address.")
    country: str | None = Field(default=None, description="Country of the billing address.")
    vatId: str | None = Field(default=None, description="VAT identification number of the customer.")
    language: Language | None = Field(default=None, description="Language the documents of this customer are written in.")
    accountManagerId: str | None = Field(default=None, description="Id of the employee in charge in sales.")


class ContractAssignment(InputModel):
    """Schema for assigning (POST) a framework contract to a customer."""
    contractId: str = Field(min_length=1, description="Id of the contract to assign.")


# --- Building blocks of the document responses --------------------------------

class DocumentCustomer(BaseModel):
    """The customer in the document list — number and name only.

    The list shows one row per document; address and VAT id would only produce data
    volume nobody displays there. For the document print the complete form sits in
    `DocumentCustomerFull`.

    On purchasing documents the customer is empty — the supplier stands there instead.
    """
    id: str = Field(description="Customer number.")
    name: str | None = Field(default=None, description="Name of the customer.")


class DocumentCustomerFull(BaseModel):
    """The customer in the document detail view — with billing address and VAT id.

    A class of its own rather than a subclass of `DocumentCustomer`: a shared schema with
    six fields of which the list never fills four would be a promise the list does not
    keep.

    The four address fields are optional. They are maintained inside the system and are
    missing on every customer nobody has filled in yet.
    """
    id: str = Field(description="Customer number.")
    name: str | None = Field(default=None, description="Name of the customer.")
    street: str | None = Field(default=None, description="Street of the billing address.")
    city: str | None = Field(default=None, description="Postcode and city of the billing address.")
    country: str | None = Field(default=None, description="Country of the billing address.")
    vatId: str | None = Field(default=None, description="VAT identification number of the customer.")


class DocumentAddress(InputModel):
    """The address frozen onto a document (`DocumentCreate.customerAddress`).

    The document freezes the address when it is created instead of reading it from the
    `Customer` node forever. Without that, a customer moving house would retroactively
    change the address on every invoice already issued — wrong for a document meant to
    evidence the state at the time of issue.

    Every field is optional and may be missing individually: what is not given here the
    server takes from the customer master. A client that sends nothing at all therefore
    automatically gets today's complete address frozen in.
    """
    name: str | None = Field(default=None, description="Differing name of the recipient.")
    street: str | None = Field(default=None, description="Differing street.")
    city: str | None = Field(default=None, description="Differing postcode and city.")
    country: str | None = Field(default=None, description="Differing country.")
    vatId: str | None = Field(default=None, description="Differing VAT identification number.")


class DocumentSupplier(BaseModel):
    """The supplier of a purchasing document — id and name only.

    Purchase order and goods receipt hang off the supplier instead of the customer.
    Exactly one of the two is set per document, never both.
    """
    id: str = Field(description="Id of the supplier.")
    name: str | None = Field(default=None, description="Name of the supplier.")


class DocumentEmployee(BaseModel):
    """The employee who created the document.

    Sits on the document as a relationship, not as a property. On imported documents it
    can be missing.
    """
    id: str = Field(description="Id of the employee.")
    name: str | None = Field(default=None, description="Name of the employee.")


# --- Document lines -----------------------------------------------------------

class DocumentLine(BaseModel):
    """One line of the document, the way it is returned.

    Quantity, unit price, discount and the override flag belong to the line and not to
    the product: the same product can be priced differently on the next document.

    `label` and `unit` come from the product master and are optional. Products that only
    entered the graph through a document line carry neither.

    `lineNumber` is the key component of the line (`DocumentLine.id = documentNumber +
    '_' + lineNumber`) and is assigned consecutively per document on creation; after that
    it stays stable. It allows the same product to appear more than once on a document
    (sales) and addresses a line unambiguously, independently of its product number.

    `amount` is the line amount from quantity, unit price and line discount, rounded to
    cents. It is calculated and not stored; the rounding happens per line so the sum of
    the amounts shown equals the subtotal.

    `deliveredQuantity` and `openQuantity` are calculated as well, from the incoming
    `FULFILS` edges (for instance invoice lines against a delivery note, goods receipt
    lines against a purchase order). A line without any incoming edge — the normal case
    outside order confirmations and purchase orders — shows `deliveredQuantity: 0` and
    `openQuantity = quantity`.

    `cancelled` marks a line cancelled as part of a partial cancellation. It stays
    visible on the document (including its calculated `amount`) and is nevertheless no
    longer counted towards `subtotal`/`totalNet` — an invoice once issued is not changed
    retroactively; a cancellation is an additional flag, not a deletion.
    """
    productNumber: str = Field(description="Product number of the line.")
    lineNumber: int | None = Field(default=None, description="Consecutive line number, assigned on creation. Key component of the line, stable afterwards.")
    label: str | None = Field(default=None, description="Label of the product from the product master.")
    unit: str | None = Field(default=None, description="Unit of measure of the product, e.g. 'pcs'.")
    quantity: float | None = Field(default=None, description="Quantity of the line. Float, because goods are also delivered in metres and kilograms.")
    unitPrice: Decimal | None = Field(default=None, description="Price per unit in euro.")
    discountPercent: float | None = Field(default=None, description="Discount on this line in percent.")
    priceOverridden: bool | None = Field(default=None, description="Whether the price of this line was set by hand.")
    hasFixedPrice: bool = Field(default=False, description="Whether this price is a contractually fixed price. Such a line is not discounted again by the order discount.")
    amount: Decimal | None = Field(default=None, description="Line amount in euro after the line discount, rounded to cents.")
    deliveredQuantity: float | None = Field(default=None, description="Sum of the quantities of all incoming FULFILS edges (cancelled ones excluded). 0 when no follow-up line exists.")
    openQuantity: float | None = Field(default=None, description="quantity minus deliveredQuantity, never negative. Null when quantity is missing.")
    cancelled: bool = Field(default=False, description="Whether this line was cancelled as part of a partial cancellation.")


class DocumentLineCreate(InputModel):
    """One line of the document, the way it is created.

    The unit price comes from the request and is not determined here — no price
    calculation takes place at this point.

    `locationId` steers which stock the line books against. Without a value the main
    warehouse applies; on document types without stock effect the field stays inert.

    `priceOverridden` marks a price set by hand and comes from the request as well.
    Without a price calculation nobody else could set it.
    """
    productNumber: str = Field(min_length=1, description="Product number of the line.")
    quantity: float = Field(gt=0, description="Quantity of the line. Has to be greater than 0.")
    unitPrice: Decimal = Field(ge=0, description="Price per unit in euro.")
    discountPercent: float = Field(default=0.0, ge=0, le=100, description="Discount on this line in percent.")
    priceOverridden: bool = Field(default=False, description="Whether the price of this line was set by hand.")
    hasFixedPrice: bool = Field(default=False, description="Whether this price is a contractually fixed price. Such a line is not discounted again by the order discount.")
    reason: str | None = Field(default=None, description="Reason for a manual price change. Mandatory when priceOverridden is True.")
    locationId: str | None = Field(default=None, description="Location booked against. Without a value the main warehouse.")

    @model_validator(mode="after")
    def reason_only_with_an_overridden_price(self) -> DocumentLineCreate:
        """Binds `reason` to `priceOverridden=True`.

        Without that coupling a reason could be sent without a recognisable cause, or a
        price set by hand without any explanation for it.
        """
        if self.priceOverridden and not self.reason:
            raise ValueError("reason has to be given when priceOverridden is True.")
        if not self.priceOverridden and self.reason:
            raise ValueError("reason is only allowed when priceOverridden is True.")
        return self


# --- Document -----------------------------------------------------------------

class Document(BaseModel):
    """Schema for one row of the document list (GET).

    Apart from the number everything is optional. The document header in the graph is
    thinner than the response: delivery date, order discount and tax rate are maintained
    inside the system and are missing across the entire imported stock.

    `customer` and `supplier` exclude each other — sales documents carry the customer,
    purchasing documents the supplier.

    `totalNet` is calculated and not stored. A document without lines returns 0.00 here.
    """
    number: str = Field(description="Document number, the business key of the document.")
    type: DocumentType | None = Field(default=None, description="Document type.")
    status: DocumentStatus | None = Field(default=None, description="Processing state of the document.")
    date: Neo4jDate | None = Field(default=None, description="Document date, as printed on the document.")
    deliveryDate: Neo4jDate | None = Field(default=None, description="Planned delivery date. On an order confirmation the basis of the serial number of newly created assets.")
    language: Language | None = Field(default=None, description="Language the document goes to the recipient in.")
    customer: DocumentCustomer | None = Field(default=None, description="Recipient on sales documents. Empty on purchasing documents.")
    supplier: DocumentSupplier | None = Field(default=None, description="Source of supply on purchasing documents. Empty on sales documents.")
    totalNet: Decimal | None = Field(default=None, description="Net amount in euro, calculated from the lines.")
    basedOn: list[str] = Field(default_factory=list, description="Numbers of the predecessor documents in the chain, as a list, because an invoice over several partial deliveries points at several delivery notes. Empty when the document has no predecessor.")
    orderProjectNumber: str | None = Field(default=None, description="Project number of the corresponding order. Empty on purchasing documents.")
    assetPurpose: AssetPurpose | None = Field(default=None, description="Sales judgement, 'newAsset' or 'spareParts'. Only maintained on sales documents.")


class DocumentDetail(BaseModel):
    """Schema for the detail view of a document (GET) — the basis of the document print.

    Deliberately does not inherit from `Document`: the customer appears here with address
    and VAT id, in the list only with number and name. An overridden field type would no
    longer be a subtype.

    Four fields are calculated and not stored: `subtotal`, `orderDiscountAmount`,
    `totalNet` and `totalGross`. The calculation runs from the inside out — line discount
    per line, from those the subtotal, off that the order discount, on top of that the
    tax.

    A missing order discount or tax rate counts as 0 but is still returned as `null`: a 0
    in the response would be a maintained value nobody maintained.

    The lines are returned in their sort order, not by product number: the same product
    can appear more than once on a sales document, and then only the order of creation
    decides.
    """
    number: str = Field(description="Document number, the business key of the document.")
    type: DocumentType | None = Field(default=None, description="Document type.")
    status: DocumentStatus | None = Field(default=None, description="Processing state of the document.")
    date: Neo4jDate | None = Field(default=None, description="Document date, as printed on the document.")
    deliveryDate: Neo4jDate | None = Field(default=None, description="Planned delivery date. On an order confirmation the basis of the serial number of newly created assets.")
    language: Language | None = Field(default=None, description="Language the document goes to the recipient in.")
    createdBy: DocumentEmployee | None = Field(default=None, description="Employee who created the document.")
    customer: DocumentCustomerFull | None = Field(default=None, description="Recipient on sales documents, with the address for the document print.")
    supplier: DocumentSupplier | None = Field(default=None, description="Source of supply on purchasing documents. Empty on sales documents.")
    lines: list[DocumentLine] = Field(default_factory=list, description="The lines of the document, in their sort order.")
    subtotal: Decimal | None = Field(default=None, description="Sum of the line amounts in euro, before the order discount.")
    orderDiscountPercent: float | None = Field(default=None, description="Discount on the whole document in percent.")
    orderDiscountAmount: Decimal | None = Field(default=None, description="Amount of the order discount in euro.")
    totalNet: Decimal | None = Field(default=None, description="Net amount in euro after the order discount.")
    taxPercent: float | None = Field(default=None, description="Tax rate in percent, valid on the document date.")
    totalGross: Decimal | None = Field(default=None, description="Gross amount in euro including tax.")
    basedOn: list[str] = Field(default_factory=list, description="Numbers of the predecessor documents in the chain. Empty when the document has no predecessor.")
    orderProjectNumber: str | None = Field(default=None, description="Project number of the corresponding order. Empty on purchasing documents.")
    assetPurpose: AssetPurpose | None = Field(default=None, description="Sales judgement, 'newAsset' or 'spareParts'. Only maintained on sales documents.")
    createdAt: Neo4jDatetime | None = Field(default=None, description="Time of creation, server-side in UTC. Empty on imported documents.")
    updatedAt: Neo4jDatetime | None = Field(default=None, description="Time of the last change, server-side in UTC.")
    createdFollowUpDocument: str | None = Field(default=None, description="Number of the invoice that came into existence automatically when a delivery note was set to status='completed'. Otherwise null.")


class DocumentCreate(InputModel):
    """Schema for creating (POST) a document.

    The document number and the document date are deliberately absent: the server assigns
    both. The number is printed on documents and follows the pattern of the existing
    stock, the date is the day of creation.

    The stock effect follows from `type`: an order confirmation reserves, delivery note
    and invoice issue, a goods receipt books in, quote and purchase order stay without
    effect. The bookings come into existence in the same transaction as the document; if
    the stock is not enough, neither of them does.
    """
    type: DocumentType = Field(description="Document type. Determines which stock effect the creation triggers.")
    customerId: str | None = Field(default=None, description="Recipient on sales documents. Mandatory on quote, order confirmation, delivery note and invoice.")
    customerAddress: DocumentAddress | None = Field(
        default=None,
        description="Differing address for this document. Without a value the server takes the address of the customer at the time of creation. Individual fields may be missing; the customer master applies to them as well.",
    )
    supplierId: str | None = Field(default=None, description="Source of supply on purchasing documents. Mandatory on purchase order and goods receipt.")
    language: Language = Field(default="DE", description="Language the document goes to the recipient in.")
    deliveryDate: Neo4jDate | None = Field(default=None, description="Planned delivery date. Mandatory on 'OrderConfirmation' — the basis of the serial number of newly created assets.")
    orderDiscountPercent: float | None = Field(default=None, ge=0, le=100, description="Discount on the whole document in percent.")
    taxPercent: float | None = Field(default=None, ge=0, le=100, description="Tax rate in percent, valid on the document date.")
    basedOn: list[str] | None = Field(default=None, description="Numbers of the predecessor documents in the chain, as a list — an invoice over several partial deliveries points at several delivery notes, the normal case stays a list with one element.")
    orderProjectNumber: str | None = Field(default=None, description="Project number of the corresponding order.")
    assetPurpose: AssetPurpose | None = Field(
        default=None,
        description="Mandatory on 'Quote' only: says whether the quote stands for a new asset ('newAsset') or for spare parts for an existing one ('spareParts'). On follow-up documents taken over from the quote by the frontend, not derived by the server.",
    )
    lines: list[DocumentLineCreate] = Field(min_length=1, description="The lines of the document. At least one.")

    @model_validator(mode="after")
    def asset_purpose_is_mandatory_on_a_quote(self) -> DocumentCreate:
        """Enforces `assetPurpose` on a new quote.

        Sales knows during the call with the customer whether this is about a new asset
        or about spare parts for an existing one. Without this obligation a forgotten
        field would silently keep the quote out of both draft lists, and an asset that
        was due would never come into existence.
        """
        if self.type == "Quote" and self.assetPurpose is None:
            raise ValueError("assetPurpose is required for the document type 'Quote'.")
        return self

    @model_validator(mode="after")
    def delivery_date_is_mandatory_on_an_order_confirmation(self) -> DocumentCreate:
        """Enforces `deliveryDate` on a new order confirmation.

        Without that date no serial number can be formed when an asset is created (month
        and year). Without this obligation it would only show up deep inside the asset
        draft, long after the order confirmation has been saved.
        """
        if self.type == "OrderConfirmation" and self.deliveryDate is None:
            raise ValueError("deliveryDate is required for the document type 'OrderConfirmation'.")
        return self

    @model_validator(mode="after")
    def exactly_one_business_partner(self) -> DocumentCreate:
        """Checks that the document matches the side it comes into existence on.

        A document hangs off either the customer or the supplier, never off both. Which
        side is meant is said by the document type: quote, order confirmation, delivery
        note and invoice go to a customer, purchase order and goods receipt to a supplier.

        Without this check a document without any business partner would come into
        existence — the list would show a row without a recipient, and the customer
        filter would never find it again. A document with both relationships would be
        just as unresolvable: the reports would not know whether it is a purchase or a
        sale.
        """
        if self.type in PURCHASING_DOCUMENTS:
            if self.supplierId is None:
                raise ValueError(f"supplierId is required for the document type '{self.type}'.")
            if self.customerId is not None:
                raise ValueError(
                    f"customerId is not allowed for the document type '{self.type}': a "
                    "purchasing document belongs to a supplier."
                )
        else:
            if self.customerId is None:
                raise ValueError(f"customerId is required for the document type '{self.type}'.")
            if self.supplierId is not None:
                raise ValueError(
                    f"supplierId is not allowed for the document type '{self.type}': a "
                    "sales document belongs to a customer."
                )
        return self

    @model_validator(mode="after")
    def the_same_product_twice_is_sales_only(self) -> DocumentCreate:
        """Rejects a purchasing document carrying the same product more than once.

        A line is identified by document number and line number, not by product number —
        on a sales document the same product may therefore appear several times (once
        regular, once as a goodwill line at a different price, say).

        In **purchasing** that stays forbidden: the ordered/received comparison of the
        goods receipt needs one unambiguous purchase order line per product, otherwise a
        delivered quantity cannot be assigned.
        """
        if self.type not in PURCHASING_DOCUMENTS:
            return self
        numbers = [line.productNumber for line in self.lines]
        duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
        if duplicates:
            raise ValueError(
                f"Every product may appear only once per document type '{self.type}', "
                f"contained more than once: {', '.join(duplicates)}."
            )
        return self


class DeliveredLine(InputModel):
    """A partial quantity of a delivery note line actually delivered in this step.

    The opposite direction of `CancelledLine`: there it is about the remainder that never
    goes out, here about the quantity that has just gone out.

    Reported is the quantity of **this one step**, not the new running total. Two
    follow-up deliveries of 2 pieces each therefore send `quantity: 2` twice. The running
    total follows on its own, because every delivery creates its own invoice line and
    that line hangs off the delivery note line through a `FULFILS` edge —
    `deliveredQuantity` is the sum of those edges and is never written forward.

    The quantity may not exceed the quantity still open. Delivering more than the
    delivery note carries would mean charging for goods that are on no document.
    """
    lineNumber: int = Field(gt=0, description="Line number of the delivered line. Addressed by number, not by product number — the same product may appear more than once on a sales document.")
    quantity: Decimal = Field(gt=0, description="Quantity delivered in this step. At most the quantity still open on the line.")


class CancelledLine(InputModel):
    """A line hit by a partial cancellation — entirely or only in part.

    Without `quantity` a partial cancellation could only take a line back as a whole, and
    the reversal always gave the full booked quantity back. For the most common case in
    the warehouse that is wrong: if two of five pieces have gone out and the remaining
    three are to be written off, all five came back into stock — two of them were already
    at the customer at that point.
    """
    lineNumber: int = Field(description="Line number of the affected line. Addressed by number, not by product number — the same product can appear more than once on a sales document.")
    quantity: Decimal | None = Field(
        default=None,
        gt=0,
        description="Partial quantity to cancel. Without a value the line is cancelled entirely. May not exceed the booked quantity.",
    )


class DocumentUpdate(InputModel):
    """Schema for updating (PATCH) a document.

    Every field is optional, because a PATCH request only has to carry the fields that
    actually change (partial update).

    Lines cannot be changed in substance: they carry the stock effect of the document,
    and a quantity changed afterwards would not pull the bookings already made along with
    it. Cancelling individual lines through `cancelledLines` is exempt from that — there
    the reversal does come along, by exactly the cancelled amount. A line cancelled
    **entirely** is marked `cancelled` and no longer counts towards the totals; a line
    cancelled **in part** keeps no marking but loses the cancelled quantity — the
    customer still owes what was actually delivered.

    Also exempt is `deliveredQuantities` on a delivery note: it changes no line but
    records what went out in this step. The server creates the invoice from it and hangs
    it off the delivery note lines through `FULFILS`; `deliveredQuantity` and
    `openQuantity` of the line follow from that. The remaining quantity of a partial
    delivery therefore lives on the server and not in the browser.

    `status: "cancelled"` cancels the whole document (all lines), `status:
    "partiallyCancelled"` only the ones named in `cancelledLines`. Both trigger a
    reversal of the original stock effect and are only possible out of `open` or
    `completed` — a document already (partially) cancelled cannot be cancelled a second
    time, and a document with an active (not cancelled) follow-up document is rejected:
    cancellation runs backwards through the chain.
    """
    status: DocumentStatus | None = Field(default=None, description="New processing state of the document.")
    cancelledLines: list[CancelledLine] | None = Field(
        default=None,
        description="Only with status='partiallyCancelled': the lines to cancel, each with a line number and an optional partial quantity. Without a quantity the line is cancelled entirely.",
    )
    deliveredQuantities: list[DeliveredLine] | None = Field(
        default=None,
        description="Only on a delivery note: the partial quantities delivered in this step. The server creates the invoice from them and links it to the delivery note lines through FULFILS. Without a value the invoice on the switch to 'completed' covers the entire quantity still open.",
    )
    deliveryDate: Neo4jDate | None = Field(default=None, description="Planned delivery date.")
    orderDiscountPercent: float | None = Field(default=None, ge=0, le=100, description="Discount on the whole document in percent.")
    taxPercent: float | None = Field(default=None, ge=0, le=100, description="Tax rate in percent, valid on the document date.")
    assetPurpose: AssetPurpose | None = Field(default=None, description="Correction of a sales judgement misjudged on creation. Only affects drafts that have not been confirmed or linked yet.")

    @model_validator(mode="after")
    def cancelled_lines_only_on_a_partial_cancellation(self) -> DocumentUpdate:
        """Binds `cancelledLines` to `status='partiallyCancelled'`.

        Without that coupling a list of lines could be sent without a matching status
        change, without it being clear what it means — or a partial cancellation without
        saying which line is meant.
        """
        if self.status == "partiallyCancelled" and not self.cancelledLines:
            raise ValueError("cancelledLines has to be given when status is 'partiallyCancelled'.")
        if self.status != "partiallyCancelled" and self.cancelledLines:
            raise ValueError("cancelledLines is only allowed when status is 'partiallyCancelled'.")
        if self.cancelledLines:
            # The same line twice would be ambiguous: the repository would reverse it
            # twice, and with two different quantities it would stay open which one holds.
            numbers = [line.lineNumber for line in self.cancelledLines]
            duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
            if duplicates:
                raise ValueError(
                    "cancelledLines names line numbers more than once: "
                    f"{', '.join(str(n) for n in duplicates)}."
                )
        return self

    @model_validator(mode="after")
    def delivered_quantities_need_a_delivery_status(self) -> DocumentUpdate:
        """Binds `deliveredQuantities` to the statuses that conclude a delivery.

        Without a status change it would stay open how things continue with the delivery
        note — and the invoice arising from the quantities would come into existence
        without a recognisable cause. `completed` is allowed but not necessary: if the
        delivery covers the remainder, the server closes the delivery note by itself.
        """
        allowed = ("partiallyDelivered", "backorder", "partiallyCancelled", "completed")
        if self.deliveredQuantities is not None and self.status not in allowed:
            raise ValueError(
                "deliveredQuantities is only allowed together with status "
                f"{', '.join(allowed)}."
            )
        if self.deliveredQuantities:
            # The same reasoning as for cancelledLines: two rows for the same line would
            # produce two invoice lines against one delivery note line, and which quantity
            # holds would stay open.
            numbers = [line.lineNumber for line in self.deliveredQuantities]
            duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
            if duplicates:
                raise ValueError(
                    "deliveredQuantities names line numbers more than once: "
                    f"{', '.join(str(n) for n in duplicates)}."
                )
        return self


class PriceOverrideLine(BaseModel):
    """A recorded manual price change of a document line.

    Comes into existence only for lines with `priceOverridden=True` — the normal case
    stays unrecorded. `oldPrice` is the standard sales price of the product at the time
    of the change, not the value that stood on the line before.

    Addressed by `lineNumber`, not by `productNumber`: the same product may appear more
    than once on a sales document, and then a row can no longer be assigned to one line
    by product number alone.
    """
    lineNumber: int = Field(description="Line number of the affected document line.")
    productNumber: str = Field(description="Product number of the affected line.")
    oldPrice: Decimal = Field(description="Standard sales price of the product at the time of the change, in euro.")
    newPrice: Decimal = Field(description="Unit price of the line set by hand, in euro.")
    reason: str = Field(description="Reason for the price change.")
    employee: DocumentEmployee | None = Field(default=None, description="Employee who recorded the change.")
    createdAt: Neo4jDatetime | None = Field(default=None, description="Time of recording, server-side in UTC.")


# --- Goods receipt control ----------------------------------------------------

class GoodsReceiptLineCreate(InputModel):
    """One checked line of the delivery.

    Addresses the purchase order line through its `lineNumber`, not through the product
    number — the same key change as on `DocumentLine`. Product and agreed price follow
    server-side from the referenced purchase order line; a `FULFILS` edge from the newly
    created goods receipt line to the purchase order line comes into existence in the
    same transaction.

    The received quantity is the one that actually arrived, not the one ordered. It may
    exceed the ordered quantity — an over-delivery is reported and not rejected.

    `purchasePrice` is optional; without a value the price agreed on the purchase order
    line applies. The value is recorded on the booking and is the basis of the moving
    average price — without it every receipt booked this way would stay invisible in the
    cost price calculation.

    No `locationId`: the receiving location is not an input but a fixed rule — goods are
    always delivered into the main warehouse.
    """
    lineNumber: int = Field(description="Line number of the purchase order line being delivered.")
    quantity: float = Field(gt=0, description="Quantity that actually arrived.")
    purchasePrice: Decimal | None = Field(default=None, ge=0, description="Purchase price per unit in euro. Without a value the price of the purchase order line.")


class GoodsReceiptCreate(InputModel):
    """Schema for the goods receipt control (POST) against a purchase order.

    The call compares ordered against received and books the same operation in. It is
    idempotent over `deliveryNoteNumber`: a second call with the same number against the
    same purchase order does not book a second time but returns the result of the first
    booking unchanged. That prevents an over-delivery through a double click or a second
    tab.

    `deliveryNoteNumber` is therefore mandatory and not optional: without it there would
    be calls for which the idempotency does not hold, and that is exactly what is to be
    ruled out. Two genuine partial deliveries stay possible — they carry different
    delivery note numbers and cannot be told apart by their body alone.

    Not all lines of the purchase order have to be included. What is missing stays
    outstanding and can be supplied with a later delivery.
    """
    deliveryNoteNumber: str = Field(min_length=1, description="Delivery note number of the supplier. Makes the call idempotent — a second call with the same number against this purchase order does not book again.")
    lines: list[GoodsReceiptLineCreate] = Field(min_length=1, description="The checked lines. At least one.")

    @model_validator(mode="after")
    def line_numbers_are_unique(self) -> GoodsReceiptCreate:
        """Rejects a delivery carrying the same purchase order line more than once.

        The ordered/received comparison summarises one row per purchase order line. Two
        rows of the same line could not be told apart in the response, and the caller
        could not recognise which of the two was booked.
        """
        numbers = [line.lineNumber for line in self.lines]
        duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
        if duplicates:
            raise ValueError(
                "Every line may appear only once per goods receipt, contained more than "
                f"once: {', '.join(str(n) for n in duplicates)}."
            )
        return self


class GoodsReceiptLineResult(BaseModel):
    """The result of the ordered/received comparison for one line.

    No field is stored. The ordered quantity sits on the purchase order line, the
    received quantity is the sum of all receipts booked against the purchase order —
    including the earlier ones.

    `openQuantity` is clamped at 0 on an over-delivery. A negative open quantity would
    not be an outstanding delivery but a question to the supplier.
    """
    productNumber: str = Field(description="Product number of the line.")
    label: str | None = Field(default=None, description="Label of the product from the product master.")
    orderedQuantity: float = Field(description="Quantity ordered according to the purchase order line.")
    receivedQuantity: float = Field(description="Sum of all receipts booked against the purchase order.")
    openQuantity: float = Field(ge=0, description="Quantity still outstanding. 0 on an over-delivery, never negative.")
    deliveryStatus: DeliveryStatus = Field(description="Result of the ordered/received comparison for this line.")


class GoodsReceiptResult(BaseModel):
    """Response to the goods receipt control (200).

    Deliberately carries no `success` field: on a 2xx it would be `true` by definition. A
    failure comes back as 400, 404, 422 or 500 through the global exception handlers.

    Reported are all lines of the purchase order, not only the delivered ones. A line
    nothing ever arrived for is the most important row of the check and must not be
    missing.

    `purchaseOrderStatus` is included because a booking can set the purchase order to
    'completed' automatically as soon as every line is complete (or over-delivered)
    afterwards — without the field the client would have to make a second
    `GET /api/documents/{number}` call just to learn whether this very call closed the
    purchase order.
    """
    purchaseOrderNumber: str = Field(description="Number of the purchase order checked against.")
    purchaseOrderStatus: DocumentStatus = Field(description="Status of the purchase order after this booking. Switches to 'completed' automatically as soon as no line is open any more.")
    goodsReceiptNumber: str = Field(description="Number of the goods receipt document created. On a repeated call the number of the first one. On a follow-up delivery to the same purchase order the base number with an appended counter (e.g. 'GR-2026-0007-2').")
    deliveryNoteNumber: str = Field(description="The delivery note number from the request.")
    lines: list[GoodsReceiptLineResult] = Field(default_factory=list, description="Ordered/received comparison per line, in line number order.")


# --- Contracts and conditions -------------------------------------------------

class ContractCustomer(BaseModel):
    """A customer assigned to a contract, as it appears in the contract response."""
    id: str = Field(description="Customer number.")
    name: str | None = Field(default=None, description="Name of the customer.")


class Condition(BaseModel):
    """A condition for a product inside a contract.

    Carries the fixed price only — the discount rate comes flat from the contract
    (`Contract.discountPercent`), not per product.
    """
    productNumber: str = Field(description="Product number the condition applies to.")
    label: str | None = Field(default=None, description="Label of the product from the product master.")
    fixedPrice: Decimal | None = Field(default=None, description="Fixed price per unit in euro.")


class Contract(BaseModel):
    """Schema for one row of the contract list (GET)."""
    id: str = Field(description="Id of the contract.")
    name: str | None = Field(default=None, description="Name of the contract.")
    description: str | None = Field(default=None, description="Short description of the contract.")
    validFrom: Neo4jDate | None = Field(default=None, description="Start of validity.")
    validTo: Neo4jDate | None = Field(default=None, description="End of validity.")
    isGlobal: bool | None = Field(default=None, description="Whether the contract applies to every customer, independently of an assignment.")
    discountPercent: float | None = Field(default=None, description="Flat discount of the contract in percent, independent of the individual condition.")
    active: bool | None = Field(default=None, description="Whether the contract is valid today. Calculated, not stored.")
    customers: list[ContractCustomer] = Field(default_factory=list, description="Customers assigned to the contract. Always empty on a global contract.")


class ContractDetail(BaseModel):
    """Schema for the detail view of a contract (GET) including all conditions."""
    id: str = Field(description="Id of the contract.")
    name: str | None = Field(default=None, description="Name of the contract.")
    description: str | None = Field(default=None, description="Short description of the contract.")
    validFrom: Neo4jDate | None = Field(default=None, description="Start of validity.")
    validTo: Neo4jDate | None = Field(default=None, description="End of validity.")
    isGlobal: bool | None = Field(default=None, description="Whether the contract applies to every customer, independently of an assignment.")
    discountPercent: float | None = Field(default=None, description="Flat discount of the contract in percent, independent of the individual condition.")
    active: bool | None = Field(default=None, description="Whether the contract is valid today. Calculated, not stored.")
    customers: list[ContractCustomer] = Field(default_factory=list, description="Customers assigned to the contract. Always empty on a global contract.")
    conditions: list[Condition] = Field(default_factory=list, description="The conditions stored in the contract.")


class ContractCreate(InputModel):
    """Schema for creating (POST) a framework contract."""
    name: str = Field(min_length=1, description="Name of the contract.")
    description: str | None = Field(default=None, description="Short description of the contract.")
    validFrom: Neo4jDate = Field(description="Start of validity.")
    validTo: Neo4jDate = Field(description="End of validity. May not lie before validFrom.")
    isGlobal: bool = Field(description="Whether the contract applies to every customer. No default, because it is security-relevant.")
    discountPercent: float | None = Field(default=None, ge=0, le=100, description="Flat discount of the contract in percent. Optional — a contract can also carry fixed prices only.")

    @model_validator(mode="after")
    def the_term_has_to_run_forwards(self) -> ContractCreate:
        """Rejects a period whose end lies before its start.

        Every validity check in the system reads `date() >= validFrom AND date() <=
        validTo`. With the dates swapped it can hold on no single day: the contract would
        come into existence, could be assigned, appears in every list — and never takes
        effect. A mistake nobody recognises from the result, because the result simply
        fails to appear.
        """
        if self.validTo < self.validFrom:
            raise ValueError("validTo may not lie before validFrom.")
        return self


class ConditionCreate(InputModel):
    """Schema for storing (POST) a condition inside a contract.

    `fixedPrice` is the only effect of a condition and therefore mandatory — a condition
    without a fixed price would be an edge without a purpose.
    """
    productNumber: str = Field(min_length=1, description="Product number the condition applies to.")
    fixedPrice: Decimal = Field(ge=0, description="Fixed price per unit in euro.")


# --- Price calculation --------------------------------------------------------

class PriceCalculationRequest(InputModel):
    """Schema for the request (POST) to the price calculation."""
    customerId: str = Field(min_length=1, description="Customer number the calculation is made for.")
    productNumber: str = Field(min_length=1, description="Product number the calculation is made for.")
    date: Neo4jDate = Field(description="Reference date of the calculation, decisive for the validity of contracts and discounts.")


class Discount(BaseModel):
    """The winning discount rule of the line level, as the calculation reports it."""
    type: str = Field(description="Name of the winning rule, e.g. 'ContractDiscount'.")
    percent: float = Field(description="Discount rate of the winning rule in percent.")
    amount: Decimal = Field(description="Discount amount in euro, from basePrice and discount rate.")
    source: str = Field(description="Name and id of the winning rule.")


class PriceCalculationResponse(BaseModel):
    """Response of the price calculation (200) — the line level exclusively.

    The customer discount is deliberately not part of this response: it applies to the
    whole invoice, not to the unit price, and were it here it would take effect a second
    time as soon as the document carries it as an order discount as well.
    """
    productNumber: str = Field(description="Product number the calculation was made for.")
    basePrice: Decimal = Field(description="Standard sales price of the product in euro, before any discount.")
    finalPrice: Decimal = Field(description="Price per unit after the line discount, in euro.")
    discountable: bool = Field(description="Whether the product can be discounted at all.")
    discount: Discount | None = Field(default=None, description="The winning rule of the line level, or null without a discount.")


# --- Revenue report -----------------------------------------------------------

RevenueGroupBy = Literal["customer", "product", "month"]


class RevenueLine(BaseModel):
    """One row of the revenue report (GET), grouped by customer, product or month."""
    group: str = Field(description="Name of the group, depending on groupBy.")
    revenue: Decimal = Field(description="Revenue in euro from invoice lines, after the order discount.")
    totalCost: Decimal | None = Field(default=None, description="Cost price in euro. Null without a determinable cost price.")
    margin: Decimal | None = Field(default=None, description="Revenue minus totalCost in euro. Null without a determinable cost price.")
    marginPercent: float | None = Field(default=None, description="Margin as a percentage of the revenue. Null without a determinable cost price.")
