"""Neo4j queries for the IAM domain."""

from uuid import uuid4

from neo4j import AsyncSession
from neo4j.exceptions import ConstraintError, Neo4jError
from pydantic import ValidationError

from core.exceptions import DatabaseError, DuplicateKeyError
from core.neo4j_query import read_many, read_single, write_summary
from core.security import hash_password

from .schemas_iam import EmployeeCreate, EmployeeOut, EmployeeUpdate

# Roles hang off the HAS_ROLE -> Role edge, not off a property on Employee.
# OPTIONAL MATCH rather than MATCH: an employee without a role would otherwise drop out
# of the result set entirely, instead of appearing with roles: []. The fragment is shared
# by three read queries (get_by_email, get_all, _GET_BY_ID_QUERY) — hence a constant
# instead of maintaining the same line in three places.
_ATTACH_ROLES = "OPTIONAL MATCH (e)-[:HAS_ROLE]->(r:Role) WITH e, collect(r.name) AS roles"

# Needed in three places: get_by_id() itself, plus the closing fetch after writing in
# create() and update(). The other queries in this file appear exactly once and therefore
# live in their method rather than up here.
_GET_BY_ID_QUERY = f"""
MATCH (e:Employee {{id: $id}})
{_ATTACH_ROLES}
RETURN properties(e) AS e, roles
"""

# The base role every functional role adds to. Each newly created employee gets it,
# independent of what the caller sends, and a replacing role list may not silently
# remove it.
_BASE_ROLE = "User"


