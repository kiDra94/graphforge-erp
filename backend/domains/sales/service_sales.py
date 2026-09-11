"""Business logic of the sales domain: customers, documents and goods receipt control."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from loguru import logger
from neo4j import AsyncSession

from core.exceptions import NotFoundError
from core.websocket import ids_and_scope, manager

from .repository_sales import (
    ContractRepository,
    CustomerRepository,
    DiscountCandidate,
    DocumentRepository,
    PriceCalculationRepository,
    PricingBasis,
    ReportRepository,
)
from .schemas_sales import (
    ConditionCreate,
    Contract,
    ContractAssignment,
    ContractCreate,
    ContractDetail,
    Customer,
    CustomerCreate,
    CustomerUpdate,
    Discount,
    Document,
    DocumentCreate,
    DocumentDetail,
    DocumentLine,
    DocumentStatus,
    DocumentType,
    DocumentUpdate,
    GoodsReceiptCreate,
    GoodsReceiptResult,
    PriceCalculationRequest,
    PriceCalculationResponse,
    PriceOverrideLine,
    RevenueGroupBy,
    RevenueLine,
)

# The precision every reported amount is rounded to. An amount with more digits cannot be
# printed on a document.
CENT = Decimal("0.01")

# Document types that CAN have a stock effect at all (see `_movement_type_for` in
# repository_sales/rules.py). Quote and purchase order are never among them — they only say
# something about an intention. On an invoice it additionally depends on the predecessor
# chain (no second issue when a delivery note has already issued); rebuilding that chain
# check in the service would need another query. One extra, possibly superfluous reload on
# the client is cheaper than a missing event.
_STOCK_EFFECT_POSSIBLE: frozenset[DocumentType] = frozenset({
    "OrderConfirmation", "DeliveryNote", "GoodsReceipt", "Invoice",
})


class Totals(NamedTuple):
    """The four calculated amounts of a document.

    No API schema but the result of the calculation function: the response then inserts
    the values into the detail view.
    """
    subtotal: Decimal
    orderDiscountAmount: Decimal
    totalNet: Decimal
    totalGross: Decimal


def _line_amount(
    quantity: float | None, unit_price: Decimal | None, discount_percent: float | None
) -> Decimal:
    """Calculates the amount of a document line from quantity, unit price and line discount.

    A pure function without database access and the only place a line amount comes into
    existence — both the amount reported on the line and the subtotal rest on it.

    Rounded commercially to cents. That happens here deliberately and not only in the sum:
    the printed document shows an amount per line, and the sum of the amounts shown has to
    equal the subtotal. Rounding only at the end makes the paper differ from the system by
    a cent or two.

    Quantity and discount rate arrive as `float` from the graph and go into a `Decimal`
    through their string representation — `Decimal(0.1)` would be the binary approximation
    and would shift the amount by cents on large quantities.

    Missing values count as 0 respectively as "no discount": a line from the old stock
    without a maintained discount rate is not a line without an amount.

    Args:
        quantity (float | None): The quantity of the line.
        unit_price (Decimal | None): The price per unit in euro.
        discount_percent (float | None): The discount on this line in percent.

    Returns:
        Decimal: The line amount in euro, rounded to cents.
    """
    if quantity is None or unit_price is None:
        return Decimal("0.00")

    discount = Decimal(str(discount_percent or 0.0))
    raw = Decimal(str(quantity)) * unit_price * (1 - discount / 100)
    return raw.quantize(CENT, rounding=ROUND_HALF_UP)


def _totals(
    lines: list[DocumentLine],
    order_discount_percent: float | None,
    tax_percent: float | None,
) -> Totals:
    """Calculates the four document totals from the lines.

    A pure function without database access. The calculation runs from the inside out: per
    line the line amount rounded to cents, their sum is the subtotal; off that the order
    discount, which gives the net amount; on top of that the tax.

    The line amounts come into existence through `_line_amount` and not from the `amount`
    field of the line handed over: in the read model it may be empty, and a total that
    silently comes out too low on an empty line would be worse than an error.

    Cancelled lines (`cancelled: true`, from a partial cancellation) no longer count — a
    cancelled line is no longer part of what the customer owes. It stays visible on the
    document all the same; only the total passes over it.

    **Fixed-price lines (`hasFixedPrice: true`) contribute their full amount to the
    subtotal but do not enter the basis of the order discount.** The fixed price takes
    precedence. The order discount is therefore calculated only from the sum of the
    non-fixed-price lines and subtracted from the subtotal — algebraically equivalent to
    the fixed-price lines staying undiscounted while only the others carry the order
    discount.

    A missing order discount or tax rate counts as 0. Without that handling every total of
    an imported document would be empty, because neither field is maintained in the old
    stock.

    Args:
        lines (list[DocumentLine]): The lines of the document.
        order_discount_percent (float | None): Discount on the whole document in percent.
        tax_percent (float | None): Tax rate in percent.

    Returns:
        Totals: Subtotal, order discount amount, net amount and gross amount in euro.
    """
    active_lines = [line for line in lines if not line.cancelled]

    # The start value carries the two decimal places through a document without lines as
    # well: `sum([])` would be an int and the response would carry "0" instead of "0.00".
    subtotal = sum(
        (
            _line_amount(line.quantity, line.unitPrice, line.discountPercent)
            for line in active_lines
        ),
        Decimal("0.00"),
    )
    discountable_sum = sum(
        (
            _line_amount(line.quantity, line.unitPrice, line.discountPercent)
            for line in active_lines
            if not line.hasFixedPrice
        ),
        Decimal("0.00"),
    )

    discount = Decimal(str(order_discount_percent or 0.0))
    order_discount_amount = (discountable_sum * discount / 100).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    total_net = subtotal - order_discount_amount

    tax = Decimal(str(tax_percent or 0.0))
    tax_amount = (total_net * tax / 100).quantize(CENT, rounding=ROUND_HALF_UP)

    return Totals(
        subtotal=subtotal,
        orderDiscountAmount=order_discount_amount,
        totalNet=total_net,
        totalGross=total_net + tax_amount,
    )


def _best_line_price(
    base_price: Decimal,
    discountable: bool,
    candidates: list[DiscountCandidate],
) -> tuple[Decimal, Discount | None]:
    """Resolves the product axis of the discount hierarchy through the best price.

    A pure function without database access — the same separation as with `_line_amount`
    and `_totals`: the repository only delivers the candidates valid on the reference date
    (`PriceCalculationRepository.read_pricing_basis`), and picking among them needs no
    database any more and can therefore be played through in isolation.

    `discountable: False` beats every candidate: the base price then applies unchanged,
    regardless of which contracts or campaigns would otherwise take effect. Without
    candidates the base price applies as well, without that being an error — a customer
    without a contract and without a relevant campaign is the normal case.

    Among several candidates the one cheapest for the customer wins (the best price), not
    the sum: two discounts harmless on their own could otherwise add up to a price below
    the cost price without anyone noticing. On a tie the first candidate in the order
    handed over wins.

    Args:
        base_price (Decimal): The standard sales price of the product in euro.
        discountable (bool): Whether the product can be discounted at all.
        candidates (list[DiscountCandidate]): The discount sources valid on the date.

    Returns:
        tuple[Decimal, Discount | None]: The final price and the winning rule, or the
            unchanged base price and `None` without a discount.
    """
    if not discountable or not candidates:
        return base_price, None

    def _price(candidate: DiscountCandidate) -> Decimal:
        if candidate.fixedPrice is not None:
            return candidate.fixedPrice
        discount = Decimal(str(candidate.percent))
        return (base_price * (1 - discount / 100)).quantize(CENT, rounding=ROUND_HALF_UP)

    best_candidate = candidates[0]
    best_price = _price(best_candidate)

    for candidate in candidates[1:]:
        price = _price(candidate)
        if price < best_price:
            best_candidate = candidate
            best_price = price

    return best_price, Discount(
        type=best_candidate.type,
        percent=best_candidate.percent,
        amount=base_price - best_price,
        source=best_candidate.source,
    )


def _with_amounts(document: DocumentDetail) -> DocumentDetail:
    """Inserts the calculated amounts into a document that was read.

    The repository delivers exclusively what is stored — line amounts and totals are not.
    They come into existence here, in exactly one place for all three endpoints returning
    a complete document.

    The document is modified rather than copied: it comes fresh from the repository and
    belongs to no other caller.

    Args:
        document (DocumentDetail): The document read, without its calculated fields.

    Returns:
        DocumentDetail: The same document with line amounts and the four totals.
    """
    for line in document.lines:
        line.amount = _line_amount(line.quantity, line.unitPrice, line.discountPercent)

    totals = _totals(document.lines, document.orderDiscountPercent, document.taxPercent)
    document.subtotal = totals.subtotal
    document.orderDiscountAmount = totals.orderDiscountAmount
    document.totalNet = totals.totalNet
    document.totalGross = totals.totalGross
    return document


class CustomerService:
    """Drives the business logic for customer master data.

    The link between router and repository. This layer's own contribution is narrow: it
    translates the repository's `None` into a business error and thereby keeps the data
    access layer free of HTTP semantics.
    """

    @staticmethod
    async def get_customers(session: AsyncSession, search: str | None = None) -> list[Customer]:
        """Fetches the customer list, optionally filtered by a free text.

        An empty result list is a valid answer and not an error.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            search (str | None): Free text across name, number, city and VAT id.

        Returns:
            list[Customer]: The customers found, or an empty list.
        """
        return await CustomerRepository.get_customers(session, search=search)

    @staticmethod
    async def get_customer(id: str, session: AsyncSession) -> Customer:
        """Looks up a customer by its customer number.

        Args:
            id (str): The customer number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Customer: The customer data found, including the account manager.

        Raises:
            NotFoundError: When no customer with that number exists.
        """
        customer = await CustomerRepository.get_customer(id, session)

        if customer is None:
            raise NotFoundError(f"A customer with the number '{id}' does not exist.")
        return customer

    @staticmethod
    async def create_customer(customer_data: CustomerCreate, session: AsyncSession) -> Customer:
        """Creates a new customer.

        Args:
            customer_data (CustomerCreate): The master data of the new customer.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Customer: The created customer including the server-assigned number.

        Raises:
            NotFoundError: When the given `accountManagerId` belongs to no employee.
        """
        customer = await CustomerRepository.create_customer(customer_data, session)
        logger.bind(customer_id=customer.id).info("Customer created successfully.")

        await manager.send_event({
            "type": "event", "entity": "customer", "trigger": "customer_created",
            "reference": customer.id, "ids": [customer.id], "scope": "list",
        })
        return customer

    @staticmethod
    async def update_customer(
        id: str, customer_data: CustomerUpdate, session: AsyncSession
    ) -> Customer:
        """Updates individual fields of an existing customer.

        Passes the write model on unchanged instead of sorting out fields itself: which
        fields the client actually sent is known only to the model.

        Args:
            id (str): The customer number.
            customer_data (CustomerUpdate): The fields to change.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Customer: The updated customer.

        Raises:
            NotFoundError: When no customer with that number exists, or the given
                `accountManagerId` belongs to no employee.
            BusinessLogicError: When not a single field was handed over to change.
        """
        customer = await CustomerRepository.update_customer(id, customer_data, session)

        if customer is None:
            raise NotFoundError(f"A customer with the number '{id}' does not exist.")
        logger.bind(customer_id=customer.id).info("Customer updated successfully.")

        await manager.send_event({
            "type": "event", "entity": "customer", "trigger": "customer_updated",
            "reference": customer.id, "ids": [customer.id], "scope": "list",
        })
        return customer

    @staticmethod
    async def assign_contract(
        id: str, assignment: ContractAssignment, session: AsyncSession
    ) -> Customer:
        """Assigns a framework contract to a customer.

        Args:
            id (str): The customer number.
            assignment (ContractAssignment): The id of the contract to assign.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Customer: The customer after the assignment.

        Raises:
            NotFoundError: When customer or contract do not exist.
            BusinessLogicError: When the contract is global.
        """
        customer = await CustomerRepository.assign_contract(id, assignment.contractId, session)

        if customer is None:
            raise NotFoundError(f"A customer with the number '{id}' does not exist.")

        await manager.send_event({
            "type": "event", "entity": "customer", "trigger": "contract_assigned",
            "reference": customer.id, "ids": [customer.id], "scope": "list",
        })
        return customer


class DocumentService:
    """Drives the business logic for documents and the goods receipt control.

    The amounts live here: the line amounts come out of the lines and the four totals of
    the document out of those. The calculation functions deliberately sit at module level
    and not in this class — that way they can be tested one by one without building a
    service.

    What does not live here are the rules needing the stored state: the stock check, the
    stock effect including the predecessor chain, and the ordered/received comparison. They
    run in the same transaction as the write operation and therefore sit in the repository,
    otherwise a concurrent request can slip in between the check and the write.
    """

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

        The filters are purely additive — the more are set, the narrower the result. A
        filter combination without hits is a valid result and not an error.

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
        """
        return await DocumentRepository.get_documents(
            session,
            type=type,
            status=status,
            customer_id=customer_id,
            supplier_id=supplier_id,
            search=search,
            from_date=from_date,
            to_date=to_date,
        )

    @staticmethod
    async def get_document(number: str, session: AsyncSession) -> DocumentDetail:
        """Fetches a document with all its lines and calculated totals.

        Args:
            number (str): The document number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            DocumentDetail: The complete document.

        Raises:
            NotFoundError: When no document with that number exists.
        """
        document = await DocumentRepository.get_document(number, session)

        if document is None:
            raise NotFoundError(f"A document with the number '{number}' does not exist.")
        return _with_amounts(document)

    @staticmethod
    async def get_price_overrides(
        number: str, session: AsyncSession
    ) -> list[PriceOverrideLine]:
        """Fetches the recorded manual price changes of a document.

        Args:
            number (str): The document number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[PriceOverrideLine]: The price changes, empty when no line was overridden
                by hand.

        Raises:
            NotFoundError: When no document with that number exists.
        """
        rows = await DocumentRepository.get_price_overrides(number, session)

        if rows is None:
            raise NotFoundError(f"A document with the number '{number}' does not exist.")
        return rows

    @staticmethod
    async def create_document(
        document_data: DocumentCreate, employee_id: str, session: AsyncSession
    ) -> DocumentDetail:
        """Creates a document and triggers its stock effect.

        Which booking comes into existence follows from the document type and the chain of
        predecessors. The document and every booking come into existence together or not at
        all: if the stock is not enough for an issue, the whole operation is rolled back. A
        partially booked document would be a state no endpoint straightens out again.

        Args:
            document_data (DocumentCreate): Document header and lines.
            employee_id (str): Id of the creating employee, from the auth token.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            DocumentDetail: The created document including its number and totals.

        Raises:
            NotFoundError: When customer, supplier, employee, product, location or
                predecessor do not exist.
            BusinessLogicError: When the stock is not enough for an issue.
        """
        document = _with_amounts(
            await DocumentRepository.create_document(document_data, employee_id, session)
        )
        logger.bind(
            document_number=document.number,
            employee_id=employee_id,
        ).info("Document created successfully.")

        await manager.send_event({
            "type": "event", "entity": "document", "trigger": "document_created",
            "reference": document.number, "ids": [document.number], "scope": "list",
        })
        if document.type in _STOCK_EFFECT_POSSIBLE:
            await manager.send_event({
                "type": "event", "entity": "stock", "trigger": "document_created",
                "reference": document.number,
                **ids_and_scope([line.productNumber for line in document.lines]),
            })
        return document

    @staticmethod
    async def update_document(
        number: str, document_data: DocumentUpdate, employee_id: str, session: AsyncSession
    ) -> DocumentDetail:
        """Updates status or maintainable fields of a document.

        Lines cannot be changed, and a concluded document only takes a status change. Both
        are checked by the repository inside the write transaction, because it needs the
        stored state. If a delivery note switches to `completed` in the process, its
        invoice comes into existence automatically; `employee_id` then carries its
        `CREATED_BY`.

        Args:
            number (str): The document number.
            document_data (DocumentUpdate): The fields to change.
            employee_id (str): Id of the employee triggering the change, from the auth
                token.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            DocumentDetail: The updated document.

        Raises:
            NotFoundError: When no document with that number exists.
            BusinessLogicError: When no field was handed over, or the document is concluded
                and is to be changed in substance.
        """
        document = await DocumentRepository.update_document(
            number, document_data, employee_id, session
        )

        if document is None:
            raise NotFoundError(f"A document with the number '{number}' does not exist.")

        is_cancellation = document_data.status in ("cancelled", "partiallyCancelled")
        if is_cancellation:
            logger.bind(
                document_number=number,
                status=document_data.status,
                cancelled_lines=document_data.cancelledLines,
            ).info(
                "Document cancelled, reversal booked."
                if document_data.status == "cancelled"
                else "Document partially cancelled, reversal booked."
            )
        else:
            logger.bind(document_number=number).info("Document updated successfully.")

        document = _with_amounts(document)

        await manager.send_event({
            "type": "event", "entity": "document", "trigger": "document_updated",
            "reference": number, "ids": [number], "scope": "list",
        })
        if is_cancellation:
            # A full cancellation gives every line back, a partial one only the ones named.
            # The validator in `DocumentUpdate` makes sure the list is not empty then. The
            # websocket payload stays product-based, so the line numbers named are
            # translated back into their product numbers.
            if document_data.status == "partiallyCancelled":
                assert document_data.cancelledLines is not None
                target_lines = {line.lineNumber for line in document_data.cancelledLines}
                affected = [
                    line.productNumber
                    for line in document.lines
                    if line.lineNumber in target_lines
                ]
            else:
                affected = [line.productNumber for line in document.lines]
            await manager.send_event({
                "type": "event", "entity": "stock", "trigger": "document_cancelled",
                "reference": number, **ids_and_scope(affected),
            })
        return document

    @staticmethod
    async def post_goods_receipt(
        number: str,
        goods_receipt_data: GoodsReceiptCreate,
        employee_id: str,
        session: AsyncSession,
    ) -> GoodsReceiptResult:
        """Books a delivery and returns the ordered/received comparison.

        Enforces that a check only ever runs against a purchase order: a goods receipt
        against a quote or an invoice makes no business sense.

        Idempotent over `deliveryNoteNumber`: a second call with the same number against
        the same purchase order does not book again but returns the result of the first
        booking.

        Args:
            number (str): The number of the purchase order.
            goods_receipt_data (GoodsReceiptCreate): The checked lines.
            employee_id (str): Id of the recording employee, from the auth token.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            GoodsReceiptResult: The ordered/received comparison across every line of the
                purchase order, plus the number of the goods receipt document created.

        Raises:
            NotFoundError: When the purchase order, a product, a location or the employee
                do not exist.
            BusinessLogicError: When the document is no purchase order, or a delivered line
                is not on it.
        """
        result = await DocumentRepository.post_goods_receipt(
            number, goods_receipt_data, employee_id, session
        )
        logger.bind(
            purchase_order_number=result.purchaseOrderNumber,
            goods_receipt_number=result.goodsReceiptNumber,
            employee_id=employee_id,
        ).info("Goods receipt booked successfully.")

        await manager.send_event({
            "type": "event", "entity": "stock", "trigger": "goods_receipt",
            "reference": result.goodsReceiptNumber,
            **ids_and_scope([line.productNumber for line in result.lines]),
        })
        return result


