"""Cypher queries of the document chain: documents, lines, bookings, cancellations and the
goods receipt control.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from neo4j import AsyncSession
from neo4j.exceptions import ConstraintError, Neo4jError
from pydantic import ValidationError

from core.exceptions import (
    BusinessLogicError,
    DatabaseError,
    DuplicateKeyError,
    NotFoundError,
)
from core.neo4j_query import read_many, read_single
from domains.inventory.repository_inventory import movement_params, post_movement
from domains.notifications.repository_notifications import create_quantity_deviation

from ..schemas_sales import (
    SALES_DOCUMENTS,
    CancelledLine,
    DeliveredLine,
    Document,
    DocumentCreate,
    DocumentDetail,
    DocumentLine,
    DocumentLineCreate,
    DocumentStatus,
    DocumentType,
    DocumentUpdate,
    GoodsReceiptCreate,
    GoodsReceiptLineResult,
    GoodsReceiptResult,
    PriceOverrideLine,
)
from ._shared import _cent, _euro
from .rules import (
    _WHOLE_NUMBER_UNIT,
    _delivery_status,
    _fractional_quantity_violations,
    _line_target,
    _movement_type_for,
    _reversal,
)

# Prefix per document type. They form the head of every document number and follow the
# existing stock (QU-2026-0001, PO-2026-0001). There is no document type without a
# prefix — the Literal `DocumentType` and this mapping have to stay congruent.
DOCUMENT_PREFIX: dict[str, str] = {
    "Quote":             "QU",
    "OrderConfirmation": "OC",
    "DeliveryNote":      "DN",
    "Invoice":           "IN",
    "PurchaseOrder":     "PO",
    "GoodsReceipt":      "GR",
}


# A document in this state is concluded and only takes a status change. `cancelled` and
# `partiallyCancelled` can still be set through PATCH /api/documents/{number} (see
# `_cancel_document`), but no field of substance can.
CONCLUDED: frozenset[str] = frozenset({"completed", "cancelled", "partiallyCancelled"})


# Only these two document types may come into existence more than once for the same
# order (several partial deliveries, several partial invoices). Quote and order
# confirmation stay at exactly one document per order.
MULTI_DOCUMENT_TYPES: frozenset[str] = frozenset({"DeliveryNote", "Invoice"})


# The complete document. The lines are collected before the header so the aggregation
# groups over the node `d` and not over a map.
_DOCUMENT_DETAIL_QUERY = """
MATCH (d:Document {number: $number})
OPTIONAL MATCH (d)-[:HAS_LINE]->(l:DocumentLine)
OPTIONAL MATCH (l)-[:OF_PRODUCT]->(p:Product)
// deliveredQuantity: the sum of the quantities of all incoming FULFILS edges, cancelled
// origin lines excluded. The CASE sits deliberately INSIDE the sum() and not in a
// preceding WHERE — a WHERE after this aggregation would throw a line without any (or
// without a valid) FULFILS edge out of the result entirely, instead of keeping it with
// deliveredQuantity=0.
OPTIONAL MATCH (l)<-[:FULFILS]-(f:DocumentLine)
WITH d, l, p, sum(CASE WHEN f IS NOT NULL AND NOT coalesce(f.cancelled, false)
                       THEN f.quantity ELSE 0.0 END) AS deliveredQuantity
WITH d, p, CASE WHEN l IS NULL THEN null ELSE {
    productNumber:     p.number,
    lineNumber:        l.lineNumber,
    label:             p.label,
    unit:              p.unit,
    quantity:          l.quantity,
    unitPriceCent:     l.unitPriceCent,
    discountPercent:   l.discountPercent,
    priceOverridden:   l.priceOverridden,
    hasFixedPrice:     coalesce(l.hasFixedPrice, false),
    cancelled:         coalesce(l.cancelled, false),
    deliveredQuantity: deliveredQuantity,
    openQuantity:      CASE WHEN l.quantity IS NULL THEN null
                            WHEN l.quantity - deliveredQuantity < 0 THEN 0.0
                            ELSE l.quantity - deliveredQuantity END
} END AS line, l
ORDER BY l.sortOrder
WITH d, collect(line) AS lines
// basedOn can point at several predecessors (an invoice at several delivery notes).
// Without this intermediate step the following OPTIONAL MATCH would multiply the row per
// predecessor and the RETURN clause would give the same document back several times.
OPTIONAL MATCH (d)-[:BASED_ON]->(v:Document)
WITH d, lines, v
ORDER BY v.number
WITH d, lines, collect(v.number) AS basedOnNumbers
OPTIONAL MATCH (d)-[:BELONGS_TO_CUSTOMER]->(c:Customer)
OPTIONAL MATCH (d)-[:BELONGS_TO_SUPPLIER]->(s:Supplier)
OPTIONAL MATCH (d)-[:CREATED_BY]->(e:Employee)
OPTIONAL MATCH (d)-[:BELONGS_TO_ORDER]->(o:Order)
RETURN d{.*,
    customer:  CASE WHEN c IS NULL THEN null ELSE {
                   id:      c.id,
                   name:    coalesce(d.customerName,    c.name),
                   street:  coalesce(d.customerStreet,  c.street),
                   city:    coalesce(d.customerCity,    c.city),
                   country: coalesce(d.customerCountry, c.country),
                   vatId:   coalesce(d.customerVatId,   c.vatId)} END,
    supplier:  CASE WHEN s IS NULL THEN null ELSE {id: s.id, name: s.name} END,
    createdBy: CASE WHEN e IS NULL THEN null ELSE {id: e.id, name: e.name} END,
    basedOn:            basedOnNumbers,
    orderProjectNumber: o.projectNumber
} AS document, lines
"""

# Existence of the products and locations of all lines in one query, plus the main
# warehouse as a fallback. `collect` swallows the null cases on its own, only the keys
# that are genuinely missing remain.
#
# `stockEffects` carries each product's `Product.stockEffect` along — needed by
# `_create_document_in_tx` to decide what a line books against (`_line_target`). For
# callers without `line.productNumber` (the goods receipt control addresses by line
# number) the field stays empty and is simply not read there.
#
# `units` carries each product's `Product.unit` along — needed to check across all
# document types that a product measured in pieces is only ordered and sold in whole
# numbers.
_LINE_CONTEXT_QUERY = """
OPTIONAL MATCH (h:Location {type: 'Warehouse'})
WITH collect(h.id)[0] AS mainWarehouseId
UNWIND $lines AS line
OPTIONAL MATCH (p:Product  {number: line.productNumber})
OPTIONAL MATCH (l:Location {id: line.locationId})
RETURN mainWarehouseId,
       collect(DISTINCT CASE WHEN p IS NULL THEN line.productNumber END) AS missingProducts,
       collect(DISTINCT CASE WHEN line.locationId IS NOT NULL AND l IS NULL
                             THEN line.locationId END)                   AS missingLocations,
       collect(DISTINCT CASE WHEN p IS NULL THEN null
                    ELSE {productNumber: p.number, stockEffect: coalesce(p.stockEffect, 'direct')}
               END)                                                      AS stockEffects,
       collect(DISTINCT CASE WHEN p IS NULL THEN null
                    ELSE {productNumber: p.number, unit: p.unit}
               END)                                                      AS units
"""

# Resolves every asset belonging to an issue line with `Product.stockEffect ==
# 'billOfMaterials'` through the predecessor chain: the predecessor of the document being
# created (on a delivery note the order confirmation itself) carries the AssetInstance per
# piece through BASED_ON_DOCUMENT. Their components are summed per product across all
# assets — several units can carry the same spare part — so a delivery of several pieces
# can be issued in one go.
_BOM_ISSUE_CONTEXT_QUERY = """
MATCH (predecessor:Document {number: $predecessorNumber})-[:BASED_ON*0..]->(oc:Document {type: 'OrderConfirmation'})
MATCH (oc)<-[:BASED_ON_DOCUMENT]-(a:AssetInstance)-[:BASED_ON]->(:Product {number: $productNumber})
WITH collect(a) AS assets
CALL (assets) {
    UNWIND assets AS a
    OPTIONAL MATCH (a)-[rel:HAS_COMPONENT]->(ci:ComponentInstance)-[:IS_TYPE]->(comp:Product)
    WHERE ci.status = 'active'
    WITH comp, sum(rel.quantity) AS quantity
    WHERE comp IS NOT NULL
    RETURN collect({productNumber: comp.number, quantity: quantity}) AS components
}
RETURN size(assets) AS assetCount,
       [a IN assets WHERE NOT coalesce(a.bomReleased, false) | a.serialNumber] AS notReleased,
       components
"""

# Creates the lines of a document that already exists. The key of the line is
# `documentNumber_lineNumber` and not `documentNumber_productNumber` — only that way can
# the same product appear more than once on a sales document. `sortOrder` is a pure
# display field in steps of ten, so a line can later be inserted between two others
# without renumbering the existing ones.
_LINES_QUERY = """
MATCH (d:Document {number: $number})
UNWIND range(0, size($lines)-1) AS idx
WITH d, $lines[idx] AS line, idx
MATCH (p:Product {number: line.productNumber})
CREATE (l:DocumentLine {id: $number + '_' + toString(line.lineNumber)})
SET l.lineNumber      = line.lineNumber,
    l.sortOrder       = line.lineNumber * 10,
    l.quantity        = line.quantity,
    l.unitPriceCent   = line.unitPriceCent,
    l.discountPercent = line.discountPercent,
    l.priceOverridden = line.priceOverridden,
    l.hasFixedPrice   = line.hasFixedPrice
