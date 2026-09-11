"""Business logic of the IAM domain."""

from loguru import logger
from neo4j import AsyncSession

from core.exceptions import BusinessLogicError, NotFoundError
from core.security import create_access_token, verify_password
from core.websocket import manager

from .repository_iam import EmployeeRepository
from .schemas_iam import (
    EmployeeCreate,
    EmployeeOut,
    EmployeeUpdate,
    LoginRequest,
    PasswordChangeRequest,
    TokenResponse,
)


class AuthService:
    """Verifies credentials and issues JWTs."""

    @staticmethod
    async def login(data: LoginRequest, session: AsyncSession) -> TokenResponse:
        """Verifies identifier, password and account status and issues a token on success.

        All three failure reasons (unknown identifier, locked account, wrong password) are
        logged, but the response never distinguishes an unknown identifier from a wrong
        password — otherwise the endpoint would tell an attacker which e-mail addresses
        exist.

        Args:
            data (LoginRequest): E-mail address or initials, plus the password.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            TokenResponse: The JWT carrying `sub`, `email`, `name` and `roles` (the latter
                from the `HAS_ROLE` edge).

        Raises:
            BusinessLogicError: On an unknown identifier, a locked account or a wrong
                password.
        """
        # The '@' is the only thing that decides which lookup runs: an identifier
        # containing one is an e-mail address, anything else is a set of initials.
        if "@" in data.identifier:
            employee = await EmployeeRepository.get_by_email(data.identifier, session)
        else:
            employee = await EmployeeRepository.get_by_initials(data.identifier, session)

        if not employee:
            logger.bind(request_id="-", identifier=data.identifier).warning(
                "Login failed: unknown e-mail address or initials"
            )
            raise BusinessLogicError("E-mail address or password is wrong.")

        if not employee.get("active", True):
            logger.bind(request_id="-", identifier=data.identifier).warning(
                "Login failed: account is locked"
            )
            raise BusinessLogicError("Account is locked.")

        stored_password = employee.get("password", "")
        if not stored_password or not verify_password(data.password, stored_password):
            logger.bind(request_id="-", identifier=data.identifier).warning(
                "Login failed: wrong password"
            )
            raise BusinessLogicError("E-mail address or password is wrong.")

        token = create_access_token({
            "sub":   str(employee["id"]),
            "email": employee["email"],
            "name":  employee["name"],
            "roles": employee.get("roles", []),
        })
        logger.bind(
            request_id="-", identifier=data.identifier, employee_id=employee["id"]
        ).info("Login succeeded")
        return TokenResponse(access_token=token)


class EmployeeService:
    """Thin shell around `EmployeeRepository`: turns `None` into `NotFoundError` and logs.

    The link between router and repository: the router stays free of logic, the database
    access stays in the repository. Every write is logged, no read is.
    """

    @staticmethod
    async def get_all(session: AsyncSession) -> list[EmployeeOut]:
        """Fetches every employee.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[EmployeeOut]: Every employee, or an empty list.
        """
        return await EmployeeRepository.get_all(session)

    @staticmethod
    async def get_by_id(employee_id: str, session: AsyncSession) -> EmployeeOut:
        """Looks an employee up by id.

        Args:
            employee_id (str): Id of the employee.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            EmployeeOut: The employee found.

        Raises:
            NotFoundError: When the id does not exist in the graph.
        """
        employee = await EmployeeRepository.get_by_id(employee_id, session)
        if not employee:
            raise NotFoundError(f"Employee with id {employee_id} was not found.")
        return employee

    @staticmethod
    async def create(data: EmployeeCreate, session: AsyncSession) -> EmployeeOut:
        """Creates a new employee.

        Args:
            data (EmployeeCreate): The validated input data including roles.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            EmployeeOut: The created employee including their `E-<uuid4>` id.

        Raises:
            DuplicateKeyError: When the e-mail address or the initials are already taken.
        """
        created = await EmployeeRepository.create(data, session)
        logger.bind(request_id="-", employee_id=created.id, roles=created.roles).info(
            "Employee created"
        )

        await manager.send_event({
            "type": "event", "entity": "employee", "trigger": "employee_created",
            "reference": created.id, "ids": [created.id], "scope": "list",
        })
        return created

    @staticmethod
    async def update(
        employee_id: str, data: EmployeeUpdate, session: AsyncSession
    ) -> EmployeeOut:
        """Changes individual fields and/or replaces an employee's roles.

        Args:
            employee_id (str): Id of the employee to change.
            data (EmployeeUpdate): The fields to change.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            EmployeeOut: The updated employee.

        Raises:
            NotFoundError: When the id does not exist in the graph.
        """
        employee = await EmployeeRepository.update(employee_id, data, session)
        if not employee:
            raise NotFoundError(f"Employee with id {employee_id} was not found.")
        # `password` is filtered out on purpose: the log records WHICH fields changed, and
        # naming the password field alongside the others would be enough to tell an
        # attacker with log access whose credentials just moved.
        changed_fields = sorted(data.model_dump(exclude_unset=True).keys() - {"password"})
        logger.bind(request_id="-", employee_id=employee_id, fields=changed_fields).info(
            "Employee updated"
        )

        await manager.send_event({
            "type": "event", "entity": "employee", "trigger": "employee_updated",
            "reference": employee_id, "ids": [employee_id], "scope": "list",
        })
        return employee

    @staticmethod
    async def change_password(
        employee_id: str, data: PasswordChangeRequest, session: AsyncSession
    ) -> None:
        """Changes one's own password after verifying the current one.

        No event is sent here, unlike for the other writes: a password change alters
        nothing any other client displays, and broadcasting it would put the fact that
        this account just changed its credentials on every open socket.

        Args:
            employee_id (str): Id of the signed-in employee.
            data (PasswordChangeRequest): Current and new password.
            session (AsyncSession): The asynchronous Neo4j database session.

        Raises:
            NotFoundError: When the employee does not exist.
            BusinessLogicError: When the current password is wrong.
        """
        raw = await EmployeeRepository.get_raw_by_id(employee_id, session)
        if not raw:
            raise NotFoundError(f"Employee with id {employee_id} was not found.")
        if not verify_password(data.currentPassword, raw.get("password", "")):
            raise BusinessLogicError("The current password is wrong.")
        await EmployeeRepository.update(
            employee_id, EmployeeUpdate(password=data.newPassword), session
        )
        logger.bind(request_id="-", employee_id=employee_id).info("Password changed by the user")

    @staticmethod
    async def delete(employee_id: str, session: AsyncSession) -> None:
        """Deletes an employee together with every one of their edges.

        Args:
            employee_id (str): Id of the employee to delete.
            session (AsyncSession): The asynchronous Neo4j database session.

        Raises:
            NotFoundError: When the id does not exist in the graph.
        """
        deleted = await EmployeeRepository.delete(employee_id, session)
        if not deleted:
            raise NotFoundError(f"Employee with id {employee_id} was not found.")
        logger.bind(request_id="-", employee_id=employee_id).info("Employee deleted")

        await manager.send_event({
            "type": "event", "entity": "employee", "trigger": "employee_deleted",
            "reference": employee_id, "ids": [employee_id], "scope": "list",
        })