class ContractService:
    """Drives the business logic for framework contracts and their conditions."""

    @staticmethod
    async def get_contracts(session: AsyncSession) -> list[Contract]:
        """Fetches the list of every framework contract.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[Contract]: The existing contracts with the customers assigned to them.
        """
        return await ContractRepository.get_contracts(session)

    @staticmethod
    async def get_contract(id: str, session: AsyncSession) -> ContractDetail:
        """Fetches a framework contract with all its conditions.

        Args:
            id (str): The id of the contract.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ContractDetail: The complete contract.

        Raises:
            NotFoundError: When no contract with that id exists.
        """
        contract = await ContractRepository.get_contract(id, session)

        if contract is None:
            raise NotFoundError(f"A contract with the id '{id}' does not exist.")
        return contract

    @staticmethod
    async def create_contract(
        contract_data: ContractCreate, session: AsyncSession
    ) -> ContractDetail:
        """Creates a new framework contract.

        Args:
            contract_data (ContractCreate): The contract data.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ContractDetail: The created contract.
        """
        return await ContractRepository.create_contract(contract_data, session)

    @staticmethod
    async def add_condition(
        contract_id: str, condition_data: ConditionCreate, session: AsyncSession
    ) -> ContractDetail:
        """Stores a condition for a product inside a contract.

        Args:
            contract_id (str): The id of the contract.
            condition_data (ConditionCreate): Product number and fixed price.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ContractDetail: The contract with the new condition.

        Raises:
            NotFoundError: When contract or product do not exist.
        """
        contract = await ContractRepository.add_condition(contract_id, condition_data, session)

        if contract is None:
            raise NotFoundError(f"A contract with the id '{contract_id}' does not exist.")
        return contract