CREATE (d)-[:HAS_LINE]->(l)
CREATE (l)-[:OF_PRODUCT]->(p)
"""

# Links a newly created line with the line it fulfils: goods receipt -> purchase order
# line, delivery note -> invoice line. Both lines already exist in the same transaction;
# a MATCH instead of a MERGE makes sure a wrong reference fails loudly instead of
# creating a ghost line.
_FULFILS_QUERY = """
UNWIND $fulfilments AS f
MATCH (source:DocumentLine {id: f.sourceId})
MATCH (target:DocumentLine {id: f.targetId})
CREATE (source)-[:FULFILS]->(target)
"""

# Records a manual price change. Runs after _LINES_QUERY, in the same transaction — the
# DocumentLine already exists at that point. oldPrice is the standard sales price of the
# product at the time of the change, not the value that stood on the line before: on a
# new line there was none yet.
_PRICE_OVERRIDES_QUERY = """
UNWIND $overrides AS o
MATCH (l:DocumentLine {id: $number + '_' + toString(o.lineNumber)})
MATCH (p:Product {number: o.productNumber})
MATCH (e:Employee {id: $employeeId})
CREATE (po:PriceOverride {
    reason:   o.reason,
    oldPrice: p.listPriceCent,
    newPrice: o.unitPriceCent,
    createdAt: $now
})
CREATE (l)-[:HAS_PRICE_OVERRIDE]->(po)
CREATE (po)-[:RECORDED_BY]->(e)
"""

# Reads the recorded price changes of a document. OPTIONAL MATCH on the document itself,
# so an unknown document (documentExists=false) stays distinguishable from a known
# document without any price change (rows=[]) — both would otherwise return the same
# empty result set.
#
# Sorted and addressed by line number, not by product number: the same product may appear
# more than once on a sales document, and then a row can no longer be assigned to one line
# by product number alone. The caller needs that assignment to reconnect a loaded line
# with its reason — without it every follow-up document fails the validation of
# `DocumentLineCreate`, which demands a reason for `priceOverridden=True`.
_PRICE_OVERRIDES_READ_QUERY = """
OPTIONAL MATCH (d:Document {number: $number})
CALL (d) {
    WITH d
    OPTIONAL MATCH (d)-[:HAS_LINE]->(l:DocumentLine)-[:HAS_PRICE_OVERRIDE]->(po:PriceOverride)-[:RECORDED_BY]->(e:Employee)
    OPTIONAL MATCH (l)-[:OF_PRODUCT]->(p:Product)
    WITH l, p, po, e
    WHERE po IS NOT NULL
    WITH l, p, po, e ORDER BY l.lineNumber
    RETURN collect({
        lineNumber:    l.lineNumber,
        productNumber: p.number,
        oldPriceCent:  po.oldPrice,
        newPriceCent:  po.newPrice,
        reason:        po.reason,
        createdAt:     po.createdAt,
        employee:      CASE WHEN e IS NULL THEN null ELSE {id: e.id, name: e.name} END
    }) AS rows
}
RETURN d IS NOT NULL AS documentExists, rows
"""

# Everything a cancellation needs, in one query: the customer for the reversal, whether an
# active follow-up document blocks the chain, the line numbers of the actual lines (which
# `cancelledLines` is checked against) and the stock movements that need a reversal. Four
# independent questions about the same document, hence four separate CALL subqueries
# instead of one shared MATCH — otherwise several follow-up documents and several bookings
# would multiply crosswise.
_CANCEL_CONTEXT_QUERY = """
MATCH (d:Document {number: $number})
CALL (d) {
    OPTIONAL MATCH (d)-[:BELONGS_TO_CUSTOMER]->(c:Customer)
    RETURN c.id AS customerId
}
CALL (d) {
    // The whole chain downwards, not only the immediate follow-up: a delivery note two
    // steps further down is just as much an active reason to block the predecessor.
    OPTIONAL MATCH (d)<-[:BASED_ON*1..]-(successor:Document)
    WHERE NOT successor.status IN ['cancelled', 'partiallyCancelled']
    RETURN count(DISTINCT successor) AS activeSuccessors
}
CALL (d) {
    MATCH (d)-[:HAS_LINE]->(l:DocumentLine)-[:OF_PRODUCT]->(p:Product)
    RETURN collect({lineNumber: l.lineNumber, productNumber: p.number}) AS lines
}
CALL (d) {
    // Only genuine stock effect, no correction that is already there — a correction
    // created by hand through POST /api/stock-movements with a document reference is a
    // decision of its own and does not automatically belong to the cancellation.
    // `lineNumber` on the movement allows a line-level reversal, now that the same
    // product can appear more than once on a sales document — filtering by product
    // number alone would otherwise also hit a sister line that was not cancelled.
    MATCH (d)<-[:BASED_ON_DOCUMENT]-(m:StockMovement)-[:POSTED_TO]->(sl:StockLevel)-[:AT_LOCATION]->(loc:Location)
    MATCH (p:Product)-[:HAS_STOCK]->(sl)
    WHERE m.type <> 'Correction'
    RETURN collect({productNumber: p.number, locationId: loc.id, type: m.type,
                    quantity: m.quantity, lineNumber: m.lineNumber}) AS bookings
}
RETURN d.status AS status, customerId, activeSuccessors, lines, bookings
"""

# Marks the given line numbers as cancelled. On status='cancelled' that is every line, on
# 'partiallyCancelled' only the ones from cancelledLines.
_CANCEL_LINES_QUERY = """
MATCH (d:Document {number: $number})-[:HAS_LINE]->(l:DocumentLine)
WHERE l.lineNumber IN $targetLines
SET l.cancelled = true
"""

# Partial cancellation of a quantity: the line stays and only loses the cancelled share.
# Deliberately NOT `cancelled = true` — the line is still part of what the customer owes
# (`_with_amounts` leaves cancelled lines out of the totals). Should the quantity drop to
# 0, the line is effectively cancelled entirely; the caller handles that case as a full
# line cancellation beforehand, so it cannot occur here any more.
_REDUCE_LINE_QUANTITY_QUERY = """
UNWIND $reductions AS r
MATCH (d:Document {number: $number})-[:HAS_LINE]->(l:DocumentLine)
WHERE l.lineNumber = r.lineNumber
SET l.quantity = l.quantity - r.quantity
"""

# Everything needed to build the invoice of a delivery note automatically: customer,
# order reference, language and tax rate from the header, plus every line that is not
# cancelled. No price calculation of its own takes place — the line takes over the price
# the delivery note already carried. A line whose price was overridden by hand brings its
# reason along, otherwise the new line fails its own validation, which demands exactly
# that.
_DELIVERY_NOTE_FOLLOW_UP_QUERY = """
MATCH (d:Document {number: $number})
OPTIONAL MATCH (d)-[:BELONGS_TO_CUSTOMER]->(c:Customer)
OPTIONAL MATCH (d)-[:BELONGS_TO_ORDER]->(o:Order)
MATCH (d)-[:HAS_LINE]->(l:DocumentLine)-[:OF_PRODUCT]->(p:Product)
WHERE coalesce(l.cancelled, false) = false
OPTIONAL MATCH (l)-[:HAS_PRICE_OVERRIDE]->(po:PriceOverride)
// openQuantity as in _DOCUMENT_DETAIL_QUERY: quantity minus whatever is already invoiced
// through FULFILS edges. Without that subtraction, concluding a partially delivered
// delivery note would charge the full quantity a second time.
OPTIONAL MATCH (l)<-[:FULFILS]-(f:DocumentLine)
WITH c, o, d, l, p, po,
     sum(CASE WHEN f IS NOT NULL AND NOT coalesce(f.cancelled, false)
              THEN f.quantity ELSE 0.0 END) AS deliveredQuantity
WITH c, o, d, l, p, po, deliveredQuantity ORDER BY l.lineNumber
RETURN c.id AS customerId,
       o.projectNumber AS orderProjectNumber,
       d.language AS language,
       d.taxPercent AS taxPercent,
       collect({
           lineNumber:      l.lineNumber,
           productNumber:   p.number,
           quantity:        l.quantity,
           openQuantity:    CASE WHEN l.quantity - deliveredQuantity < 0 THEN 0.0
                                 ELSE l.quantity - deliveredQuantity END,
           reason:          po.reason,
           unitPriceCent:   l.unitPriceCent,
           discountPercent: l.discountPercent,
           priceOverridden: l.priceOverridden,
           hasFixedPrice:   coalesce(l.hasFixedPrice, false)
       }) AS lines