class EmployeeRepository:
    """Encapsulates the Cypher queries for the `Employee` node.

    Two rules apply here as they do in every other domain:

    - **Multi-step writes run inside a single transaction function** (`create()`,
      `update()`) via `session.execute_write(...)` with `tx.run()` directly. An employee
      left standing between two steps without their role edges would be useless to every
      role-checked endpoint, without any error pointing at it.
    - **No `session.run()`.** Single read/write steps go through the helpers in
      `core/neo4j_query.py`, multi-step ones through their own transaction function.

    `employee_id` is a `str` everywhere, not an `int` — the same business-key convention
    as `Product.number` or `Customer.id` in the other domains. Ids created here follow the
    `E-<uuid4>` scheme.
    """

    @staticmethod
    def _to_dict(record: dict) -> dict:
        """Merges `properties(e)` and the separately collected `roles` into one dict.

        Args:
            record (dict): A record with the keys `e` (node properties, without `roles`)
                and `roles` (the list from `collect(r.name)`).

        Returns:
            dict: The merged employee data including `roles`.
        """
        merged = {**dict(record["e"]), "roles": record["roles"]}
        merged["id"] = str(merged["id"])
        return merged

    @staticmethod
    def _to_employee(record: dict) -> EmployeeOut:
        """Converts a merged record into the read model.

        Args:
            record (dict): A record in the shape `_to_dict()` expects.

        Returns:
            EmployeeOut: The validated read model.

        Raises:
            DatabaseError: When the node holds data the read model cannot represent. That
                is a server-side data problem, not a client error, and is therefore
                treated as a 500.
        """
        try:
            return EmployeeOut(**EmployeeRepository._to_dict(record))
        except ValidationError as e:
            raise DatabaseError(
                f"Employee '{record['e'].get('id')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    async def get_by_email(email: str, session: AsyncSession) -> dict | None:
        """Returns an employee including the password hash, for the login check.

        Args:
            email (str): The e-mail address to match on.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            dict | None: The raw employee dict including the `password` hash, or None when
                no e-mail matches. Deliberately not an `EmployeeOut`: the read model
                carries no password field, but `AuthService.login` needs it to verify.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            record = await read_single(
                session,
                f"""
                MATCH (e:Employee {{email: $email}})
                {_ATTACH_ROLES}
                RETURN properties(e) AS e, roles
                """,
                email=email,
            )
            return EmployeeRepository._to_dict(record) if record else None
        except Neo4jError as e:
            raise DatabaseError(f"Failed to fetch the employee: {e}") from e

    @staticmethod
    async def get_by_initials(initials: str, session: AsyncSession) -> dict | None:
        """Returns an employee including the password hash, for the login by initials.

        Args:
            initials (str): The initials, matched case-insensitively.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            dict | None: The raw employee dict including the `password` hash, or None.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            record = await read_single(
                session,
                f"""
                MATCH (e:Employee)
                WHERE toLower(e.initials) = toLower($initials)
                {_ATTACH_ROLES}
                RETURN properties(e) AS e, roles
                """,
                initials=initials,
            )
            return EmployeeRepository._to_dict(record) if record else None
        except Neo4jError as e:
            raise DatabaseError(f"Failed to fetch the employee: {e}") from e

    @staticmethod
    async def get_raw_by_id(employee_id: str, session: AsyncSession) -> dict | None:
        """Returns raw employee data including the password hash, for the password check.

        Args:
            employee_id (str): Id of the employee.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            dict | None: The raw employee dict including the `password` hash, or None.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            record = await read_single(session, _GET_BY_ID_QUERY, id=employee_id)
            return EmployeeRepository._to_dict(record) if record else None
        except Neo4jError as e:
            raise DatabaseError(f"Failed to fetch the employee: {e}") from e

    @staticmethod
    async def get_all(session: AsyncSession) -> list[EmployeeOut]:
        """Fetches every employee, ordered by name.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[EmployeeOut]: Every employee including their roles from the `HAS_ROLE`
                edge, or an empty list.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            records = await read_many(
                session,
                f"""
                MATCH (e:Employee)
                {_ATTACH_ROLES}
                RETURN properties(e) AS e, roles
                ORDER BY e.name
                """,
            )
            return [EmployeeRepository._to_employee(record) for record in records]
        except Neo4jError as e:
            raise DatabaseError(f"Failed to fetch the employees: {e}") from e

    @staticmethod
    async def get_by_id(employee_id: str, session: AsyncSession) -> EmployeeOut | None:
        """Fetches a single employee by id.

        Args:
            employee_id (str): Id of the employee.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            EmployeeOut | None: The employee including roles, or None when the id does not
                exist.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            record = await read_single(session, _GET_BY_ID_QUERY, id=employee_id)
            return EmployeeRepository._to_employee(record) if record else None
        except Neo4jError as e:
            raise DatabaseError(f"Failed to fetch the employee: {e}") from e

    @staticmethod
    async def create(data: EmployeeCreate, session: AsyncSession) -> EmployeeOut:
        """Creates an employee and sets their role edges.

        The id is generated server-side as `E-<uuid4>`. A sequential number would have to
        read the current maximum before every write and would already be stale by the time
        the write happens — two concurrent requests would receive the same id.

        Creating the node and setting the `HAS_ROLE` edges happen in this one transaction.

        Args:
            data (EmployeeCreate): The validated input data including roles.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            EmployeeOut: The created employee including the roles that were set.

        Raises:
            DuplicateKeyError: When the e-mail address or the initials are already taken.
            DatabaseError: On unexpected failures during the write.
        """
        employee_id = f"E-{uuid4()}"
        props = {
            "id":       employee_id,
            "name":     data.name,
            "initials": data.initials,
            "email":    data.email,
            "active":   True,
            "password": hash_password(data.password),
        }
        # `dict.fromkeys` keeps the order and drops an accidental duplicate, in case the
        # base role was sent along.
        roles = list(dict.fromkeys([*data.roles, _BASE_ROLE]))

        async def _create(tx):
            """Creates the node, draws the role edges and reads back — atomically."""
            await tx.run("CREATE (e:Employee $props)", {"props": props})
            # UNWIND over an empty $roles list yields zero iterations — MERGE simply does
            # not happen, without an error.
            await tx.run(
                """
                MATCH (e:Employee {id: $id})
                UNWIND $roles AS role_name
                MATCH (r:Role {name: role_name})
                MERGE (e)-[:HAS_ROLE]->(r)
                """,
                {"id": employee_id, "roles": roles},
            )
            return await (await tx.run(_GET_BY_ID_QUERY, {"id": employee_id})).single()

        if await EmployeeRepository.get_by_initials(data.initials, session):
            raise DuplicateKeyError(f"Initials '{data.initials}' are already taken.")

        try:
            record = await session.execute_write(_create)
            if record is None:
                raise DatabaseError("Employee was not created, Neo4j returned an empty result.")
            return EmployeeRepository._to_employee(record)
        except ConstraintError as e:
            raise DuplicateKeyError(f"E-mail '{data.email}' is already taken.") from e
        except Neo4jError as e:
            raise DatabaseError(f"Failed to create the employee: {e}") from e

    @staticmethod
    async def update(
        employee_id: str, data: EmployeeUpdate, session: AsyncSession
    ) -> EmployeeOut | None:
        """Updates individual fields and/or replaces an employee's roles.

        Writes only the fields actually sent (`model_dump(exclude_unset=True)`). A
        password that was sent is hashed. A role list that was sent REPLACES the existing
        `HAS_ROLE` edges rather than adding to them: all existing edges are deleted first
        and the new ones set afterwards, in the same transaction as the field changes.

        Args:
            employee_id (str): Id of the employee to change.
            data (EmployeeUpdate): The fields to change.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            EmployeeOut | None: The updated employee, or None when the id does not exist.

        Raises:
            DuplicateKeyError: When the new initials belong to somebody else.
            DatabaseError: When the request carries no field at all, or on unexpected
                failures during the write.
        """
        updates = data.model_dump(exclude_unset=True)
        new_roles = updates.pop("roles", None)
        if new_roles is not None:
            new_roles = list(dict.fromkeys([*new_roles, _BASE_ROLE]))
        if "password" in updates:
            updates["password"] = hash_password(updates.pop("password"))
        if not updates and new_roles is None:
            raise DatabaseError("No fields given to update.")

        if "initials" in updates:
            existing = await EmployeeRepository.get_by_initials(updates["initials"], session)
            if existing and existing.get("id") != employee_id:
                raise DuplicateKeyError(f"Initials '{updates['initials']}' are already taken.")

        async def _update(tx):
            """Sets fields and/or replaces the role edges and reads back — atomically."""
            if updates:
                await tx.run(
                    "MATCH (e:Employee {id: $id}) SET e += $props",
                    {"id": employee_id, "props": updates},
                )
            if new_roles is not None:
                # WITH DISTINCT e stops OPTIONAL MATCH/DELETE from multiplying the row
                # once per deleted edge before UNWIND $roles builds on it.
                await tx.run(
                    """
                    MATCH (e:Employee {id: $id})
                    OPTIONAL MATCH (e)-[old:HAS_ROLE]->()
                    DELETE old
                    WITH DISTINCT e
                    UNWIND $roles AS role_name
                    MERGE (r:Role {name: role_name})
                    MERGE (e)-[:HAS_ROLE]->(r)
                    """,
                    {"id": employee_id, "roles": new_roles},
                )
            return await (await tx.run(_GET_BY_ID_QUERY, {"id": employee_id})).single()

        try:
            record = await session.execute_write(_update)
            return EmployeeRepository._to_employee(record) if record else None
        except Neo4jError as e:
            raise DatabaseError(f"Failed to update the employee: {e}") from e

    @staticmethod
    async def delete(employee_id: str, session: AsyncSession) -> bool:
        """Deletes an employee together with every one of their edges.

        Args:
            employee_id (str): Id of the employee to delete.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            bool: True when a node was deleted, otherwise False.

        Raises:
            DatabaseError: On unexpected failures during the write.
        """
        try:
            summary = await write_summary(
                session, "MATCH (e:Employee {id: $id}) DETACH DELETE e", id=employee_id
            )
            return summary.counters.nodes_deleted > 0
        except Neo4jError as e:
            raise DatabaseError(f"Failed to delete the employee: {e}") from e
