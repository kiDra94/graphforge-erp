"""HTTP endpoints of the assets domain, under /api/assets."""

# `status` would be ambiguous here: GET /api/assets carries a query parameter of that name
# and the API specification prescribes it that way — renaming it is not an option. Inside
# the function the parameter shadows the module. The import therefore carries an
# unambiguous name instead of managing the collision with a comment.
from fastapi import APIRouter, Depends, Query, Response
from fastapi import status as http_status
from neo4j import AsyncSession

from core.database import get_db_session
from core.schemas import ErrorResponse
from core.security import get_current_user, require_any_role

from .schemas_assets import (
    Asset,
    AssetCreate,
    AssetCreated,
    AssetDraft,
    AssetDraftConfirmation,
    AssetListItem,
    AssetReleaseRequest,
    AssetReleaseResponse,
    AssetStatus,
    AssetUpdate,
    AssetUpdated,
    ComponentLineCreate,
    ComponentLineDeleted,
    InstalledComponent,
    ServiceCompletion,
    ServiceCompletionRequest,
    ServiceForecast,
    SparePartsDraft,
    SparePartsLink,
    SparePartsLinkCreated,
)
from .service_assets import AssetService, AssetServiceForecastService

# The prefix reads /api/assets, because the service forecast belongs to the assets in
# business terms and the API specification carries the endpoint under /api/assets/service.
#
# IMPORTANT for endpoints to come: the static route "/service" always has to be registered
# BEFORE a dynamic route "/{id}". FastAPI evaluates in registration order and would
# otherwise take "service" for the {id}.
router = APIRouter(prefix="/api/assets", tags=["Assets"])

# See router_iam.py: a constant instead of the factory call directly in the default
# argument, so B008 does not fire on every use.
#
# `Admin` is absent from these lists on purpose: it passes every role check automatically
# (core/security.py, has_role).
sales_only = Depends(require_any_role("Sales"))
sales_or_engineering = Depends(require_any_role("Sales", "Engineering"))
engineering_only = Depends(require_any_role("Engineering"))
engineering_or_backoffice = Depends(require_any_role("Engineering", "BackOffice"))


@router.get(
    "/service",
    response_model=list[ServiceForecast],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Sales missing"},
        500: {"model": ErrorResponse, "description": "Internal database error during the graph query"},
    },
)
async def get_due_services(
    session: AsyncSession = Depends(get_db_session),
    _: dict = sales_only,
) -> list[ServiceForecast]:
    """Returns a list of due services.

    Determines every installed wear part whose service interval expires within the next 30
    days. Returns a detailed list carrying the account manager, the customer, the affected
    machine and the calculated due date. Returns an empty list when nothing is currently
    due.
    """
    return await AssetServiceForecastService.get_due_services(session)


