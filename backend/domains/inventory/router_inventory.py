"""HTTP endpoints of the inventory domain, under /api/locations, /api/stock and
/api/stock-movements."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from neo4j import AsyncSession

from core.database import get_db_session
from core.schemas import ErrorResponse
from core.security import get_current_user, has_role, require_any_role

from .schemas_inventory import (
    Location,
    MovementType,
    Stock,
    StockMovement,
    StockMovementCreate,
    StockMovementResponse,
)
from .service_inventory import LocationService, StockMovementService, StockService

router_locations = APIRouter(prefix="/api/locations", tags=["Inventory"])
router_stock = APIRouter(prefix="/api/stock", tags=["Inventory"])
router_movements = APIRouter(prefix="/api/stock-movements", tags=["Inventory"])

# See router_iam.py: a constant instead of the factory call directly in the default
# argument, so B008 does not fire on every use.
#
# Base gate for POST: who may create a stock movement at all, regardless of its type. Which
# type is then allowed is decided by _check_movement_role, based on `type` — which is only
# known after the body has been parsed and therefore cannot be a plain Depends.
backoffice_or_warehouse = Depends(require_any_role("BackOffice", "Warehouse"))


def _check_movement_role(type: MovementType, user: dict) -> None:
    """Only BackOffice may book every movement type, Warehouse only transfers.

    A transfer between locations is a single internal operation Warehouse may trigger
    itself. Receipt, reservation, issue and correction stay with BackOffice — a receipt
    runs through the document endpoint for Warehouse anyway, not through this one.
    """
    if has_role(user, "BackOffice"):
        return
    if type == "Transfer" and has_role(user, "Warehouse"):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Not authorised: movement type '{type}' requires the role 'BackOffice'.",
    )


@router_locations.get(
    "",
    response_model=list[Location],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_locations(
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> list[Location]:
    """Returns every location of the location master data.

    Without filters and without pagination — the master data holds a handful of entries and
    is used by the interface as a selection list.
    """
    return await LocationService.get_locations(session)


@router_stock.get(
    "",
    response_model=list[Stock],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_stock_list(
    belowMinStock: bool = Query(
        False,
        description="Restricts the result to products with a maintained minStock whose total stock falls below it.",
    ),
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> list[Stock]:
    """Returns the stock of every product, broken down per location.

    With `belowMinStock=true` only products remain that have a maintained minimum stock and
    fall below it — the basis of the stock warnings.
    """
    return await StockService.get_stock_list(session, belowMinStock)


@router_stock.get(
    "/{product_number}",
    response_model=Stock,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Product was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_stock(
    product_number: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> Stock:
    """Returns a single product's stock across every location.

    Returns 404 when the product does not exist. A product that exists but was never stored
    returns zeros, not a 404.
    """
    return await StockService.get_stock(product_number, session)


@router_movements.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=StockMovementResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Business rule violated, e.g. an issue beyond the available stock, or a fractional quantity on a product measured in whole pieces (except for 'Correction')"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role BackOffice is missing, or role Warehouse on a movement type other than Transfer"},
        404: {"model": ErrorResponse, "description": "Product, source location, destination location, customer or document was not found"},
        422: {"description": "Invalid input data, e.g. an unknown movement type or a transfer without targetLocationId"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def post_movement(
    data: StockMovementCreate,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = backoffice_or_warehouse,
) -> StockMovementResponse:
    """Books a stock movement and advances the stock.

    The movement type in `type` decides the effect: receipt and correction raise the stock,
    issue lowers it, reservation blocks it, and a transfer moves it between two locations
    and therefore additionally needs `targetLocationId`.

    Returns 400 when the booking exceeds the available stock, and 403 when the role may not
    book the requested movement type.
    """
    _check_movement_role(data.type, current_user)
    return await StockMovementService.post_movement(data, session)


@router_movements.get(
    "",
    response_model=list[StockMovement],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role BackOffice nor Warehouse"},
        422: {"description": "Unknown value in the query parameter `type`"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_movements(
    productNumber: str | None = Query(
        None,
        description="Restricts the result to the movements of one product, e.g. 'ACME-2003'.",
    ),
    locationId: str | None = Query(
        None,
        description="Restricts the result to movements in or out of one location, e.g. '1'.",
    ),
    type: MovementType | None = Query(
        None,
        description="Restricts the result to one movement type. An unknown value yields 422.",
    ),
    fromDate: date | None = Query(
        None,
        description="Lower bound of the booking period, inclusive. Format YYYY-MM-DD.",
    ),
    toDate: date | None = Query(
        None,
        description="Upper bound of the booking period, inclusive. Format YYYY-MM-DD.",
    ),
    session: AsyncSession = Depends(get_db_session),
    _: dict = backoffice_or_warehouse,
) -> list[StockMovement]:
    """Returns the movement log, newest booking first.

    Every filter can be left out individually and they combine. `fromDate` and `toDate`
    each include their boundary day.
    """
    return await StockMovementService.get_movements(
        session,
        product_number=productNumber,
        location_id=locationId,
        type=type,
        from_date=fromDate,
        to_date=toDate,
    )
