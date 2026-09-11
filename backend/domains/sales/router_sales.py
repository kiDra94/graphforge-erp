"""HTTP endpoints of the sales domain, under /api/customers, /api/documents, /api/contracts,
/api/pricing and /api/reports."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from neo4j import AsyncSession

from core.database import get_db_session
from core.schemas import ErrorResponse
from core.security import get_current_user, has_role, require_any_role
from core.visibility import sees_cost_prices, sees_fixed_prices

from .schemas_sales import (
    ConditionCreate,
    Contract,
    ContractAssignment,
    ContractCreate,
    ContractDetail,
    Customer,
    CustomerCreate,
    CustomerUpdate,
    Document,
    DocumentCreate,
    DocumentDetail,
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
from .service_sales import (
    ContractService,
    CustomerService,
    DocumentService,
    PriceCalculationService,
    ReportService,
)

router_customers = APIRouter(prefix="/api/customers", tags=["Customers"])
router_documents = APIRouter(prefix="/api/documents", tags=["Documents"])
router_contracts = APIRouter(prefix="/api/contracts", tags=["Contracts"])
router_pricing = APIRouter(prefix="/api/pricing", tags=["Pricing"])
router_reports = APIRouter(prefix="/api/reports", tags=["Reports"])

# See router_iam.py: a constant instead of the factory call directly in the default
# argument, so B008 does not fire on every use. `Admin` passes every check automatically
# (core/security.py, has_role) and does not have to be listed here.
sales_or_purchasing = Depends(require_any_role("Sales", "Purchasing"))
backoffice_only = Depends(require_any_role("BackOffice"))
# The warehouse takes the goods in itself, so it may book the receipt — for this one
# endpoint only. Every other document endpoint stays with purchasing and the back office.
purchasing_backoffice_or_warehouse = Depends(
    require_any_role("Purchasing", "BackOffice", "Warehouse")
)

# `POST`/`PATCH /api/documents` serve six document types, each with an owner of its own
# (quote: sales; order confirmation and delivery note: back office; invoice: accounting;
# purchase order: purchasing). The `Depends` declaration does not know the document type
# from the body yet — FastAPI parses it only afterwards, the same restriction as on the
# movement-type check in inventory. The outer gate therefore lets the union of every
# document-carrying role through; which role may actually create the concrete type is
# checked by `_check_document_type_role` afterwards.
_document_creating_roles = Depends(
    require_any_role("Sales", "Purchasing", "BackOffice", "Accounting")
)

_ROLE_PER_DOCUMENT_TYPE: dict[str, str] = {
    "Quote":             "Sales",
    "OrderConfirmation": "BackOffice",
    "DeliveryNote":      "BackOffice",
    "Invoice":           "Accounting",
    "PurchaseOrder":     "Purchasing",
}


def _check_document_type_role(type: str, user: dict) -> None:
    """Checks whether the user may create or change this document type.

    `type` is only known after the body has been parsed, hence no plain `Depends`. A
    `GoodsReceipt` never arrives here — it comes into existence exclusively through
    `POST /api/documents/{number}/goods-receipt`.
    """
    required_role = _ROLE_PER_DOCUMENT_TYPE.get(type)
    if required_role and not has_role(user, required_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not authorised: document type '{type}' requires the role '{required_role}'.",
        )


def _hide_cost_prices_in_list(documents: list[Document], user: dict) -> list[Document]:
    """Hides the net amount of purchase orders when the role may not see cost prices.

    A purchase order is at heart a list of purchase prices. Every other document type
    carries sales prices, which stay visible unchanged.
    """
    if sees_cost_prices(user):
        return documents
    for document in documents:
        if document.type == "PurchaseOrder":
            document.totalNet = None
    return documents


def _hide_cost_prices_in_detail(document: DocumentDetail, user: dict) -> DocumentDetail:
    """Hides the purchase prices of a purchase order when the role may not see them.

    The counterpart of `_hide_cost_prices_in_list` for the detail view — there the purchase
    price additionally stands per line (`unitPrice`/`amount`), not only aggregated.
    """
    if sees_cost_prices(user) or document.type != "PurchaseOrder":
        return document
    document.subtotal            = None
    document.orderDiscountAmount = None
    document.totalNet            = None
    document.totalGross          = None
    for line in document.lines:
        line.unitPrice = None
        line.amount = None
    return document


def _hide_margin(rows: list[RevenueLine], user: dict) -> list[RevenueLine]:
    """Hides cost price and margin when the role may not see them."""
    if sees_cost_prices(user):
        return rows
    for row in rows:
        row.totalCost = None
        row.margin = None
        row.marginPercent = None
    return rows


def _hide_contract_prices(contract: ContractDetail, user: dict) -> ContractDetail:
    """Hides the discount rate and the fixed prices of a contract when the role may not see them."""
    if sees_fixed_prices(user):
        return contract
    contract.discountPercent = None
    for condition in contract.conditions:
        condition.fixedPrice = None
    return contract


def _hide_contract_rates(contracts: list[Contract], user: dict) -> list[Contract]:
    """Hides the discount rate of every contract when the role may not see it."""
    if sees_fixed_prices(user):
        return contracts
    for contract in contracts:
        contract.discountPercent = None
    return contracts


@router_customers.get(
    "",
    response_model=list[Customer],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_customers(
    search: str | None = Query(
        None,
        description="Free-text search across name, customer number, city and VAT id. Case is ignored.",
    ),
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> list[Customer]:
    """Returns the list of customers, optionally filtered.

    Without a search term every customer is returned. An empty list is a valid result and
    not an error.
    """
    return await CustomerService.get_customers(session, search=search)


@router_customers.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=Customer,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Sales nor Purchasing"},
        404: {"model": ErrorResponse, "description": "The given account manager was not found"},
        409: {"model": ErrorResponse, "description": "The assigned customer number was taken by a concurrent creation — try again"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def create_customer(
    customer_data: CustomerCreate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = sales_or_purchasing,
) -> Customer:
    """Creates a new customer.

    Only the name is mandatory; the remaining master data fields can be filled in later.
    The server assigns the customer number and the creation timestamp.
    """
    return await CustomerService.create_customer(customer_data, session)


@router_customers.get(
    "/{id}",
    response_model=Customer,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Customer was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_customer(
    id: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> Customer:
    """Returns the details of one customer.

    The path parameter is the customer number. Returns 404 when no customer with that
    number exists.
    """
    return await CustomerService.get_customer(id, session)


@router_customers.patch(
    "/{id}",
    response_model=Customer,
    responses={
        400: {"model": ErrorResponse, "description": "Not a single field was sent to change"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Sales nor Purchasing"},
        404: {"model": ErrorResponse, "description": "Customer or the given account manager was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def update_customer(
    id: str,
    customer_data: CustomerUpdate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = sales_or_purchasing,
) -> Customer:
    """Updates selected fields of a customer.

    Only the fields that change have to be sent (partial update); fields that are not sent
    stay untouched. The customer number cannot be changed — every document, order and asset
    hangs off it.
    """
    return await CustomerService.update_customer(id, customer_data, session)


@router_customers.post(
    "/{id}/contract",
    response_model=Customer,
    responses={
        400: {"model": ErrorResponse, "description": "The contract is global and cannot be assigned to a single customer"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role BackOffice is missing"},
        404: {"model": ErrorResponse, "description": "Customer or contract was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def assign_contract(
    id: str,
    assignment: ContractAssignment,
    session: AsyncSession = Depends(get_db_session),
    _: dict = backoffice_only,
) -> Customer:
    """Assigns a framework contract to a customer.

    A global contract already applies to every customer without an assignment and can
    therefore not be assigned on top. A second call with the same combination leaves
    exactly one assignment behind.
    """
    return await CustomerService.assign_contract(id, assignment, session)


@router_documents.get(
    "",
    response_model=list[Document],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_documents(
    type: DocumentType | None = Query(None, description="Only documents of this type."),
    status: DocumentStatus | None = Query(None, description="Only documents in this processing state."),
    customerId: str | None = Query(None, description="Only documents of this customer."),
    supplierId: str | None = Query(None, description="Only documents of this supplier."),
    search: str | None = Query(None, description="Free-text search across document number and the name of the business partner."),
    fromDate: date | None = Query(None, description="Lower bound of the document date, inclusive."),
    toDate: date | None = Query(None, description="Upper bound of the document date, inclusive."),
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = Depends(get_current_user),
) -> list[Document]:
    """Returns the list of documents, optionally filtered.

    Every filter is optional and they combine. A filter combination without hits returns
    an empty list and no error.

    The net amount of a purchase order stays empty when the role may not see purchase
    prices — on every other document type there is a sales price, which stays unmasked.
    """
    documents = await DocumentService.get_documents(
        session,
        type=type,
        status=status,
        customer_id=customerId,
        supplier_id=supplierId,
        search=search,
        from_date=fromDate,
        to_date=toDate,
    )
    return _hide_cost_prices_in_list(documents, current_user)


@router_documents.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentDetail,
    responses={
        400: {"model": ErrorResponse, "description": "The stock is not enough for the issue, or a line with a product measured in pieces carries a fractional quantity"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "The role for this document type is missing — quote: Sales, order confirmation/delivery note: BackOffice, invoice: Accounting, purchase order: Purchasing"},
        404: {"model": ErrorResponse, "description": "Customer, supplier, employee, product, location or predecessor document was not found"},
        409: {"model": ErrorResponse, "description": "The document number formed is already taken"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def create_document(
    document_data: DocumentCreate,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = _document_creating_roles,
) -> DocumentDetail:
    """Creates a document with its lines and books its stock effect.

    The document type determines what happens in the warehouse: an order confirmation
    reserves, delivery note and invoice issue, a goods receipt books in, quote and purchase
    order stay without effect. An invoice only issues when no delivery note came before it
    — otherwise the same goods would be taken out twice.

    Document and bookings come into existence together or not at all. If the stock is not
    enough, the endpoint answers with a 400, and neither document nor movement comes into
    existence.

    Which role may create which document type is fixed: quote Sales, order confirmation and
    delivery note BackOffice, invoice Accounting, purchase order Purchasing.

    The server assigns the document number. Who created the document comes from the auth
    token (`sub`), not from the request body.
    """
    _check_document_type_role(document_data.type, current_user)
    return await DocumentService.create_document(document_data, current_user["sub"], session)


@router_documents.get(
    "/{number}",
    response_model=DocumentDetail,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Document was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_document(
    number: str,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = Depends(get_current_user),
) -> DocumentDetail:
    """Returns a document with all its lines and calculated totals.

    The basis of the document print. The totals are not stored but calculated from the
    lines on every call. The lines are returned in their sort order, not by product number
    — the same product can appear more than once on a sales document.

    On a purchase order, unit prices, line amounts and every total stay empty when the role
    may not see purchase prices.
    """
    document = await DocumentService.get_document(number, session)
    return _hide_cost_prices_in_detail(document, current_user)


@router_documents.patch(
    "/{number}",
    response_model=DocumentDetail,
    responses={
        400: {"model": ErrorResponse, "description": "No field handed over, the document is concluded and may no longer be changed in substance, the document is already cancelled or partially cancelled, an active follow-up document blocks the cancellation, or cancelledLines names a line number that is no line of this document"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Sales, Purchasing, BackOffice nor Accounting, or the role does not match the document type of the existing document"},
        404: {"model": ErrorResponse, "description": "Document was not found"},
        409: {"model": ErrorResponse, "description": "A concurrent second conclusion of the same delivery note won the number of the automatically created invoice narrowly"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def update_document(
    number: str,
    document_data: DocumentUpdate,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = _document_creating_roles,
) -> DocumentDetail:
    """Updates status or maintainable fields of a document, cancellation included.

    Lines cannot be changed in substance: they carry the stock effect of the document, and
    a quantity changed afterwards would not pull the bookings already made along with it. A
    concluded document only takes a status change.

    `status: "cancelled"` cancels the whole document, `status: "partiallyCancelled"` only
    the lines named in `cancelledLines`. Both trigger a reversal of the original stock
    effect and are only possible out of `open` or `completed`; a document with an active
    (not cancelled) follow-up document is rejected.

    If a delivery note switches to `completed` in the process, its invoice comes into
    existence automatically and is returned in `createdFollowUpDocument`; who triggered the
    status change comes from the auth token (`sub`), not from the request body.

    The outer gate lets, as on `create_document`, only the union of every
    document-carrying role through; which role may actually change the existing document
    type is checked by `_check_document_type_role` against its stored type.
    """
    existing_document = await DocumentService.get_document(number, session)
    # `DocumentDetail.type` is `DocumentType | None` (the schema is used for projections
    # without a type elsewhere) — a document already created and found by its number always
    # carries a type in practice. Without this guard the role check would silently be
    # skipped for the None case that is not expected here.
    if existing_document.type is not None:
        _check_document_type_role(existing_document.type, current_user)
    return await DocumentService.update_document(
        number, document_data, current_user["sub"], session
    )


@router_documents.get(
    "/{number}/price-overrides",
    response_model=list[PriceOverrideLine],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Document was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_price_overrides(
    number: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> list[PriceOverrideLine]:
    """Returns the recorded manual price changes of a document.

    Only lines with `priceOverridden=True` leave an entry — a document without a manual
    price change returns an empty list, not an error.
    """
    return await DocumentService.get_price_overrides(number, session)


@router_documents.post(
    "/{number}/goods-receipt",
    response_model=GoodsReceiptResult,
    responses={
        400: {"model": ErrorResponse, "description": "The document is no purchase order, or a referenced line number is not on it"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Purchasing, BackOffice nor Warehouse"},
        404: {"model": ErrorResponse, "description": "Purchase order, location or employee was not found"},
        409: {"model": ErrorResponse, "description": "A concurrent call with the same delivery note number won the idempotency short circuit narrowly — try again"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def post_goods_receipt(
    number: str,
    goods_receipt_data: GoodsReceiptCreate,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = purchasing_backoffice_or_warehouse,
) -> GoodsReceiptResult:
    """Books a delivery against a purchase order and compares ordered with received.

    The endpoint creates the goods receipt document and writes one inbound booking per
    line. What comes back is the ordered/received comparison across **every** line of the
    purchase order — including the ones nothing was delivered for.

    Idempotent over `deliveryNoteNumber`: a second call with the same number against this
    purchase order does not book again but returns the result of the first booking
    unchanged (200, as on the first call). Two genuine partial deliveries stay possible,
    because they carry different delivery note numbers. A check can only run against a
    document of type purchase order. Who recorded the goods receipt comes from the auth
    token (`sub`), not from the request body.

    Every line addresses the purchase order line it delivers through `lineNumber`, not
    through `productNumber`.
    """
    return await DocumentService.post_goods_receipt(
        number, goods_receipt_data, current_user["sub"], session
    )


@router_contracts.get(
    "",
    response_model=list[Contract],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_contracts(
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = Depends(get_current_user),
) -> list[Contract]:
    """Returns the list of every framework contract, including the customers assigned.

    The discount rate stays empty when the role may not see fixed prices and discounts.
    """
    contracts = await ContractService.get_contracts(session)
    return _hide_contract_rates(contracts, current_user)


@router_contracts.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ContractDetail,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role BackOffice is missing"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def create_contract(
    contract_data: ContractCreate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = backoffice_only,
) -> ContractDetail:
    """Creates a new framework contract.

    `isGlobal` is mandatory and has no default: a contract global by accident would take
    effect at every customer immediately.
    """
    return await ContractService.create_contract(contract_data, session)


@router_contracts.get(
    "/{id}",
    response_model=ContractDetail,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Contract was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_contract(
    id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = Depends(get_current_user),
) -> ContractDetail:
    """Returns a framework contract with all its stored conditions.

    Discount rate and fixed prices stay empty when the role may not see them.
    """
    contract = await ContractService.get_contract(id, session)
    return _hide_contract_prices(contract, current_user)


@router_contracts.post(
    "/{id}/conditions",
    status_code=status.HTTP_201_CREATED,
    response_model=ContractDetail,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role BackOffice is missing"},
        404: {"model": ErrorResponse, "description": "Contract or product was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def add_condition(
    id: str,
    condition_data: ConditionCreate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = backoffice_only,
) -> ContractDetail:
    """Stores a fixed price for a product inside a contract."""
    return await ContractService.add_condition(id, condition_data, session)


@router_pricing.post(
    "/calculate",
    response_model=PriceCalculationResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Customer or product was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def calculate_price(
    request: PriceCalculationRequest,
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> PriceCalculationResponse:
    """Determines the current best price of the line level for a customer and a product.

    Resolves the product-related discount sources exclusively — contract fixed price,
    contract discount, product and product group discount. The customer-related discount
    applies to the whole invoice and is therefore not part of this response.
    """
    return await PriceCalculationService.calculate(request, session)


@router_reports.get(
    "/revenue",
    response_model=list[RevenueLine],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Sales nor Purchasing"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def revenue_report(
    fromDate: date = Query(description="Lower bound of the document date, inclusive."),
    toDate: date = Query(description="Upper bound of the document date, inclusive."),
    groupBy: RevenueGroupBy = Query(description="Grouping of the report."),
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = sales_or_purchasing,
) -> list[RevenueLine]:
    """Determines revenue, cost price and margin over a period.

    Summed are invoices exclusively. A product without a determinable cost price reports an
    empty margin instead of counting it as 0.

    Cost price and margin stay empty when the role may not see them — sales therefore sees
    revenue and quantity, but no margin.
    """
    rows = await ReportService.revenue(fromDate, toDate, groupBy, session)
    return _hide_margin(rows, current_user)
