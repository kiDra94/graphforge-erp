"""HTTP endpoints of the notifications domain, under /api/notifications."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from neo4j import AsyncSession

from core.database import get_db_session
from core.schemas import ErrorResponse
from core.security import get_current_user, has_role, require_any_role

from .schemas_notifications import Notification, UnavailableReport
from .service_notifications import NotificationService

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])

# See router_iam.py: a constant instead of the factory call directly in the default
# argument, so B008 does not fire on every use.
admin_only = Depends(require_any_role("Admin"))
# Whoever handles the outgoing goods also reports what was not there. That is BackOffice;
# `Warehouse` stays admitted, because the same finding arises while picking at the shelf.
# `Admin` passes every role check anyway.
reporting_roles = Depends(require_any_role("BackOffice", "Warehouse"))


@router.get(
    "",
    response_model=list[Notification],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_notifications(
    done: bool | None = Query(
        None,
        description="Only ticked-off (true), respectively open (false) notifications. Without a value both.",
    ),
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = Depends(get_current_user),
) -> list[Notification]:
    """Returns the caller's own notifications, filtered by the roles from the token.

    Every signed-in user sees exclusively notifications whose target role they hold —
    `Admin` sees all of them, independently of their other roles. Creates the service list
    of the current calendar year idempotently in the process, should it not exist for this
    year yet — there is no scheduler in the backend.
    """
    roles = None if has_role(current_user, "Admin") else current_user.get("roles", [])
    return await NotificationService.get_notifications(roles, done, session)


@router.put(
    "/{id}/done",
    response_model=Notification,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "The target role of the notification is missing"},
        404: {"model": ErrorResponse, "description": "Notification was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def set_done(
    id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user: dict = Depends(get_current_user),
) -> Notification:
    """Ticks a notification off.

    The role is only established after a look at the notification itself — who may tick off
    is the **target role of this one row**, not a fixed role for the whole endpoint (the
    same principle as `_check_document_type_role` on `POST /api/documents`, only with a
    database read instead of a field out of the body). `FOR_ROLE` never changes after
    creation, so a second read before the actual tick-off is no TOCTOU risk.
    """
    target_role = await NotificationService.get_target_role(id, session)
    if not has_role(current_user, target_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not permitted: this notification requires the role '{target_role}'.",
        )
    return await NotificationService.set_done(id, session)


@router.post(
    "/unavailable",
    response_model=list[Notification],
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role BackOffice or Warehouse missing"},
        404: {"model": ErrorResponse, "description": "Document or line number does not exist"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def report_unavailable(
    report: UnavailableReport,
    session: AsyncSession = Depends(get_db_session),
    _: dict = reporting_roles,
) -> list[Notification]:
    """Reports quantities of a delivery note missing at the location to purchasing.

    Does the stock not suffice when shipping, one notification for the role `Purchasing`
    comes about per line named.

    Idempotent through the business key `unavailable_{documentNumber}_{lineNumber}`: a
    second call creates no second row, it carries the quantity forward. A `done` already
    ticked off stays in place.
    """
    return await NotificationService.report_unavailable(report, session)


@router.post(
    "/annual-run",
    response_model=int,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Admin missing"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def create_annual_service_list(
    year: int = Query(description="The calendar year the service list is created for."),
    session: AsyncSession = Depends(get_db_session),
    _: dict = admin_only,
) -> int:
    """Creates the service list of a year manually.

    The same creation `GET /api/notifications` already triggers automatically for the
    current year — callable explicitly here, for an external cron or to catch up on a past
    year. Idempotent: a second call for the same year creates no second row.
    """
    return await NotificationService.create_annual_service_list(year, session)
