"""HTTP endpoints of the catalog domain, under /api/products and /api/product-groups."""

from fastapi import APIRouter, Depends, Query, Response, status
from neo4j import AsyncSession

from core.database import get_db_session
from core.schemas import ErrorResponse
from core.security import get_current_user, require_any_role

from .schemas_catalog import (
    BomLine,
    BomLineCreate,
    BomLineDeleted,
    CategoryOption,
    Product,
    ProductCreate,
    ProductType,
    ProductUpdate,
)
from .service_catalog import BomService, CategoryService, ProductService

router = APIRouter(prefix="/api/products", tags=["Products"])
router_product_groups = APIRouter(prefix="/api/product-groups", tags=["Products"])

# See router_iam.py: a constant instead of the factory call directly in the default
# argument, so B008 does not fire on every use.
engineering_only = Depends(require_any_role("Engineering"))

# Who may see purchase prices on a product: the target cost price of the product master and
# the purchase price of every supplier condition. Both say the same thing — what the goods
# cost us — so they share one rule; hiding one while showing the other would hide nothing.
#
# Deliberately wider than `core.visibility.sees_cost_prices` (Purchasing alone): these
# prices are planning figures several departments work with, while the cost prices on a
# sales document — where the contribution margin becomes visible per customer — stay
# narrower.
_PURCHASE_PRICE_ROLES = {"Admin", "Purchasing", "BackOffice", "Accounting", "Engineering"}


def _hide_purchase_prices(user: dict) -> bool:
    """Whether purchase prices have to be stripped from the response for this user."""
    return not (_PURCHASE_PRICE_ROLES & set(user.get("roles", [])))


def _without_purchase_prices(product: Product) -> Product:
    """Returns a copy of the product without its cost price and supplier purchase prices.

    The supplier list itself stays: lead time and preferred supplier are what sales needs
    to name a delivery date, and neither reveals what the goods cost.
    """
    return product.model_copy(update={
        "costPrice": None,
        "suppliers": [
            supplier.model_copy(update={"purchasePrice": None})
            for supplier in product.suppliers
        ],
    })


def _bom_without_purchase_prices(lines: list[BomLine]) -> list[BomLine]:
    """Strips the purchase prices from every component of a bill of materials.

    Recursive, because every level of the tree carries full products — masking only the
    first level would leave the prices of every sub-assembly readable.
    """
    return [
        line.model_copy(update={
            "component": _without_purchase_prices(line.component),
            "subComponents": (
                None if line.subComponents is None
                else _bom_without_purchase_prices(line.subComponents)
            ),
        })
        for line in lines
    ]