"""


class DocumentRepository:
    """Repository for documents, document lines and the goods receipt control.

    The document header carries only a few properties in the graph; everything else hangs
    off edges — lines, business partner, order, predecessor and creator. The totals are
    stored nowhere.

    The bookings of a document come into existence in the same transaction as the document
    itself: the document has to exist already when the movement looks for its relationship
    to it, and a document without its bookings would be a state no endpoint straightens
    out again.
    """

    @staticmethod
    def _to_line(record: dict) -> DocumentLine:
        """Converts a result row of a document line into a Pydantic schema.

        Converts the unit price, stored internally as integer cents, into a euro Decimal.
        A cent value of 0 is taken over as a recorded 0.00 EUR and not swallowed into None.

        `amount` stays empty: the line amount is calculated, not stored, and comes into
        existence in the calculation function of the service.

        Args:
            record (dict): The projected result row of a line.

        Returns:
            DocumentLine: The validated line without its calculated amount.

        Raises:
            DatabaseError: When the row contains data the read model cannot map.
        """
        props = dict(record)
        props["unitPrice"] = _euro(props.pop("unitPriceCent", None))

        try:
            return DocumentLine.model_validate(props)
        except ValidationError as e:
            raise DatabaseError(
                f"Document line '{props.get('productNumber')}' could not be read from the "
                f"graph: {e}"
            ) from e

    @staticmethod
    def _to_document(record: dict) -> Document:
        """Converts a result row of the document list into a Pydantic schema.

        Expects the document header including business partner, predecessor, project
        number and the net amount already summed in the query.

        Args:
            record (dict): The projected result row of a document.

        Returns:
            Document: The validated document for the list view.

        Raises:
            DatabaseError: When the row contains data the read model cannot map.
        """
        props = dict(record)
        props["totalNet"] = _euro(props.pop("totalNetCent", None))

        try:
            return Document.model_validate(props)
        except ValidationError as e:
            raise DatabaseError(
                f"Document '{props.get('number')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    def _to_document_detail(record: dict, lines: list[DocumentLine]) -> DocumentDetail:
        """Assembles the detail view of a document from header and lines.

        Contains exclusively what is stored. The four totals and the line amounts stay
        empty — they are calculated and come into existence in the service, which then
        fills them in.

        Args:
            record (dict): The projected document header including customer, creator and
                order.
            lines (list[DocumentLine]): The already converted lines.

        Returns:
            DocumentDetail: The document without the calculated fields.

        Raises:
            DatabaseError: When the data cannot be mapped by the read model.
        """
        props = dict(record)
        props["lines"] = lines

        try:
            return DocumentDetail.model_validate(props)
        except ValidationError as e:
            raise DatabaseError(
                f"Document '{props.get('number')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    def _from_detail_record(record) -> DocumentDetail:
        """Builds the detail view from a result row of the detail query.

        A function of its own, because the same row comes into existence in three places:
        on read, after creation and after an update.
        """
        lines = [DocumentRepository._to_line(line) for line in record["lines"]]
        return DocumentRepository._to_document_detail(record["document"], lines)

    @staticmethod
    async def get_documents(
        session: AsyncSession,
        type: DocumentType | None = None,
        status: DocumentStatus | None = None,
        customer_id: str | None = None,
        supplier_id: str | None = None,
        search: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[Document]:
        """Fetches the document list, optionally filtered.

        Assembles the WHERE clause dynamically from the filters that are set; only
        fragments hard-coded in this module reach the query string, every value from the
        client is bound as a parameter. The filters are additive.

        The net amount is calculated in the query and rounded per line — the same order as
        in the calculation function of the service, so list and detail view report the
        same amount.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            type (DocumentType | None): Only documents of this type.
            status (DocumentStatus | None): Only documents in this processing state.
            customer_id (str | None): Only documents of this customer.
            supplier_id (str | None): Only documents of this supplier.
            search (str | None): Free text across document number and business partner name.
            from_date (date | None): Lower bound of the document date, inclusive.
            to_date (date | None): Upper bound of the document date, inclusive.

        Returns:
            list[Document]: The documents found, or an empty list.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        conditions: list[str] = []
        params: dict = {}

        if type:
            conditions.append("d.type = $type")
            params["type"] = type
        if status:
            conditions.append("d.status = $status")
            params["status"] = status
        if customer_id:
            conditions.append("c.id = $customerId")
            params["customerId"] = customer_id
        if supplier_id:
            conditions.append("s.id = $supplierId")
            params["supplierId"] = supplier_id
        if search and search.strip():
            conditions.append(
                "(toLower(d.number) CONTAINS toLower($search)"
                " OR toLower(c.name) CONTAINS toLower($search)"
                " OR toLower(s.name) CONTAINS toLower($search))"
            )
            params["search"] = search.strip()
        if from_date:
            conditions.append("d.date >= $fromDate")
            params["fromDate"] = from_date
        if to_date:
            conditions.append("d.date <= $toDate")
            params["toDate"] = to_date

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        try:
            query = f"""
            MATCH (d:Document)
            OPTIONAL MATCH (d)-[:HAS_LINE]->(l:DocumentLine)
            WITH d,
                 sum(round(coalesce(l.quantity, 0.0)
                           * coalesce(l.unitPriceCent, 0)
                           * (1 - coalesce(l.discountPercent, 0.0) / 100.0))) AS subtotalCent,
                 // Fixed-price lines contribute to the subtotal but not to the basis of
                 // the order discount — the fixed price takes precedence. See `_totals`
                 // in service_sales.py for the same rule.
                 sum(CASE WHEN coalesce(l.hasFixedPrice, false) THEN 0
                          ELSE round(coalesce(l.quantity, 0.0)
                                     * coalesce(l.unitPriceCent, 0)
                                     * (1 - coalesce(l.discountPercent, 0.0) / 100.0))
                     END) AS discountableCent
            WITH d, subtotalCent,
                 round(discountableCent * coalesce(d.orderDiscountPercent, 0.0) / 100.0) AS discountCent
            OPTIONAL MATCH (d)-[:BELONGS_TO_CUSTOMER]->(c:Customer)
            OPTIONAL MATCH (d)-[:BELONGS_TO_SUPPLIER]->(s:Supplier)
            // basedOn can point at several predecessors (an invoice over several delivery
            // notes). collect() gathers the rows back to one per document before the WHERE
            // clause — otherwise such a document would appear several times in the list,
            // once per predecessor.
            OPTIONAL MATCH (d)-[:BASED_ON]->(v:Document)
            WITH d, subtotalCent, discountCent, c, s, collect(v.number) AS basedOnNumbers
            {where_clause}
            OPTIONAL MATCH (d)-[:BELONGS_TO_ORDER]->(o:Order)
            RETURN d{{.number, .type, .status, .date, .deliveryDate, .language, .assetPurpose,
                customer: CASE WHEN c IS NULL THEN null ELSE {{id: c.id, name: coalesce(d.customerName, c.name)}} END,
                supplier: CASE WHEN s IS NULL THEN null ELSE {{id: s.id, name: s.name}} END,
                basedOn:            basedOnNumbers,
                orderProjectNumber: o.projectNumber,
                totalNetCent:       toInteger(subtotalCent - discountCent)
            }} AS document
            ORDER BY d.date DESC, d.number
            """
            records = await read_many(session, query, **params)
            return [DocumentRepository._to_document(record["document"]) for record in records]
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the documents: {e}") from e

    @staticmethod
    async def get_document(number: str, session: AsyncSession) -> DocumentDetail | None:
        """Fetches a document with all its lines.

        The lines are delivered in their sort order: relationships in the graph have no
        order, and `lineNumber` alone would no longer be the display order after an
        insertion.

        Args:
            number (str): The document number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            DocumentDetail | None: The complete document, or None when no document with
                that number exists.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            record = await read_single(session, _DOCUMENT_DETAIL_QUERY, number=number)
            if record is None:
                return None
            return DocumentRepository._from_detail_record(record)
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the document: {e}") from e

    @staticmethod
    def _to_price_override(record: dict) -> PriceOverrideLine:
        """Converts a result row of the price change query into a Pydantic schema.

        Args:
            record (dict): The projected result row of a price change.

        Returns:
            PriceOverrideLine: The validated price change.

        Raises:
            DatabaseError: When the row contains data the read model cannot map.
        """
        props = dict(record)
        props["oldPrice"] = _euro(props.pop("oldPriceCent", None))
        props["newPrice"] = _euro(props.pop("newPriceCent", None))

        try:
            return PriceOverrideLine.model_validate(props)
        except ValidationError as e:
            raise DatabaseError(
                f"Price change for product '{props.get('productNumber')}' could not be "
                f"read from the graph: {e}"
            ) from e

    @staticmethod
    async def get_price_overrides(
        number: str, session: AsyncSession
    ) -> list[PriceOverrideLine] | None:
        """Fetches the recorded manual price changes of a document.

        Only lines with `priceOverridden=True` leave an entry — a document without a
        manual price change returns an empty list, not `None`. `None` stands exclusively
        for an unknown document.

        Args:
            number (str): The document number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[PriceOverrideLine] | None: The price changes in line number order, or
                `None` when no document with that number exists.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            record = await read_single(session, _PRICE_OVERRIDES_READ_QUERY, number=number)
            if record is None or not record["documentExists"]:
                return None
            return [DocumentRepository._to_price_override(row) for row in record["rows"]]
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the price changes: {e}") from e

    @staticmethod
    async def _issue_bill_of_materials(
        tx,
        *,
        predecessor_number: str | None,
        product_number: str,
        line_quantity: float,
        line_number: int,
        document_number: str,
        location_id: str,
        customer_id: str | None,
    ) -> None:
        """Issues a line with `Product.stockEffect == 'billOfMaterials'` against the
        components of its assets instead of against the product itself.

        Resolves every `AssetInstance` belonging to the line through the predecessor chain
        (`_BOM_ISSUE_CONTEXT_QUERY`) and books one issue per component product found, with
        the quantity summed across all assets.

        **No delivery note without a release:** if even one of the assets involved is not
        released, the whole line — and with it the whole document, since everything runs
        in one transaction — is rejected before anything is booked.

        **Deliberately not built for partial deliveries of INDIVIDUAL assets:** always
        addressed are all assets belonging to this predecessor and product, not a subset —
        `line_quantity` therefore has to match the number of assets found. Several delivery
        notes per order confirmation are possible in general, but that does not solve WHICH
        of several assets of the same line a second delivery note would cover; that would
        need a marker per `AssetInstance` recording which one has already shipped, and
        there is none. A second delivery note against the same bill-of-materials line
        therefore still fails this equality check — a deliberate, loudly failing behaviour
        instead of a silent double shipment.

        Args:
            tx: The running Neo4j write transaction.
            predecessor_number (str | None): The first element of the new document's
                `basedOn` (a bill-of-materials line always has exactly one order
                confirmation predecessor).
            product_number (str): Product number of the line.
            line_quantity (float): The quantity given on the line.
            line_number (int): Line number of the triggering line, for the line-level
                reversal.
            document_number (str): Number of the document being created.
            location_id (str): Location booked against.
            customer_id (str | None): Customer of the new document.

        Raises:
            BusinessLogicError: When the document carries no `basedOn`, the number of
                assets found does not match the line quantity, or at least one asset is
                not released yet.
            NotFoundError: When no asset exists for this product and predecessor.
        """
        if predecessor_number is None:
            raise BusinessLogicError(
                f"Line '{product_number}' has Product.stockEffect='billOfMaterials', but "
                "the document carries no basedOn. Without a predecessor no asset can be "
                "derived."
            )

        context = await (await tx.run(_BOM_ISSUE_CONTEXT_QUERY, {
            "predecessorNumber": predecessor_number,
            "productNumber": product_number,
        })).single()

        if context is None or context["assetCount"] == 0:
            raise NotFoundError(
                f"No asset exists for product '{product_number}' through the predecessor "
                f"'{predecessor_number}'."
            )
        if context["notReleased"]:
            raise BusinessLogicError(
                f"Asset(s) {', '.join(sorted(context['notReleased']))} for product "
                f"'{product_number}' are not released. No delivery note without a release."
            )
        if context["assetCount"] != line_quantity:
            raise BusinessLogicError(
                f"Line '{product_number}' names {line_quantity} pieces, but "
                f"{context['assetCount']} assets exist for the predecessor "
                f"'{predecessor_number}'."
            )

        for component in context["components"]:
            await post_movement(
                tx,
                movement_params(
                    component["productNumber"],
                    location_id,
                    "Issue",
                    component["quantity"],
                    document_number=document_number,
                    line_number=line_number,
                    customer_id=customer_id,
                ),
            )

    @staticmethod
    async def _create_document_in_tx(tx, document_data: DocumentCreate, employee_id: str) -> dict:
        """Checks, numbers, creates a document with its lines and books — all or nothing,
        inside an already running write transaction.

        The core of `create_document`, factored out as a method of its own:
        `update_document` calls the same logic inside its own transaction when a delivery
        note switches to `completed` and its invoice comes into existence automatically —
        both need the check and the write in one transaction, not in two separate
        `execute_write` calls.

        If the new document carries a `basedOn`, every predecessor named in it is set to
        `status = 'completed'` in the same transaction and thereby locked in substance —
        but only as far as they are still `'open'`. A predecessor already carrying another
        state keeps it: a partially delivered delivery note stays `'partiallyDelivered'`
        and therefore open across the invoice over the partial quantity.

        An order confirmation starts as `status = 'completed'` itself rather than `'open'`:
        it IS the confirmation of the order, and a further manual click would only be an
        avoidable second confirmation before the warehouse sees it as an open request to
        deliver. It is therefore locked in substance from creation like any other concluded
        document — an order confirmation entered wrongly needs the same correction path as
        any other: cancel and create anew.

        Args:
            tx: The running Neo4j write transaction.
            document_data (DocumentCreate): Document header and lines.
            employee_id (str): Id of the creating employee, from the auth token.

        Returns:
            dict: The result row of `_DOCUMENT_DETAIL_QUERY` (`document`, `lines`).

        Raises:
            NotFoundError: When customer, supplier, employee, product, location or
                predecessor do not exist.
            BusinessLogicError: When the stock is not enough for an issue, or a line with
                a product measured in pieces carries a fractional quantity.
            DatabaseError: When the detail query returns no result.
        """
        now = datetime.now(UTC)
        props = {
            "type":                 document_data.type,
            # The document date is the day of creation and is not taken from the request:
            # it is printed on the document.
            "date":                 now.date(),
            "status":               "completed" if document_data.type == "OrderConfirmation" else "open",
            "language":             document_data.language,
            "deliveryDate":         document_data.deliveryDate,
            "orderDiscountPercent": document_data.orderDiscountPercent,
            "taxPercent":           document_data.taxPercent,
            "assetPurpose":         document_data.assetPurpose,
            "createdAt":            now,
        }
        # The line number is the key component and is assigned consecutively in the order
        # the client sent, starting at 1. It stays stable after creation.
        lines = [
            {
                "lineNumber":      idx,
                "productNumber":   line.productNumber,
                "quantity":        line.quantity,
                "unitPriceCent":   _cent(line.unitPrice),
                "discountPercent": line.discountPercent,
                "priceOverridden": line.priceOverridden,
                "hasFixedPrice":   line.hasFixedPrice,
                "locationId":      line.locationId,
            }
            for idx, line in enumerate(document_data.lines, start=1)
        ]
        params: dict = {
            "props":              props,
            "lines":              lines,
            "type":               document_data.type,
            "customerId":         document_data.customerId,
            # A map or None — the query reads it as $customerAddress.field; on null the
            # access yields null in Cypher as well, and the coalesce then falls back
            # cleanly to the customer node.
            "customerAddress":    (
                document_data.customerAddress.model_dump() if document_data.customerAddress else None
            ),
            "supplierId":         document_data.supplierId,
            "employeeId":         employee_id,
            "basedOn":            document_data.basedOn or [],
            "orderProjectNumber": document_data.orderProjectNumber,
            "year":               now.year,
        }

        context = await DocumentRepository._check_document_context(tx, params)
        main_warehouse_id = context["mainWarehouseId"]

        # A product measured in pieces cannot be delivered in part — this holds for every
        # document type: a piece stays a piece, whether bought or sold.
        violations = _fractional_quantity_violations(params["lines"], context["unitPerProduct"])
        if violations:
            raise BusinessLogicError(
                f"Quantity has to be a whole number for unit '{_WHOLE_NUMBER_UNIT}': "
                + ", ".join(violations) + "."
            )

        # There is no separate place that hands out orders or project numbers — a sales
        # document without a project number gets its order here, in the same transaction as
        # the document itself. For sales documents only: a purchasing document hangs off
        # the supplier and belongs to no customer order. Runs only AFTER the context check,
        # so an unknown customerId shows up as a 404 instead of leaving an orphaned order
        # behind (the rollback of the transaction would catch that anyway, but the order of
        # the checks stays in one place).
        if document_data.type in SALES_DOCUMENTS and not params["orderProjectNumber"]:
            params["orderProjectNumber"] = await DocumentRepository._create_order_in_tx(
                tx, params["customerId"], document_data.deliveryDate, now.year
            )

        # Hard-coded fragments the creation query is assembled from. One FOREACH per edge
        # would be the alternative, but it makes the query unreadable — and the existence
        # of every node has already been checked at this point.
        edges = ["MATCH (e:Employee {id: $employeeId}) MERGE (d)-[:CREATED_BY]->(e) WITH d"]
        if document_data.customerId:
            # The address moves onto the document as a copy. A later move of the customer
            # therefore no longer changes retroactively what is printed on invoices already
            # issued. `coalesce` per field: if the client sends a differing address, it
            # applies; for everything else the customer master of right now applies.
            edges.append(
                "MATCH (c:Customer {id: $customerId}) "
                "MERGE (d)-[:BELONGS_TO_CUSTOMER]->(c) "
                "SET d.customerName    = coalesce($customerAddress.name,    c.name), "
                "    d.customerStreet  = coalesce($customerAddress.street,  c.street), "
                "    d.customerCity    = coalesce($customerAddress.city,    c.city), "
                "    d.customerCountry = coalesce($customerAddress.country, c.country), "
                "    d.customerVatId   = coalesce($customerAddress.vatId,   c.vatId) "
                "WITH d"
            )
        if document_data.supplierId:
            edges.append(
                "MATCH (s:Supplier {id: $supplierId}) MERGE (d)-[:BELONGS_TO_SUPPLIER]->(s) WITH d"
            )
        if document_data.basedOn:
            # basedOn is a list (an invoice can point at several delivery notes). UNWIND
            # briefly multiplies the rows for d to one predecessor per list entry;
            # "WITH DISTINCT d" gathers them back into a single row before the next
            # fragment continues. Only a predecessor that is still 'open' gets closed. A
            # document already carrying a decision keeps it: on a partial delivery the
            # delivery note stands at 'partiallyDelivered' or 'backorder' and is therefore
            # still open — the invoice over the partial quantity already delivered must not
            # close it, otherwise the remainder disappears from the outbound list. The same
            # holds for 'partiallyCancelled' and 'cancelled', which would otherwise flatten
            # into a meaningless 'completed'.
            edges.append(
                "UNWIND $basedOn AS predecessorNumber "
                "MATCH (v:Document {number: predecessorNumber}) "
                "MERGE (d)-[:BASED_ON]->(v) "
                "SET v.status = CASE WHEN v.status = 'open' THEN 'completed' ELSE v.status END "
                "WITH DISTINCT d"
            )
        if params["orderProjectNumber"]:
            edges.append(
                "MATCH (o:Order {projectNumber: $orderProjectNumber}) "
                "MERGE (d)-[:BELONGS_TO_ORDER]->(o) WITH d"
            )

        create_query = f"""
        CREATE (d:Document {{number: $number}})
        SET d += $props
        WITH d
        FOREACH (_ IN CASE WHEN $type = 'Quote'             THEN [1] ELSE [] END | SET d:Quote)
        FOREACH (_ IN CASE WHEN $type = 'OrderConfirmation' THEN [1] ELSE [] END | SET d:OrderConfirmation)
        FOREACH (_ IN CASE WHEN $type = 'DeliveryNote'      THEN [1] ELSE [] END | SET d:DeliveryNote)
        FOREACH (_ IN CASE WHEN $type = 'Invoice'           THEN [1] ELSE [] END | SET d:Invoice)
        FOREACH (_ IN CASE WHEN $type = 'PurchaseOrder'     THEN [1] ELSE [] END | SET d:PurchaseOrder)
        FOREACH (_ IN CASE WHEN $type = 'GoodsReceipt'      THEN [1] ELSE [] END | SET d:GoodsReceipt)
        WITH d
        {chr(10).join(edges)}
        RETURN d.number AS number
        """

        number = await DocumentRepository._next_document_number(
            tx, params["type"], params["orderProjectNumber"], params["year"]
        )
        await (await tx.run(create_query, {**params, "number": number})).consume()
        await (await tx.run(_LINES_QUERY, {**params, "number": number})).consume()

        # Only prices overridden by hand leave a PriceOverride — the normal case stays
        # unrecorded.
        overrides = [
            {
                "lineNumber":    raw["lineNumber"],
                "productNumber": line.productNumber,
                "reason":        line.reason,
                "unitPriceCent": _cent(line.unitPrice),
            }
            for line, raw in zip(document_data.lines, lines, strict=True)
            if line.priceOverridden
        ]
        if overrides:
            await (await tx.run(
                _PRICE_OVERRIDES_QUERY,
                {
                    "overrides":  overrides,
                    "number":     number,
                    "employeeId": params["employeeId"],
                    "now":        now,
                },
            )).consume()

        movement_type = _movement_type_for(params["type"], context["predecessorTypes"])
        if movement_type is not None:
            for line, raw in zip(document_data.lines, params["lines"], strict=True):
                stock_effect = context["stockEffectPerProduct"].get(raw["productNumber"], "direct")
                target = _line_target(movement_type, stock_effect)
                if target == "none":
                    continue

                location_id = raw["locationId"] or main_warehouse_id
                if location_id is None:
                    raise BusinessLogicError(
                        f"Line '{raw['productNumber']}' has no location, and no main "
                        "warehouse is stored. Without a place nothing can be booked."
                    )

                if target == "billOfMaterials":
                    # A bill-of-materials line always has exactly one order confirmation
                    # predecessor — the assets behind it come into existence through
                    # exactly one order confirmation. The list practically never carries
                    # more than one element here; the first is therefore the only sensible
                    # one.
                    await DocumentRepository._issue_bill_of_materials(
                        tx,
                        predecessor_number=document_data.basedOn[0] if document_data.basedOn else None,
                        product_number=raw["productNumber"],
                        line_quantity=raw["quantity"],
                        line_number=raw["lineNumber"],
                        document_number=number,
                        location_id=location_id,
                        customer_id=params["customerId"],
                    )
                    continue

                await post_movement(
                    tx,
                    movement_params(
                        raw["productNumber"],
                        location_id,
                        movement_type,
                        raw["quantity"],
                        document_number=number,
                        line_number=raw["lineNumber"],
                        customer_id=params["customerId"],
                        # Only an inbound movement carries a purchase price. On a sales
                        # document the sales price on the movement would be a wrong basis
                        # for the average cost price.
                        purchase_price=(
                            line.unitPrice if movement_type == "Receipt" else None
                        ),
                    ),
                )

        return await (await tx.run(_DOCUMENT_DETAIL_QUERY, {"number": number})).single()

    @staticmethod
    async def create_document(
        document_data: DocumentCreate, employee_id: str, session: AsyncSession
    ) -> DocumentDetail:
        """Creates a document with its lines and books its stock effect.

        Everything runs in a single write transaction: number assignment, document header,
        lines, the relationships to business partner, order, predecessor and creator, and
        every stock booking. If the stock is not enough for an issue, neither document nor
        movement comes into existence. The actual work is done by
        `_create_document_in_tx`, which `update_document` uses from its own transaction as
        well.

        The booking logic itself is shared with the inventory domain and not rebuilt — a
        second version would mean maintaining the invariant between stock and movement
        history in two places.

        Args:
            document_data (DocumentCreate): Document header and lines.
            employee_id (str): Id of the creating employee, from the auth token.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            DocumentDetail: The created document including its number and totals.

        Raises:
            NotFoundError: When customer, supplier, employee, product, location or
                predecessor do not exist.
            BusinessLogicError: When the stock is not enough for an issue, or a line with a
                product measured in pieces carries a fractional quantity.
            DuplicateKeyError: When the assigned document number is already taken.
            DatabaseError: On unexpected errors during the transaction.
        """
        try:
            record = await session.execute_write(
                DocumentRepository._create_document_in_tx, document_data, employee_id
            )
            if record is None:
                raise DatabaseError("Document was not created, Neo4j returned an empty result.")
            return DocumentRepository._from_detail_record(record)
        except ConstraintError as e:
            raise DuplicateKeyError(
                "The document number formed is already taken. Only one document per type "
                "can come into existence for one order."
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while creating the document: {e}") from e

    @staticmethod
    async def _check_document_context(tx, params: dict) -> dict:
        """Checks every referenced node and determines the predecessor chain.

        Runs in the same transaction as the write operation. A check in a query of its own
        beforehand would be a time-of-check-to-time-of-use window: the customer that was
        checked could disappear between the check and the creation.

        Checked in two queries — one for the nodes on the document header including the
        chain of predecessors, one for the products and locations of every line. Both
        report everything that is missing instead of stopping at the first error.

        Args:
            tx: The running Neo4j write transaction.
            params (dict): The bound parameters of the creation.

        Returns:
            dict: `mainWarehouseId` as a fallback, `predecessorTypes` as the document
                types of the whole chain, `stockEffectPerProduct` as product number ->
                `Product.stockEffect`, and `unitPerProduct` as product number ->
                `Product.unit` — each for every line.

        Raises:
            NotFoundError: When one of the referenced nodes does not exist.
        """
        header_query = """
        OPTIONAL MATCH (c:Customer {id: $customerId})
        OPTIONAL MATCH (s:Supplier {id: $supplierId})
        OPTIONAL MATCH (e:Employee {id: $employeeId})
        OPTIONAL MATCH (o:Order    {projectNumber: $orderProjectNumber})
        // $basedOn is a list (empty when there is no predecessor). The whole chain of
        // predecessors, not only the immediate one: a delivery note two steps further up
        // has issued the goods just as much. A CALL subquery of its own keeps the
        // aggregate cleanly apart from the remaining OPTIONAL MATCHes instead of
        // multiplying with them.
        CALL () {
            UNWIND $basedOn AS predecessorNumber
            OPTIONAL MATCH (v:Document {number: predecessorNumber})
            OPTIONAL MATCH (v)-[:BASED_ON*0..]->(chain:Document)
            RETURN collect(DISTINCT CASE WHEN v IS NULL THEN predecessorNumber END) AS missingRaw,
                   collect(DISTINCT chain.type) AS predecessorTypes
        }
        RETURN c IS NOT NULL AS customer,
               s IS NOT NULL AS supplier,
               e IS NOT NULL AS employee,
               o IS NOT NULL AS order,
               [x IN missingRaw WHERE x IS NOT NULL] AS missingPredecessors,
               predecessorTypes
        """

        header = await (await tx.run(header_query, params)).single()
        if header is None:
            raise DatabaseError("The context query returned no result.")

        if params["customerId"] and not header["customer"]:
            raise NotFoundError(f"A customer with the id '{params['customerId']}' does not exist.")
        if params["supplierId"] and not header["supplier"]:
            raise NotFoundError(f"A supplier with the id '{params['supplierId']}' does not exist.")
        if not header["employee"]:
            raise NotFoundError(
                f"An employee with the id '{params['employeeId']}' does not exist."
            )
        if header["missingPredecessors"]:
            missing = ", ".join(sorted(header["missingPredecessors"]))
            raise NotFoundError(f"Document(s) do not exist: {missing}.")
        if params["orderProjectNumber"] and not header["order"]:
            raise NotFoundError(
                f"An order with the project number '{params['orderProjectNumber']}' does "
                "not exist."
            )

        lines = await (await tx.run(_LINE_CONTEXT_QUERY, params)).single()
        if lines is None:
            raise DatabaseError("The context query of the lines returned no result.")

        if lines["missingProducts"]:
            missing = ", ".join(sorted(lines["missingProducts"]))
            raise NotFoundError(f"Products do not exist: {missing}.")
        if lines["missingLocations"]:
            missing = ", ".join(sorted(lines["missingLocations"]))
            raise NotFoundError(f"Locations do not exist: {missing}.")

        return {
            "mainWarehouseId":  lines["mainWarehouseId"],
            "predecessorTypes": header["predecessorTypes"],
            "stockEffectPerProduct": {
                row["productNumber"]: row["stockEffect"]
                for row in lines["stockEffects"]
                if row is not None
            },
            "unitPerProduct": {
                row["productNumber"]: row["unit"]
                for row in lines["units"]
                if row is not None
            },
        }

    @staticmethod
    async def update_document(
        number: str, document_data: DocumentUpdate, employee_id: str, session: AsyncSession
    ) -> DocumentDetail | None:
        """Updates individual fields of a document, cancellation included.

        The check and the write run in the same transaction: whether the document is
        already concluded decides whether the change is allowed at all, and a state read
        separately would already be stale at the moment of writing.

        A concluded document only takes a status change. Anything else would change a
        document already printed and booked after the fact.

        `status: "cancelled"` or `"partiallyCancelled"` additionally triggers a reversal of
        the original stock effect (`_reversal`) and marks the affected `DocumentLine`
        nodes. A document with an active follow-up document that is not already cancelled
        is rejected — cancellation runs backwards through the chain, not automatically as a
        chain reaction.

        If a delivery note switches from another status to `completed`, its invoice comes
        into existence automatically in the same transaction — with the same lines and the
        same customer, `basedOn` the delivery note. The `BASED_ON` edge set by
        `_create_document_in_tx` already locks the delivery note; the closing
        `SET d += $props` of this method then only sets the same status a second time,
        without effect.

        Args:
            number (str): The document number.
            document_data (DocumentUpdate): The fields to change.
            employee_id (str): Id of the employee triggering the change, from the auth
                token. Only needed for an automatically created follow-up invoice
                (`CREATED_BY`).
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            DocumentDetail | None: The updated document, or None when no document with
                that number exists. `createdFollowUpDocument` carries the number of the
                invoice newly created, otherwise `None`.

        Raises:
            BusinessLogicError: When no field was handed over, the document is concluded
                and is to be changed in substance, the document is already cancelled or
                partially cancelled, an active follow-up document exists, or
                `cancelledLines` names a line number that is no line of this document.
            DuplicateKeyError: When two concurrent calls conclude the same delivery note at
                the same time and compete for the same invoice number.
            DatabaseError: On unexpected errors during the transaction.
        """
        props = document_data.model_dump(exclude_unset=True)

        if not props:
            raise BusinessLogicError("No fields to update were handed over.")

        # `model_dump()` has already turned the CancelledLine objects into dicts — turned
        # back into models for `_cancel_document`, so it accesses them typed rather than
        # through key names.
        cancelled_raw = props.pop("cancelledLines", None)
        cancelled_lines_request = (
            [CancelledLine.model_validate(line) for line in cancelled_raw] if cancelled_raw else None
        )
        # Not a node property either: `deliveredQuantities` describes an operation, not a
        # field of the document. Left in `props` the list would land on the document.
        delivered_raw = props.pop("deliveredQuantities", None)
        delivered_quantities_request = (
            [DeliveredLine.model_validate(line) for line in delivered_raw] if delivered_raw else None
        )
        substantive_fields = sorted(set(props) - {"status"})
        props["updatedAt"] = datetime.now(UTC)
        params: dict = {"number": number, "props": props}

        async def _update_document(tx, params):
            """Checks the lock and writes in the same transaction."""
            state = await (await tx.run(
                "MATCH (d:Document {number: $number}) RETURN d.status AS status, d.type AS type",
                params,
            )).single()

            if state is None:
                return None, None

            if state["status"] in CONCLUDED and substantive_fields:
                raise BusinessLogicError(
                    f"Document '{params['number']}' is '{state['status']}' and therefore "
                    f"concluded. No longer changeable: {', '.join(substantive_fields)}. "
                    "Only a status change is still allowed."
                )

            if delivered_quantities_request is not None:
                if state["type"] != "DeliveryNote":
                    raise BusinessLogicError(
                        f"Document '{params['number']}' is a {state['type']}. "
                        "deliveredQuantities exists only on a delivery note."
                    )
                if state["status"] in CONCLUDED:
                    raise BusinessLogicError(
                        f"Delivery note '{params['number']}' is '{state['status']}' and "
                        "therefore concluded. Nothing more can be delivered."
                    )

            # The cancellation runs before the invoice: `_cancel_document` rejects a
            # document with an active follow-up, and the invoice of this step would be
            # exactly one.
            if props.get("status") in ("cancelled", "partiallyCancelled"):
                await DocumentRepository._cancel_document(
                    tx, params["number"], state["status"], props["status"],
                    cancelled_lines_request,
                )

            created_invoice_number = None
            if state["type"] == "DeliveryNote":
                if delivered_quantities_request is not None:
                    created_invoice_number, complete = (
                        await DocumentRepository._invoice_for_delivery(
                            tx, params["number"], employee_id, delivered_quantities_request
                        )
                    )
                    # If the delivery covers the last remainder, the delivery note is done
                    # — regardless of what the caller asked for. Whoever ships the last two
                    # pieces does not want a document that stays at 'partiallyDelivered'
                    # and hangs in the outbound list. 'partiallyCancelled' stays put: there
                    # the remainder is written off, not delivered.
                    if complete and props.get("status") in ("partiallyDelivered", "backorder"):
                        props["status"] = "completed"
                elif state["status"] != "completed" and props.get("status") == "completed":
                    created_invoice_number, _ = (
                        await DocumentRepository._invoice_for_delivery(
                            tx, params["number"], employee_id
                        )
                    )

            await (await tx.run(
                "MATCH (d:Document {number: $number}) SET d += $props", params
            )).consume()
            record = await (await tx.run(_DOCUMENT_DETAIL_QUERY, params)).single()
            return record, created_invoice_number

        try:
            record, created_invoice_number = await session.execute_write(_update_document, params)
            if record is None:
                return None
            document = DocumentRepository._from_detail_record(record)
            document.createdFollowUpDocument = created_invoice_number
            return document
        except ConstraintError as e:
            raise DuplicateKeyError(
                "The document number formed for the automatic follow-up invoice is already "
                "taken. Presumably a concurrent second conclusion of the same document."
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while updating the document: {e}") from e

    @staticmethod
    async def _invoice_for_delivery(
        tx,
        delivery_note_number: str,
        employee_id: str,
        delivered_quantities: list[DeliveredLine] | None = None,
    ) -> tuple[str, bool]:
        """Creates the invoice for a delivery and links it to the delivery note.

        Takes over customer, order reference and the prices one to one from the delivery
        note — no price calculation of its own takes place, the delivery note has already
        fixed the prices. `_movement_type_for` recognises from the predecessor that the
        goods have already been issued and books nothing more for the invoice itself.

        **Which quantity ends up on the invoice** is decided by `delivered_quantities`:

        - `None` — the delivery note is being concluded, the invoice covers the entire
          quantity still **open**. Not `quantity`: a delivery note delivered in part before
          would otherwise carry the pieces already invoiced a second time.
        - a list — a partial delivery, the invoice covers exactly these quantities.

        Every invoice line gets a `FULFILS` edge onto the delivery note line it fulfils.
        From those, `_DOCUMENT_DETAIL_QUERY` and this query compute the `deliveredQuantity`
        and the `openQuantity` of the line. The state is therefore written forward nowhere
        but read from the edges every time — if somebody cancels the invoice, its quantity
        automatically counts as open again.

        Args:
            tx: The running Neo4j write transaction of `update_document`.
            delivery_note_number (str): Number of the delivery note.
            employee_id (str): Id of the employee triggering the delivery.
            delivered_quantities (list[DeliveredLine] | None): The quantities of this
                delivery step, or None for the entire open quantity.

        Returns:
            tuple[str, bool]: The number of the newly created invoice, and whether the
                delivery note is fully delivered with it.

        Raises:
            BusinessLogicError: When the delivery note carries no open line any more, a
                named line number is no open line of this document, or a quantity exceeds
                the open quantity.
            DatabaseError: When the context query returns no result.
        """
        context = await (await tx.run(
            _DELIVERY_NOTE_FOLLOW_UP_QUERY, {"number": delivery_note_number}
        )).single()
        if context is None:
            raise DatabaseError(
                f"The context query for delivery note '{delivery_note_number}' returned no result."
            )
        if not context["lines"]:
            raise BusinessLogicError(
                f"Delivery note '{delivery_note_number}' has no open line left an invoice "
                "could come into existence from automatically."
            )

        open_per_line = {
            line["lineNumber"]: Decimal(str(line["openQuantity"])) for line in context["lines"]
        }
        if delivered_quantities is None:
            to_deliver = {n: quantity for n, quantity in open_per_line.items() if quantity > 0}
        else:
            to_deliver = {line.lineNumber: line.quantity for line in delivered_quantities}
            unknown = sorted(set(to_deliver) - set(open_per_line))
            if unknown:
                raise BusinessLogicError(
                    f"Line numbers {', '.join(str(n) for n in unknown)} are no open line of "
                    f"delivery note '{delivery_note_number}'."
                )
            too_much = sorted(n for n, q in to_deliver.items() if q > open_per_line[n])
            if too_much:
                mentions = ", ".join(
                    f"line {n}: {to_deliver[n]} of {open_per_line[n]}" for n in too_much
                )
                untouched = ", ".join(
                    str(n) for n in sorted(set(open_per_line) - set(to_deliver))
                )
                raise BusinessLogicError(
                    f"The delivered quantity exceeds the open quantity ({mentions}). Only "
                    "what the delivery note still carries can be delivered."
                    + (f" Untouched: line {untouched}." if untouched else "")
                )

        if not to_deliver:
            raise BusinessLogicError(
                f"Delivery note '{delivery_note_number}' has no open quantity left. There "
                "is nothing to invoice."
            )

        # In line number order, so the invoice keeps the order of the delivery note and the
        # FULFILS assignment below runs over the same index.
        sources = [line for line in context["lines"] if to_deliver.get(line["lineNumber"])]
        invoice_lines = []
        for line in sources:
            unit_price = _euro(line["unitPriceCent"])
            # Every line comes from a DocumentLine already created, whose unitPriceCent was
            # mandatory on creation (`DocumentLineCreate.unitPrice` is not optional) — no
            # None can arrive here.
            assert unit_price is not None
            invoice_lines.append(
                DocumentLineCreate(
                    productNumber=line["productNumber"],
                    quantity=float(to_deliver[line["lineNumber"]]),
                    unitPrice=unit_price,
                    discountPercent=line["discountPercent"],
                    priceOverridden=line["priceOverridden"],
                    hasFixedPrice=line["hasFixedPrice"],
                    reason=line["reason"],
                )
            )

        invoice_data = DocumentCreate(
            type="Invoice",
            customerId=context["customerId"],
            language=context["language"] or "DE",
            taxPercent=context["taxPercent"],
            basedOn=[delivery_note_number],
            orderProjectNumber=context["orderProjectNumber"],
            lines=invoice_lines,
        )

        record = await DocumentRepository._create_document_in_tx(tx, invoice_data, employee_id)
        invoice_number = record["document"]["number"]

        # The invoice lines came into existence in the same order as `sources` and
        # therefore carry the numbers 1..N over exactly this list
        # (`_create_document_in_tx` assigns them consecutively). That fixes the assignment
        # without reading it back.
        fulfilments = [
            {
                "sourceId": f"{invoice_number}_{idx}",
                "targetId": f"{delivery_note_number}_{source['lineNumber']}",
            }
            for idx, source in enumerate(sources, start=1)
        ]
        await (await tx.run(_FULFILS_QUERY, {"fulfilments": fulfilments})).consume()

        remainder = sum(
            (open_q - to_deliver.get(n, Decimal(0)) for n, open_q in open_per_line.items()),
            Decimal(0),
        )
        return invoice_number, remainder <= 0

    @staticmethod
    async def _cancel_document(
        tx,
        number: str,
        current_status: str,
        new_status: str,
        cancelled_lines: list[CancelledLine] | None,
    ) -> None:
        """Checks the cancellation prerequisites, marks lines and books the reversal.

        Runs inside the running write transaction of `update_document` — the check (active
        follow-up document, unknown line number) and the write (marking lines, reversing
        stock movements) must not fall apart, otherwise a concurrent request could create
        another follow-up document between the check and the booking.

        The reversal runs per line through `StockMovement.lineNumber`, not through the
        product number: the same product can appear more than once on a sales document,
        and a filter by product number alone would then also hit a sister line that was not
        cancelled.

        **Partial quantities.** If a `CancelledLine` carries a `quantity`, only that share
        is reversed and the line keeps the remainder; without a quantity the whole line is
        cancelled. The case comes from the outbound: of five pieces ordered, two go out and
        the remaining three are written off. Reversing the full booked quantity would leave
        the two pieces already shipped lying in the warehouse on paper.

        A partial quantity equal to the full booked quantity is treated like a full line
        cancellation: the line is marked instead of being set to quantity 0.

        Args:
            tx: The running Neo4j write transaction.
            number (str): The document number.
            current_status (str): The status of the document before this call.
            new_status (str): `"cancelled"` or `"partiallyCancelled"`.
            cancelled_lines (list[CancelledLine] | None): On `"partiallyCancelled"` the
                lines to cancel, each with an optional partial quantity. Disregarded on
                `"cancelled"` — every line is then cancelled entirely.

        Raises:
            BusinessLogicError: When the document is already cancelled or partially
                cancelled, an active follow-up document exists, a named line number is no
                line of this document, or a partial quantity exceeds the booked quantity.
        """
        if current_status in ("cancelled", "partiallyCancelled"):
            raise BusinessLogicError(
                f"Document '{number}' is already '{current_status}'. Cancelling it again "
                "would reverse the stock effect a second time."
            )

        context = await (await tx.run(_CANCEL_CONTEXT_QUERY, {"number": number})).single()
        all_line_numbers = {line["lineNumber"] for line in context["lines"]}

        if context["activeSuccessors"] > 0:
            raise BusinessLogicError(
                f"Document '{number}' has an active (not cancelled) follow-up document and "
                "cannot be cancelled directly. Cancellation runs backwards through the "
                "document chain."
            )

        # Per target line the quantity to cancel, or None for "the whole line".
        if new_status == "partiallyCancelled":
            requested = {line.lineNumber: line.quantity for line in (cancelled_lines or [])}
            unknown = sorted(set(requested) - all_line_numbers)
            if unknown:
                raise BusinessLogicError(
                    f"Line numbers {', '.join(str(n) for n in unknown)} are no line of "
                    f"document '{number}'."
                )
        else:  # cancelled — the whole document, so every line entirely
            requested = {n: None for n in all_line_numbers}

        # The booked quantity per line is the upper bound: giving back more than the
        # document ever booked would mean creating stock out of nothing. Lines without a
        # stock effect (flat fees, assets not released) do not appear here — for them only
        # the marking remains, there is nothing to reverse.
        booked_per_line = {
            b["lineNumber"]: Decimal(str(b["quantity"])) for b in context["bookings"]
        }
        too_much = sorted(
            n for n, quantity in requested.items()
            if quantity is not None and quantity > booked_per_line.get(n, Decimal(0))
        )
        if too_much:
            mentions = ", ".join(
                f"line {n}: {requested[n]} of {booked_per_line.get(n, Decimal(0))}"
                for n in too_much
            )
            raise BusinessLogicError(
                f"The cancelled quantity exceeds the booked quantity ({mentions}). A "
                "cancellation can only give back what the document booked."
            )

        # A partial quantity equal to the full booked quantity is a full line cancellation
        # — otherwise a line with quantity 0 would remain instead of being marked as
        # cancelled.
        cancelled_entirely = [
            n for n, quantity in requested.items()
            if quantity is None or quantity == booked_per_line.get(n)
        ]
        reduced = [
            {"lineNumber": n, "quantity": float(quantity)}
            for n, quantity in requested.items()
            if quantity is not None and quantity != booked_per_line.get(n)
        ]

        if cancelled_entirely:
            await (await tx.run(
                _CANCEL_LINES_QUERY, {"number": number, "targetLines": cancelled_entirely}
            )).consume()
        if reduced:
            await (await tx.run(
                _REDUCE_LINE_QUANTITY_QUERY, {"number": number, "reductions": reduced}
            )).consume()

        for booking in context["bookings"]:
            if booking["lineNumber"] not in requested:
                continue
            partial = requested[booking["lineNumber"]]
            quantity = booking["quantity"] if partial is None else float(partial)
            correction_quantity, target_reserved = _reversal(booking["type"], quantity)
            reversal_params = movement_params(
                booking["productNumber"], booking["locationId"], "Correction",
                correction_quantity,
                document_number=number, line_number=booking["lineNumber"],
                customer_id=context["customerId"],
                note=f"Cancellation reversal for document {number}",
            )
            await post_movement(tx, reversal_params, target_reserved=target_reserved)

    @staticmethod
    async def _next_goods_receipt_number(
        tx, purchase_order_number: str, year: int
    ) -> tuple[str, str]:
        """Forms the number of a goods receipt, with a follow-up delivery suffix.

        The first delivery against a purchase order gets the next free yearly number
        through `_next_document_number` (e.g. `GR-2026-0007`) and is at the same time its
        own base number. Every further delivery against THE SAME purchase order (a
        follow-up delivery, because the first did not bring the full quantity) appends a
        running counter to that base number instead: `GR-2026-0007-2`, `GR-2026-0007-3`, …
        Unlike delivery note and invoice (`MULTI_DOCUMENT_TYPES`, counted through the order
        project number), a purchase order has no project number — the counting therefore
        runs directly over the property `purchaseOrderNumber` kept on every goods receipt.

        `ORDER BY gr.createdAt ASC` makes the choice of the base number deterministic.

        Two concurrent follow-up deliveries against the same purchase order could
        theoretically compute the same number — that is caught by the same unique
        constraint on `Document.number` as every other document number assignment
        (`post_goods_receipt` already translates the resulting `ConstraintError` into a
        `DuplicateKeyError`).

        Args:
            tx: The running Neo4j write transaction.
            purchase_order_number (str): Number of the purchase order booked against.
            year (int): Year for the yearly number of the FIRST delivery.

        Returns:
            tuple[str, str]: `(number, baseNumber)` — on the first delivery both are equal.
        """
        query = """
        MATCH (gr:Document {type: 'GoodsReceipt', purchaseOrderNumber: $purchaseOrderNumber})
        WITH gr ORDER BY gr.createdAt ASC
        RETURN collect(coalesce(gr.baseNumber, gr.number))[0] AS baseNumber,
               count(gr) AS count
        """
        record = await (
            await tx.run(query, {"purchaseOrderNumber": purchase_order_number})
        ).single()
        count = record["count"] if record else 0
        if count == 0:
            number = await DocumentRepository._next_document_number(
                tx, "GoodsReceipt", None, year
            )
            return number, number
        base_number = record["baseNumber"]
        return f"{base_number}-{count + 1}", base_number

    @staticmethod
    async def post_goods_receipt(
        number: str,
        goods_receipt_data: GoodsReceiptCreate,
        employee_id: str,
        session: AsyncSession,
    ) -> GoodsReceiptResult:
        """Books a delivery against a purchase order and compares ordered with received.

        Creates the goods receipt document in one transaction, links it to the purchase
        order and writes one inbound booking per line. The bookings keep their reference to
        the **purchase order**: only through it does the ordered/received comparison find
        them again, including those of earlier deliveries.

        Idempotent over `deliveryNoteNumber`: if a goods receipt with the same delivery note
        number already exists for the purchase order, this call books nothing new but
        returns the result of the first booking unchanged. Two genuine partial deliveries
        stay possible, because they carry different delivery note numbers.

        **Follow-up deliveries carry a derived document number**
        (`_next_goods_receipt_number`).

        **Sets the purchase order to `status='completed'` automatically** as soon as no
        line of it is open after this booking (complete or over-delivered; cancelled lines
        count as done) — before that it stays `open`. A document already cancelled or
        partially cancelled is not overwritten.

        If a line has no purchase price, the one agreed on the purchase order line applies.
        Without a price on the booking the receipt would stay invisible in the cost price
        calculation.

        Args:
            number (str): The number of the purchase order.
            goods_receipt_data (GoodsReceiptCreate): The checked lines.
            employee_id (str): Id of the recording employee, from the auth token.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            GoodsReceiptResult: The ordered/received comparison across every line of the
                purchase order, plus the number of the goods receipt created and the
                (possibly automatically updated) purchase order status.

        Raises:
            NotFoundError: When the purchase order or the employee do not exist.
            BusinessLogicError: When the document is no purchase order, a referenced line
                number is no line of the purchase order, or no main warehouse is stored.
            DuplicateKeyError: When two concurrent calls with the same delivery note number
                both miss the idempotency short circuit.
            DatabaseError: On unexpected errors during the transaction.
        """
        now = datetime.now(UTC)
        params: dict = {
            "number":             number,
            "employeeId":         employee_id,
            "deliveryNoteNumber": goods_receipt_data.deliveryNoteNumber,
            # The price is converted to cents here already: a `Decimal` among the bound
            # parameters would be rejected by the driver outright. The purchase order line
            # is addressed by its line number, not by the product number — product and
            # agreed price follow server-side from the purchase order line.
            "lines": [
                {
                    "lineNumber":        line.lineNumber,
                    "quantity":          line.quantity,
                    "purchasePriceCent": _cent(line.purchasePrice),
                }
                for line in goods_receipt_data.lines
            ],
            "year": now.year,
        }

        purchase_order_query = """
        MATCH (d:Document {number: $number})
        OPTIONAL MATCH (d)-[:BELONGS_TO_SUPPLIER]->(s:Supplier)
        OPTIONAL MATCH (e:Employee {id: $employeeId})
        OPTIONAL MATCH (d)-[:HAS_LINE]->(l:DocumentLine)-[:OF_PRODUCT]->(p:Product)
        WITH d, s, e, collect({
            lineNumber:    l.lineNumber,
            productNumber: p.number,
            unitPriceCent: l.unitPriceCent
        }) AS purchaseOrderLines
        RETURN d.type        AS type,
               d.status      AS status,
               s.id          AS supplierId,
               e IS NOT NULL AS employeeFound,
               purchaseOrderLines
        """

        # Idempotency: a goods receipt document carries purchaseOrderNumber and
        # deliveryNoteNumber redundantly as its own properties, although purchaseOrderNumber
        # would also be reachable through the BASED_ON edge — only properties on the same
        # node can form a unique constraint, an edge cannot. If this lookup already finds a
        # goods receipt, the call is a repeat: nothing is booked, the result of the first
        # booking is returned directly.
        already_booked_query = """
        MATCH (gr:GoodsReceipt {purchaseOrderNumber: $number, deliveryNoteNumber: $deliveryNoteNumber})
        RETURN gr.number AS goodsReceiptNumber
        """

        # The goods receipt document records the delivery. It takes the supplier over from
        # its predecessor, so the purchasing filter finds it just as it finds the purchase
        # order.
        goods_receipt_query = """
        CREATE (gr:Document {number: $goodsReceiptNumber})
        SET gr += $props
        WITH gr
        SET gr:GoodsReceipt
        WITH gr
        MATCH (e:Employee {id: $employeeId})
        MERGE (gr)-[:CREATED_BY]->(e)
        WITH gr
        MATCH (po:Document {number: $number})
        MERGE (gr)-[:BASED_ON]->(po)
        WITH gr
        OPTIONAL MATCH (s:Supplier {id: $supplierId})
        FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
            MERGE (gr)-[:BELONGS_TO_SUPPLIER]->(s))
        """

        # The ordered/received comparison. It runs over the FULFILS edges of the purchase
        # order line, not over the stock movements directly — the same deliveredQuantity
        # projection as in _DOCUMENT_DETAIL_QUERY, and therefore robust even if a later
        # extension allowed several lines of the same product on a purchase order.
        comparison_query = """
        MATCH (po:Document {number: $number})-[:HAS_LINE]->(line:DocumentLine)
              -[:OF_PRODUCT]->(p:Product)
        OPTIONAL MATCH (line)<-[:FULFILS]-(grLine:DocumentLine)
        WITH line, p, sum(CASE WHEN grLine IS NOT NULL AND NOT coalesce(grLine.cancelled, false)
                               THEN grLine.quantity ELSE 0.0 END) AS receivedQuantity
        RETURN line.lineNumber AS lineNumber,
               p.number        AS productNumber,
               p.label         AS label,
               line.quantity   AS orderedQuantity,
               receivedQuantity AS receivedQuantity,
               coalesce(line.cancelled, false) AS cancelled
        ORDER BY line.lineNumber
        """

        async def _post_goods_receipt(tx, params):
            """Checks the purchase order, creates the document, books and compares."""
            purchase_order = await (await tx.run(purchase_order_query, params)).single()

            if purchase_order is None:
                raise NotFoundError(
                    f"A document with the number '{params['number']}' does not exist."
                )
            if purchase_order["type"] != "PurchaseOrder":
                raise BusinessLogicError(
                    f"Document '{params['number']}' is of type '{purchase_order['type']}'. "
                    "A goods receipt can only be checked against a purchase order."
                )
            if not purchase_order["employeeFound"]:
                raise NotFoundError(
                    f"An employee with the id '{params['employeeId']}' does not exist."
                )

            # Idempotency short circuit: the same delivery note against the same purchase
            # order has already been booked. No further check, no second booking — the
            # stored state is already the right result.
            already_booked = await (await tx.run(already_booked_query, params)).single()
            if already_booked is not None:
                comparison = await (await tx.run(comparison_query, params)).data()
                return (
                    already_booked["goodsReceiptNumber"], comparison, purchase_order["status"]
                )

            purchase_order_lines_by_number = {
                row["lineNumber"]: row
                for row in purchase_order["purchaseOrderLines"]
                if row["lineNumber"] is not None
            }
            unknown = sorted(
                {
                    line["lineNumber"]
                    for line in params["lines"]
                    if line["lineNumber"] not in purchase_order_lines_by_number
                }
            )
            if unknown:
                raise BusinessLogicError(
                    "These line numbers are not on purchase order "
                    f"'{params['number']}': {', '.join(str(n) for n in unknown)}."
                )

            # `_LINE_CONTEXT_QUERY` is shared with the general document path; its
            # `missingLocations` always stays empty here, because no goods receipt line
            # carries a `locationId` (see `GoodsReceiptLineCreate`) — the only thing needed
            # from this query is `mainWarehouseId`.
            context = await (await tx.run(_LINE_CONTEXT_QUERY, params)).single()
            if context is None:
                raise DatabaseError("The context query of the lines returned no result.")

            goods_receipt_number, base_number = (
                await DocumentRepository._next_goods_receipt_number(
                    tx, params["number"], params["year"]
                )
            )
            goods_receipt_props = {
                "type":     "GoodsReceipt",
                "date":     now.date(),
                "status":   "open",
                "language": "DE",
                "createdAt": now,
                # Redundant to the BASED_ON edge, but necessary for the unique constraint
                # (purchaseOrderNumber, deliveryNoteNumber) — a constraint binds properties
                # of the same node, not an edge.
                "purchaseOrderNumber": params["number"],
                "deliveryNoteNumber":  params["deliveryNoteNumber"],
                # Carries the same number as `number` itself on the first delivery; on
                # every follow-up delivery the base the counter suffix is appended to.
                "baseNumber":          base_number,
            }
            await (await tx.run(goods_receipt_query, {
                **params,
                "goodsReceiptNumber": goods_receipt_number,
                "props":              goods_receipt_props,
                "supplierId":         purchase_order["supplierId"],
            })).consume()

            # The lines of the goods receipt document record what actually arrived. Product
            # number and line number are values of this document, newly assigned here — not
            # those of the referenced purchase order line.
            goods_receipt_lines = [
                {
                    "lineNumber":      idx,
                    "productNumber":   purchase_order_lines_by_number[line["lineNumber"]]["productNumber"],
                    "quantity":        line["quantity"],
                    # Without a price of its own the one agreed on the purchase order line
                    # applies.
                    "unitPriceCent":   (
                        line["purchasePriceCent"]
                        if line["purchasePriceCent"] is not None
                        else purchase_order_lines_by_number[line["lineNumber"]]["unitPriceCent"]
                    ),
                    "discountPercent": 0.0,
                    "priceOverridden": line["purchasePriceCent"] is not None,
                    "hasFixedPrice":   False,
                }
                for idx, line in enumerate(params["lines"], start=1)
            ]
            await (await tx.run(
                _LINES_QUERY, {"number": goods_receipt_number, "lines": goods_receipt_lines}
            )).consume()

            # A FULFILS edge from every new goods receipt line onto the purchase order line
            # it delivers — the basis of deliveredQuantity/openQuantity and of the
            # ordered/received comparison below.
            fulfilments = [
                {
                    "sourceId": f"{goods_receipt_number}_{row['lineNumber']}",
                    "targetId": f"{params['number']}_{line['lineNumber']}",
                }
                for line, row in zip(params["lines"], goods_receipt_lines, strict=True)
            ]
            await (await tx.run(_FULFILS_QUERY, {"fulfilments": fulfilments})).consume()

            # Always the main warehouse, without a choice: goods are always delivered
            # there. No fallback to a line-level value — `GoodsReceiptLineCreate` has none.
            if context["mainWarehouseId"] is None:
                raise BusinessLogicError(
                    "No main warehouse is stored. Without a place no goods receipt can be "
                    "booked."
                )
            for line, row in zip(params["lines"], goods_receipt_lines, strict=True):
                await post_movement(
                    tx,
                    movement_params(
                        row["productNumber"],
                        context["mainWarehouseId"],
                        "Receipt",
                        line["quantity"],
                        # Deliberately the number of the PURCHASE ORDER and of its line:
                        # only through it does a later cancellation of the purchase order
                        # find the booking again, line by line.
                        document_number=params["number"],
                        line_number=line["lineNumber"],
                        purchase_price=_euro(row["unitPriceCent"]),
                    ),
                )

            comparison = await (await tx.run(comparison_query, params)).data()

            # For every line affected by THIS goods receipt whose cumulative delivery status
            # now differs from 'Complete', a notification to purchasing comes into existence
            # — in the same transaction as the booking, otherwise the message could be lost
            # while the goods are already on the shelf. The idempotency short circuit over
            # deliveryNoteNumber above keeps this code path from running at all on a repeat,
            # so a second notification for the same goods receipt never comes into existence.
            comparison_by_line = {row["lineNumber"]: row for row in comparison}
            affected_line_numbers = {line["lineNumber"] for line in params["lines"]}
            for line_number in affected_line_numbers:
                row = comparison_by_line.get(line_number)
                if row is None:
                    continue
                status, _ = _delivery_status(row["orderedQuantity"], row["receivedQuantity"])
                if status != "Complete":
                    await create_quantity_deviation(
                        tx,
                        goods_receipt_number=goods_receipt_number,
                        purchase_order_number=params["number"],
                        line_number=line_number,
                        now=now,
                    )

            # Automatic status change to 'completed' as soon as no line is open any more.
            # Cancelled lines count as done (never delivered and never will be — a cancelled
            # line must not keep the purchase order open forever), an over-delivery counts
            # as done as well (the same definition as `DocumentLine.openQuantity`).
            all_lines_complete = all(
                row["cancelled"]
                or _delivery_status(row["orderedQuantity"], row["receivedQuantity"])[1] == 0.0
                for row in comparison
            )
            purchase_order_status = purchase_order["status"]
            if all_lines_complete and purchase_order["status"] == "open":
                # `WHERE d.status = 'open'` is an additional safeguard in the same
                # transaction (not only the Python-side comparison above): it prevents a
                # purchase order cancelled meanwhile by a concurrent call from being
                # overwritten here.
                await (await tx.run(
                    "MATCH (d:Document {number: $number}) WHERE d.status = 'open' "
                    "SET d.status = 'completed', d.updatedAt = $now",
                    {"number": params["number"], "now": now},
                )).consume()
                purchase_order_status = "completed"

            return goods_receipt_number, comparison, purchase_order_status

        try:
            goods_receipt_number, comparison, purchase_order_status = await session.execute_write(
                _post_goods_receipt, params
            )
        except ConstraintError as e:
            # Two possible causes under the same exception: the goods receipt number formed
            # is already taken, or a concurrent call with the same delivery note number won
            # the idempotency short circuit narrowly. For the caller both are the same
            # signal: try again.
            raise DuplicateKeyError(
                "The booking collides with a concurrent operation — either the goods "
                "receipt number formed or the delivery note number was taken just now. "
                "Please try again."
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while booking the goods receipt: {e}") from e

        lines: list[GoodsReceiptLineResult] = []
        for row in comparison:
            status, open_quantity = _delivery_status(
                row["orderedQuantity"], row["receivedQuantity"]
            )
            lines.append(
                GoodsReceiptLineResult(
                    productNumber=row["productNumber"],
                    label=row["label"],
                    orderedQuantity=row["orderedQuantity"],
                    receivedQuantity=row["receivedQuantity"],
                    openQuantity=open_quantity,
                    deliveryStatus=status,
                )
            )

        return GoodsReceiptResult(
            purchaseOrderNumber=number,
            purchaseOrderStatus=purchase_order_status,
            goodsReceiptNumber=goods_receipt_number,
            deliveryNoteNumber=goods_receipt_data.deliveryNoteNumber,
            lines=lines,
        )

    @staticmethod
    async def _next_document_number(
        tx, type: DocumentType, project_number: str | None, year: int
    ) -> str:
        """Determines the next free document number.

        Runs inside an already open write transaction. Documents belonging to an order
        carry its project number behind the prefix of the document type; purchasing
        documents without an order reference get a yearly count.

        **The number counts for `DeliveryNote` and `Invoice`** — both are document types
        allowing several partial deliveries or partial invoices. The first document stays
        without a suffix (a number already printed must never change), from the second on
        the counting happens at the end: `DN-2026-0001` → `DN-2026-0001-2` →
        `DN-2026-0001-3`. For every other document type it stays at exactly one document
        per order — a second creation runs into the uniqueness constraint and is answered
        as a 409.

        **Two traps in the counting query:** looked for is the **exact** base number *or* a
        number starting with `base number + '-'` — a bare `STARTS WITH` on the base number
        alone would also hit a project number that happens to be an extension of its own
        (`DN-2026-0072` against `DN-2026-00723`). And the query is a pure counter, not a
        duplicate pre-check: two concurrent requests can compute the same number, the
        second `CREATE` then fails on the constraint and becomes a 409.

        Args:
            tx: The running Neo4j write transaction.
            type (DocumentType): The document type the prefix follows from.
            project_number (str | None): The project number of the order, if there is one.
            year (int): The year whose counter is continued on purchasing documents.

        Returns:
            str: The next free document number.
        """
        prefix = DOCUMENT_PREFIX[type]

        if project_number:
            base = f"{prefix}-{project_number}"
            if type not in MULTI_DOCUMENT_TYPES:
                return base

            head = f"{base}-"
            result = await tx.run(
                """
                MATCH (d:Document)
                WHERE d.number = $base OR d.number STARTS WITH $head
                RETURN count(CASE WHEN d.number = $base THEN 1 END) > 0 AS hasBase,
                       max(CASE WHEN d.number STARTS WITH $head
                                THEN toInteger(substring(d.number, $length)) END) AS maxSuffix
                """,
                {"base": base, "head": head, "length": len(head)},
            )
            record = await result.single()
            if record is None or not record["hasBase"]:
                return base
            following = (record["maxSuffix"] or 1) + 1
            return f"{base}-{following}"

        head = f"{prefix}-{year}-"
        result = await tx.run(
            """
            MATCH (d:Document)
            WHERE d.number STARTS WITH $head
            RETURN max(toInteger(substring(d.number, $length))) AS maximum
            """,
            {"head": head, "length": len(head)},
        )
        record = await result.single()
        maximum = (record["maximum"] if record else None) or 0
        return f"{head}{maximum + 1:04d}"

    @staticmethod
    async def _create_order_in_tx(
        tx, customer_id: str, delivery_date: date | None, year: int
    ) -> str:
        """Creates a new order and returns its project number.

        There is no separate place that hands out orders or project numbers. As soon as a
        sales document is created without an `orderProjectNumber`, the order therefore
        comes into existence here, together with the document that caused it.

        The project number format is `{year}-{sequence:04d}`; the sequence counts within
        the year, over all customers.

        **The sequence is shared with the document numbers of the year and is therefore
        counted over both.** A sales document belonging to an order is numbered
        `{prefix}-{projectNumber}`, so the project number `2026-0003` claims `QU-2026-0003`
        and `IN-2026-0003` along with it. A document that got its number from the yearly
        counter instead — every purchasing document, and anything imported without an order
        — occupies exactly the same slot. Counting orders alone would hand out a project
        number whose document number is already taken, and the `CREATE` would then fail on
        the uniqueness constraint. Skipping a few sequence numbers is the cheaper side of
        that trade.

        Runs inside the already open write transaction of `_create_document_in_tx`. An
        invalid `customer_id` cannot occur here — `_check_document_context` runs in the
        same transaction beforehand and would already have rejected it with a
        `NotFoundError`.

        Args:
            tx: The running Neo4j write transaction.
            customer_id (str): The customer number the order comes into existence under.
            delivery_date (date | None): The planned delivery date of the causing document,
                if set. Taken over as `Order.deliveryDate`.
            year (int): The year of the document creation, taken over as `Order.year`.

        Returns:
            str: The project number of the newly created order.
        """
        head = f"{year}-"
        # Two independent sources for the same counter, hence two CALL subqueries: a shared
        # MATCH would multiply orders and documents crosswise. The document numbers are cut
        # apart with `split` rather than by offset — a document of a follow-up delivery
        # carries a fourth part (`DN-2026-0001-2`), and only the third one counts.
        result = await tx.run(
            """
            CALL () {
                MATCH (o:Order)
                WHERE o.projectNumber STARTS WITH $head
                RETURN max(toInteger(substring(o.projectNumber, $length))) AS orderMaximum
            }
            CALL () {
                MATCH (d:Document)
                WITH split(d.number, '-') AS parts
                WHERE size(parts) >= 3 AND parts[1] = $year AND parts[2] =~ '[0-9]+'
                RETURN max(toInteger(parts[2])) AS documentMaximum
            }
            RETURN orderMaximum, documentMaximum
            """,
            {"head": head, "length": len(head), "year": str(year)},
        )
        record = await result.single()
        sequence = (
            max(
                (record["orderMaximum"] if record else None) or 0,
                (record["documentMaximum"] if record else None) or 0,
            )
            + 1
        )
        project_number = f"{head}{sequence:04d}"

        await (await tx.run(
            """
            MATCH (c:Customer {id: $customerId})
            CREATE (o:Order {projectNumber: $projectNumber})
            SET o.year         = $year,
                o.deliveryDate = $deliveryDate
            MERGE (c)-[:HAS_ORDER]->(o)
            """,
            {
                "customerId":    customer_id,
                "projectNumber": project_number,
                "year":          year,
                "deliveryDate":  delivery_date,
            },
        )).consume()
        return project_number
