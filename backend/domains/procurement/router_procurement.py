"""HTTP endpoints of the procurement domain, under /api/suppliers and /api/reorder-suggestions."""

from fastapi import APIRouter, Depends, Query, Response, status
from neo4j import AsyncSession

from core.database import get_db_session
from core.schemas import ErrorResponse
from core.security import get_current_user, require_any_role

from .schemas_procurement import (
    ReorderSuggestion,
    Supplier,
    SupplierCreate,
    SupplierProductCreate,
    SupplierProductResponse,
    SupplierUpdate,
)
from .service_procurement import ReorderSuggestionService, SupplierService

router_suppliers = APIRouter(prefix="/api/suppliers", tags=["Suppliers"])
router_reorder_suggestions = APIRouter(prefix="/api/reorder-suggestions", tags=["Procurement"])

# See router_iam.py: a constant instead of the factory call directly in the default
# argument, so B008 does not fire on every use.
#
# Reading a supplier stays open to every authenticated user — the master data appears as
# a selection list in several views. Everything that writes, and the reorder analysis
# with its purchase prices, belongs to purchasing alone.
purchasing_only = Depends(require_any_role("Purchasing"))


@router_suppliers.get(
    "/{id}",
    response_model=Supplier,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Supplier was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_supplier(
    id: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> Supplier:
    """Returns the details of one supplier.

    Returns the supplier by its id. Returns 404 when no supplier with that id exists.
    """
    return await SupplierService.get_supplier(id, session)


@router_suppliers.get(
    "",
    response_model=list[Supplier],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_suppliers(
    search: str | None = Query(
        None,
        description="Free-text search across name and city. Case is ignored.",
    ),
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> list[Supplier]:
    """Returns the list of suppliers, optionally filtered.

    Without a search term every supplier is returned. An empty list is a valid result and
    not an error.
    """
    return await SupplierService.get_suppliers(session, search=search)


@router_suppliers.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=Supplier,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Purchasing is missing"},
        409: {"model": ErrorResponse, "description": "The computed supplier number was taken by a concurrent creation just now — try again"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def create_supplier(
    supplier_data: SupplierCreate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = purchasing_only,
) -> Supplier:
    """Creates a new supplier.

    Only the name is mandatory; the remaining master data fields can be filled in later.
    The server assigns the id — `S-` plus a uuid4 — and the creation timestamp.
    """
    return await SupplierService.create_supplier(supplier_data, session)


@router_suppliers.patch(
    "/{id}",
    response_model=Supplier,
    responses={
        400: {"model": ErrorResponse, "description": "Not a single field was sent to change"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Purchasing is missing"},
        404: {"model": ErrorResponse, "description": "Supplier was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def update_supplier(
    id: str,
    supplier_data: SupplierUpdate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = purchasing_only,
) -> Supplier:
    """Updates selected fields of a supplier.

    Only the fields that change have to be sent (partial update); fields that are not
    sent stay untouched. Returns 404 when no supplier with that id exists.
    """
    return await SupplierService.update_supplier(id, supplier_data, session)


@router_suppliers.post(
    "/{id}/products",
    status_code=status.HTTP_201_CREATED,
    response_model=SupplierProductResponse,
    responses={
        200: {"model": SupplierProductResponse, "description": "Product was already in the supply range, the conditions were updated"},
        201: {"model": SupplierProductResponse, "description": "Product was newly taken into the supply range"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Purchasing is missing"},
        404: {"model": ErrorResponse, "description": "Supplier or product was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def add_supplied_product(
    id: str,
    product_data: SupplierProductCreate,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    _: dict = purchasing_only,
):
    """Takes a product into the supply range of a supplier.

    Creates the source of supply along with lead time, purchase price and the preferred
    supplier flag. The call is repeatable: a second call for the same combination does
    not create a second source of supply but overwrites the conditions and answers with
    200 instead of 201. Returns 404 when the supplier or the product does not exist.
    """
    result = await SupplierService.add_supplied_product(id, product_data, session)
    response.status_code = (
        status.HTTP_200_OK if result.get("wasUpdated") else status.HTTP_201_CREATED
    )
    return result


@router_reorder_suggestions.get(
    "",
    response_model=list[ReorderSuggestion],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Purchasing is missing"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_reorder_suggestions(
    session: AsyncSession = Depends(get_db_session),
    _: dict = purchasing_only,
) -> list[ReorderSuggestion]:
    """Determines the reorder demand across the entire product master.

    Lists every product whose stock has fallen below its minimum stock, with a suggested
    order quantity and the best source of supply. Products without a stored supplier
    appear without a source of supply instead of being missing; replaced products do not
    appear at all. The endpoint only suggests — it orders nothing.
    """
    return await ReorderSuggestionService.get_reorder_suggestions(session)