class PriceCalculationService:
    """Drives the business logic of the price calculation on the line level."""

    @staticmethod
    async def calculate(
        request: PriceCalculationRequest, session: AsyncSession
    ) -> PriceCalculationResponse:
        """Determines base price and final price of a product for a customer on a date.

        Args:
            request (PriceCalculationRequest): Customer, product and reference date.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            PriceCalculationResponse: Base price, final price and the winning rule.

        Raises:
            NotFoundError: When customer or product do not exist.
        """
        basis: PricingBasis | None = await PriceCalculationRepository.read_pricing_basis(
            request, session
        )

        if basis is None:
            raise NotFoundError(
                f"Customer '{request.customerId}' or product '{request.productNumber}' "
                "does not exist."
            )

        final_price, discount = _best_line_price(
            basis.basePrice, basis.discountable, basis.candidates
        )
        return PriceCalculationResponse(
            productNumber=basis.productNumber,
            basePrice=basis.basePrice,
            finalPrice=final_price,
            discountable=basis.discountable,
            discount=discount,
        )


class ReportService:
    """Drives the business logic of the revenue and margin report."""

    @staticmethod
    async def revenue(
        from_date: date, to_date: date, group_by: RevenueGroupBy, session: AsyncSession
    ) -> list[RevenueLine]:
        """Sums revenue and margin over invoices in the period.

        Args:
            from_date (date): Lower bound of the document date, inclusive.
            to_date (date): Upper bound of the document date, inclusive.
            group_by (RevenueGroupBy): Grouping by customer, product or month.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[RevenueLine]: Revenue, cost price and margin per group.
        """
        return await ReportRepository.revenue(from_date, to_date, group_by, session)
