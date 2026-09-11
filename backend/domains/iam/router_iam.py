"""API routes for authentication and user management."""

from fastapi import APIRouter, Depends, Response, status
from neo4j import AsyncSession

from core.database import get_db_session
from core.schemas import ErrorResponse
from core.security import get_current_user, require_any_role

from .schemas_iam import (
    EmployeeCreate,
    EmployeeOut,
    EmployeeUpdate,
    LoginRequest,
    PasswordChangeRequest,
    TokenResponse,
)
from .service_iam import AuthService, EmployeeService

router = APIRouter(tags=["IAM"])

# Evaluated once at import instead of five times in the signatures. require_any_role is a
# factory: calling it returns the actual check callable. Placed directly in a default
# argument, B008 flags it — and rightly so, since the rule cannot know the return value is
# stateless here. The constant takes the question away from it.
#
# fastapi.Depends itself is declared harmless in pyproject.toml, but that exemption only
# covers the outer call, not the nested one inside it.
admin_only = Depends(require_any_role("Admin"))


@router.post(
    "/api/auth/login",
    response_model=TokenResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Wrong credentials, or locked account"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def login(data: LoginRequest, session: AsyncSession = Depends(get_db_session)):
    """Verifies the credentials and returns a JWT on success.

    The token carries `sub` (employee id), `email`, `name` and `roles` — the roles coming
    from the `HAS_ROLE` edge, not from a property on the node. A locked account
    (`active: false`) is answered exactly like wrong credentials, so an attacker cannot
    use the difference to discover which e-mail addresses exist.
    """
    return await AuthService.login(data, session)


@router.post(
    "/api/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"model": ErrorResponse, "description": "Missing or invalid token"}},
)
async def logout(_: dict = Depends(get_current_user)) -> None:
    """Signs the current user out.

    The JWT is stateless and carries no server-side session — there is nothing to
    invalidate. The endpoint verifies the token anyway (an expired or missing one still
    yields `401`) and gives the frontend a fixed place that a future blocklist could hook
    into without changing the contract. Discarding the token itself is the client's job.
    """
    return None


@router.get(
    "/api/auth/me",
    response_model=EmployeeOut,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "The employee behind the token no longer exists"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def me(
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Loads and returns the currently signed-in employee.

    The token itself is not passed through — it carries `sub`/`exp` and other JWT
    internals that have no place in a user response. Instead the employee is loaded fresh
    from the graph via `sub`, including `id` and `initials`, which the token does not
    carry.
    """
    return await EmployeeService.get_by_id(current_user["sub"], session)


@router.patch(
    "/api/auth/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        400: {"model": ErrorResponse, "description": "The current password is wrong"},
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        404: {"model": ErrorResponse, "description": "Employee not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def change_own_password(
    data: PasswordChangeRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Changes one's own password after verifying the current one.

    Only the signed-in user can use this endpoint — there is no admin access to somebody
    else's password here. The current password has to be supplied: without that check, a
    stolen token would be enough to take the account over permanently.
    """
    await EmployeeService.change_password(current_user["sub"], data, session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/api/users",
    response_model=list[EmployeeOut],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Admin is missing"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_users(
    session: AsyncSession = Depends(get_db_session),
    _: dict = admin_only,
):
    """Fetches every employee, ordered by name. Admin only."""
    return await EmployeeService.get_all(session)


@router.get(
    "/api/users/{employee_id}",
    response_model=EmployeeOut,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Admin is missing"},
        404: {"model": ErrorResponse, "description": "Employee was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def get_user(
    employee_id: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = admin_only,
):
    """Looks a single employee up by id. Admin only."""
    return await EmployeeService.get_by_id(employee_id, session)


@router.post(
    "/api/users",
    response_model=EmployeeOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Admin is missing"},
        409: {"model": ErrorResponse, "description": "E-mail address or initials already taken"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def create_user(
    data: EmployeeCreate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = admin_only,
):
    """Creates a new employee together with their role edges. Admin only.

    The server assigns the id as `E-<uuid4>` — it is therefore not part of the request.
    """
    return await EmployeeService.create(data, session)


@router.patch(
    "/api/users/{employee_id}",
    response_model=EmployeeOut,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Admin is missing"},
        404: {"model": ErrorResponse, "description": "Employee was not found"},
        409: {"model": ErrorResponse, "description": "Initials already taken"},
        422: {"description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Internal database error, empty request included"},
    },
)
async def update_user(
    employee_id: str,
    data: EmployeeUpdate,
    session: AsyncSession = Depends(get_db_session),
    _: dict = admin_only,
):
    """Changes individual fields and/or replaces an employee's roles. Admin only.

    Only fields actually sent are changed. A role list that is sent REPLACES the existing
    `HAS_ROLE` edges completely rather than adding to them.
    """
    return await EmployeeService.update(employee_id, data, session)


@router.delete(
    "/api/users/{employee_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Role Admin is missing"},
        404: {"model": ErrorResponse, "description": "Employee was not found"},
        500: {"model": ErrorResponse, "description": "Internal database error"},
    },
)
async def delete_user(
    employee_id: str,
    session: AsyncSession = Depends(get_db_session),
    _: dict = admin_only,
):
    """Deletes an employee together with every one of their edges. Admin only."""
    await EmployeeService.delete(employee_id, session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