@router.get(
    "/{number}",
    response_model=Product,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Product was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_product(
    number: str,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = Depends(get_current_user),
) -> Product:
    """Looks a single product up by its unique number.

    Returns 404 when the product does not exist. Cost price and supplier purchase prices
    are stripped for users without one of the purchasing-related roles.
    """
    product = await ProductService.get_product(number, session)
    if _hide_purchase_prices(current_user):
        product = _without_purchase_prices(product)
    return product


@router.get(
    "",
    response_model=list[Product],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_products(
    search: str | None = Query(
        None,
        description="Free-text search over product number and label. Case is ignored.",
    ),
    type: ProductType | None = Query(
        None,
        description="Restricts the result to parts or assemblies. Without a value, both are returned.",
    ),
    active: bool = Query(
        True,
        description="Filters on the active status. Default: active products only. Products without a maintained status count as active.",
    ),
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = Depends(get_current_user),
) -> list[Product]:
    """Fetches a filtered list of products.

    Every filter is optional and they combine additively. Without parameters, all active
    products are returned. An empty list means nothing matched.
    """
    products = await ProductService.get_products(
        session, search=search, type=type, active=active
    )
    if _hide_purchase_prices(current_user):
        products = [_without_purchase_prices(p) for p in products]
    return products


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=Product,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Engineering is missing"},
        409: {"model": ErrorResponse, "description": "Product number already exists"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def create_product(
    product: ProductCreate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_only,
):
    """Creates a new product.

    Returns 409 when the product number is already taken.
    """
    return await ProductService.create_product(product, session)


@router.patch(
    "/{number}",
    response_model=Product,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid or empty update data"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Engineering is missing"},
        404: {"model": ErrorResponse, "description": "Product was not found"},
        409: {"model": ErrorResponse, "description": "The new product number is already taken"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def update_product(
    number: str,
    update_data: ProductUpdate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_only,
):
    """Updates selected fields of a product.

    Only the fields that should change have to be sent. Returns 404 when the product does
    not exist.

    The `number` itself can be changed too — meant for correcting a typo made at creation.
    Relationships (bill of materials, document lines) hang off edges and survive the
    rename. If the new number is already taken, the answer is a 409.
    """
    return await ProductService.update_product(number, update_data, session)


@router.delete(
    "/{number}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Product deleted"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Engineering is missing"},
        404: {"model": ErrorResponse, "description": "Product was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def delete_product(
    number: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_only,
):
    """Deletes a product by its number.

    Runs a `DETACH DELETE` so no orphaned relationships are left behind. Returns 404 when
    the product does not exist.
    """
    await ProductService.delete_product(number, session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{number}/bom",
    response_model=list[BomLine],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Product was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_bom(
    number: str,
    depth: int = Query(
        1,
        ge=-1,
        description="Depth of the bill-of-materials resolution. Default: 1 (direct components only). Use -1 for the whole tree.",
    ),
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = Depends(get_current_user),
):
    """Resolves the bill of materials.

    Without the `depth` parameter only the direct level is loaded. Every component is a full
    product, so the same masking as on the product itself applies — on every level of the
    tree.
    """
    bom = [
        BomLine.model_validate(line)
        for line in await BomService.get_bom(number, depth, session)
    ]
    if _hide_purchase_prices(current_user):
        bom = _bom_without_purchase_prices(bom)
    return bom


@router.post(
    "/{number}/bom",
    status_code=status.HTTP_201_CREATED,
    response_model=BomLine,
    responses={
        200: {"model": BomLine, "description": "Component already existed, quantity was updated"},
        201: {"model": BomLine, "description": "Component was newly added to the bill of materials"},
        400: {"model": ErrorResponse, "description": "Business rule violated, e.g. a cycle or a self-reference"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Engineering is missing"},
        404: {"model": ErrorResponse, "description": "Assembly was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def add_component(
    number: str,
    line: BomLineCreate,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_only,
):
    """Adds a component to a product's bill of materials.

    Answers 201 for a newly created line and 200 when an existing line's quantity was
    updated.
    """
    result = await BomService.add_component(number, line, session)

    response.status_code = (
        status.HTTP_200_OK if result.get("wasUpdated") else status.HTTP_201_CREATED
    )
    return result


@router.delete(
    "/{number}/bom/{component_number}",
    response_model=BomLineDeleted,
    responses={
        200: {"model": BomLineDeleted, "description": "Component was removed from the bill of materials"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Engineering is missing"},
        404: {"model": ErrorResponse, "description": "There is no bill-of-materials link between the two products"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def delete_component(
    number: str,
    component_number: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_only,
):
    """Removes a component from a product's bill of materials.

    Only the link between the two products is deleted; the component itself remains as a
    product of its own. If it was the last line, the product counts as a part again —
    visible as `remainingLines: 0` in the response. Returns 404 when the component is not
    part of this bill of materials.
    """
    return await BomService.delete_component(number, component_number, session)


@router_product_groups.get(
    "",
    response_model=list[CategoryOption],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_product_groups(
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> list[CategoryOption]:
    """Returns the category hierarchy for the selection lists.

    Every category carries its product groups, every product group its subcategories. A
    level without a maintained name falls back to a placeholder built from its id.
    """
    return await CategoryService.get_hierarchy(session)