@router.put(
    "/service/{componentInstanceId}/completion",
    response_model=ServiceCompletion,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Sales nor Engineering"},
        404: {"model": ErrorResponse, "description": "Component instance was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def set_service_completion(
    componentInstanceId: str,
    completion: ServiceCompletionRequest,
    session: AsyncSession = Depends(get_db_session),
    _: dict = sales_or_engineering,
) -> ServiceCompletion:
    """Reports a due service as done.

    Creates a slim `ServiceEvent` node on the `ComponentInstance` for it. `completedAt` is
    set by the server and is used as the starting point for the next replacement cycle from
    then on, instead of counting from the shipping date of the asset — the affected row
    therefore disappears from `GET /api/assets/service` until the newly calculated date
    falls inside the 30-day window again. Reporting the same component again overwrites the
    previous report.
    """
    return await AssetServiceForecastService.set_completion(
        componentInstanceId, completion, session
    )


@router.get(
    "",
    response_model=list[AssetListItem],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        422: {"description": "Unknown value in the query parameter `status`"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_assets(
    customerId: str | None = Query(
        None,
        description="Narrows the result down to the assets of one customer, for instance 'C-1001'.",
    ),
    status: AssetStatus | None = Query(
        None,
        description="Filters on the calculated state. Without a value all three are returned.",
    ),
    search: str | None = Query(
        None,
        description="Free-text search across serial number, internal number and project number. Case is ignored.",
    ),
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> list[AssetListItem]:
    """Returns a filtered list of the assets.

    Every filter is optional and acts additively. Without parameters every asset is
    returned. Returns an empty list when no asset matches.

    The `status` does not sit on the node, it is calculated from shipping date and
    bill-of-materials release — as a filter it is fully effective all the same.
    """
    return await AssetService.get_assets(
        session, customerId=customerId, status=status, search=search
    )


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    response_model=AssetCreated,
    responses={
        400: {"model": ErrorResponse, "description": "Document is no order confirmation or carries no customer"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Sales nor Engineering"},
        404: {"model": ErrorResponse, "description": "Product or document was not found"},
        409: {"model": ErrorResponse, "description": "Serial number or internal number is already taken"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def create_asset(
    asset: AssetCreate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = sales_or_engineering,
) -> AssetCreated:
    """Creates a physical asset instance on the basis of an order confirmation.

    The customer is not in the body — it is derived from the document. Only an order
    confirmation may serve as the basis; every other document type is rejected with 400.

    The new asset stands in state `planned`. Its bill of materials comes about right away
    as a copy of the standard bill of materials of the product
    (`GET /api/assets/{serialNumber}/bom`) — engineering can still change it before the
    release.
    """
    return await AssetService.create_asset(asset, session)


# Static routes under "/drafts/..." likewise have to be registered BEFORE
# "/{serialNumber}" — the same rule as for "/service" above.


@router.get(
    "/drafts/assets",
    response_model=list[AssetDraft],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Engineering nor BackOffice"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_asset_drafts(
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_or_backoffice,
) -> list[AssetDraft]:
    """Returns the open asset drafts.

    One row per order confirmation with `assetPurpose == 'newAsset'` that does not carry an
    asset yet. `lines` lists the lines of the document — the starting point for the
    editable bill of materials engineering sends along on confirmation. Returns an empty
    list when there is currently nothing to confirm.

    Readable for BackOffice as well: a deep link out of the warehouse view has to be able
    to show something even when an order confirmation has no confirmed asset yet but only
    exists as an open draft. Confirming (`POST .../confirm`) stays reserved for
    engineering.
    """
    return await AssetService.get_asset_drafts(session)


@router.post(
    "/drafts/assets/confirm",
    response_model=list[AssetCreated],
    responses={
        400: {"model": ErrorResponse, "description": "Document does not fit the draft, or already carries an asset"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Engineering missing"},
        404: {"model": ErrorResponse, "description": "Document or employee was not found"},
        409: {"model": ErrorResponse, "description": "Serial number already taken"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def confirm_asset_draft(
    confirmation: AssetDraftConfirmation,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = engineering_only,
) -> list[AssetCreated]:
    """Confirms an asset draft.

    Merges what used to be two separate steps: creates the `AssetInstance`, writes
    `components` as its bill of materials and marks it as released — all in one
    transaction. Replaces the double step of `POST /api/assets` and `PATCH .../release` for
    the draft flow.

    Who confirmed comes from the auth token (`sub`), not from the request body.
    """
    return await AssetService.confirm_draft(confirmation, current_user["sub"], session)


@router.get(
    "/drafts/spare-parts",
    response_model=list[SparePartsDraft],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Engineering nor BackOffice"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_spare_parts_drafts(
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_or_backoffice,
) -> list[SparePartsDraft]:
    """Returns the open spare-parts drafts.

    One row per order confirmation with `assetPurpose == 'spareParts'` that is not assigned
    to an existing asset yet. Returns an empty list when there is currently nothing to
    link.
    """
    return await AssetService.get_spare_parts_drafts(session)


@router.post(
    "/drafts/spare-parts/{documentNumber}/link",
    response_model=SparePartsLinkCreated,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Engineering nor BackOffice"},
        404: {"model": ErrorResponse, "description": "Document or one of the assets named was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def link_spare_parts(
    documentNumber: str,
    link: SparePartsLink,
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_or_backoffice,
) -> SparePartsLinkCreated:
    """Links a spare-parts document with one or more existing assets.

    Pure traceability for the service history — unlike with an asset draft no new asset and
    no reservation come about.
    """
    return await AssetService.link_spare_parts(documentNumber, link, session)


@router.get(
    "/{serialNumber}",
    response_model=Asset,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Asset was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_asset(
    serialNumber: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = Depends(get_current_user),
) -> Asset:
    """Looks up and returns one specific asset.

    Besides the master data it holds the customer, the release block and the as-built
    state: the wear parts of the digital twin currently installed.

    The component list is empty as long as the asset carries no bill of materials — which
    is the normal case for simple assets and no error.
    """
    return await AssetService.get_asset(serialNumber, session)


@router.patch(
    "/{serialNumber}/release",
    response_model=AssetReleaseResponse,
    responses={
        400: {"model": ErrorResponse, "description": "A withdrawal after shipping is no longer possible, or the stock does not suffice for at least one component"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Engineering missing"},
        404: {"model": ErrorResponse, "description": "Asset or employee was not found"},
        422: {"description": "Invalid input data, for instance a note together with a withdrawal"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def set_release(
    serialNumber: str,
    release: AssetReleaseRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = engineering_only,
) -> AssetReleaseResponse:
    """Releases the bill of materials of an asset or takes the release back.

    Meant is the release by engineering, with which the asset may be built — not the
    acceptance of the finished machine by the customer.

    `released: false` withdraws. Date, note and the reference to the releasing employee are
    removed in the process, and the status falls back to `planned`. After shipping a
    withdrawal is no longer admissible.

    A real release reserves the bill of materials in the same transaction; does the stock
    not suffice for even one component, neither the release nor a booking comes about
    (400). A withdrawal releases the same reservation again.

    Who released comes from the auth token (`sub`), not from the request body — a client
    must never claim that itself.
    """
    return await AssetService.set_release(
        serialNumber, release, current_user["sub"], session
    )


@router.patch(
    "/{serialNumber}",
    response_model=AssetUpdated,
    responses={
        400: {"model": ErrorResponse, "description": "Empty update data or taking back a shipping date already set"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Sales nor Engineering"},
        404: {"model": ErrorResponse, "description": "Asset was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def update_asset(
    serialNumber: str,
    update_data: AssetUpdate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = sales_or_engineering,
) -> AssetUpdated:
    """Maintains the shipping and installation date of an asset.

    With the shipping date set the service interval begins and the asset appears in the
    service forecast — the bill of materials itself has existed since the asset was
    created. The answer reports the number of newly created components all the same: `0` in
    the normal case, positive only for a legacy asset without a previous copy.
    """
    return await AssetService.update_asset(serialNumber, update_data, session)


# --- Bill of materials of the single asset -----------------------------------
# Mirrored on GET/POST/DELETE /api/products/{number}/bom in catalog, so that no second
# idiom comes about beside the BOM pattern that already exists there.

@router.get(
    "/{serialNumber}/bom",
    response_model=list[InstalledComponent],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Neither role Engineering nor BackOffice"},
        404: {"model": ErrorResponse, "description": "Asset was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_bom(
    serialNumber: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_or_backoffice,
) -> list[InstalledComponent]:
    """Returns the bill of materials of a single asset.

    The same rows as `components` in `GET /api/assets/{serialNumber}`, but without the rest
    of the asset. Before the release this is engineering's working state, afterwards the
    frozen, already reserved state.
    """
    return await AssetService.get_bom(serialNumber, session)


@router.post(
    "/{serialNumber}/bom",
    status_code=http_status.HTTP_201_CREATED,
    response_model=InstalledComponent,
    responses={
        200: {"model": InstalledComponent, "description": "Component already existed, quantity was updated"},
        201: {"model": InstalledComponent, "description": "Component was newly added to the bill of materials"},
        400: {"model": ErrorResponse, "description": "The bill of materials is already released"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Engineering missing"},
        404: {"model": ErrorResponse, "description": "Asset or product was not found"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def set_component(
    serialNumber: str,
    line: ComponentLineCreate,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_only,
):
    """Adds a component to the bill of materials or changes its quantity.

    Locked after the release (400) — the bill of materials could otherwise be changed after
    stock had already been reserved for it.
    """
    result = await AssetService.set_component(serialNumber, line, session)

    response.status_code = (
        http_status.HTTP_200_OK if result.get("was_updated") else http_status.HTTP_201_CREATED
    )
    return result


@router.delete(
    "/{serialNumber}/bom/{productNumber}",
    response_model=ComponentLineDeleted,
    responses={
        200: {"model": ComponentLineDeleted, "description": "Component was removed from the bill of materials"},
        400: {"model": ErrorResponse, "description": "The bill of materials is already released"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Engineering missing"},
        404: {"model": ErrorResponse, "description": "Asset was not found, or it does not carry this component"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def delete_component(
    serialNumber: str,
    productNumber: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = engineering_only,
) -> ComponentLineDeleted:
    """Strikes a component from the bill of materials of an asset.

    Locked after the release (400), for the same reason as when adding. Unlike a cancelled
    document line the struck component does not stay visible — before the release it is a
    planning mistake, not a recordable event.
    """
    return await AssetService.delete_component(serialNumber, productNumber, session)
