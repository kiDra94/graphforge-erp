"""Cypher queries for customers: master data, account manager and contract assignment."""

from datetime import UTC, datetime
from uuid import uuid4

from neo4j import AsyncSession
from neo4j.exceptions import ConstraintError, Neo4jError
from pydantic import ValidationError

from core.exceptions import (
    BusinessLogicError,
    DatabaseError,
    DuplicateKeyError,
    NotFoundError,
)
from core.neo4j_query import read_many, read_single

from ..schemas_sales import (
    Customer,
    CustomerCreate,
    CustomerUpdate,
)

# Prefix of every customer number the server assigns. The part behind it is a uuid4 —
# see `CustomerRepository.create_customer` for why it is not a counter.
_CUSTOMER_NUMBER_PREFIX = "C-"


def _best_contract_rate(contract_rates: list[float]) -> float | None:
    """Picks the highest discount rate among the contracts of a customer valid today.

    A pure function without database access. The customer discount hangs exclusively off
    the contract — if a customer has several contracts valid at the same time (an own one
    and a global one, say), the higher rate wins instead of both adding up.

    Args:
        contract_rates (list[float]): `discountPercent` of every contract of the customer
            valid today, already without `None` values.

    Returns:
        float | None: The highest rate, or None without a single valid contract with a
            maintained rate.
    """
    return max(contract_rates) if contract_rates else None


# --- Reused query building blocks ---------------------------------------------

# The account manager hangs off an edge. It is collected first and then reduced to a
# single value: two edges would otherwise show the customer twice in the list.
_ACCOUNT_MANAGER_MATCH = """
OPTIONAL MATCH (e:Employee)-[:ACCOUNT_MANAGER_OF]->(c)
WITH c, e ORDER BY e.id
WITH c, collect(e)[0] AS e
"""

_CUSTOMER_PROJECTION = """
c{.*, accountManager: CASE WHEN e IS NULL THEN null
                           ELSE {id: toString(e.id), name: e.name} END} AS customer
"""

# For the detail view only: the customer discount hangs exclusively off the contract.
# Collected are the `discountPercent` values of every contract of the customer valid
# today — own ones through HAS_CONTRACT and global ones independently of that. Picking
# among the values is the job of `_best_contract_rate`. Requires c and e to be bound.
_CUSTOMER_CONTRACT_RATES_MATCH = """
CALL (c) {
    OPTIONAL MATCH (c)-[:HAS_CONTRACT]->(own:Contract)
    WHERE date() >= own.validFrom AND date() <= own.validTo
    RETURN collect(own.discountPercent) AS ownRates
}
CALL () {
    OPTIONAL MATCH (global:Contract {isGlobal: true})
    WHERE date() >= global.validFrom AND date() <= global.validTo
    RETURN collect(global.discountPercent) AS globalRates
}
WITH c, e, [r IN ownRates + globalRates WHERE r IS NOT NULL] AS contractRates
"""


class CustomerRepository:
    """Repository for the customer master data in Neo4j.

    Encapsulates the Cypher queries for the `Customer` node and the account manager
    relationship to the employee. The account manager deliberately sits on an edge and not
    as a property on the customer: a change of the person in charge is a change of
    assignment, not a changed value.
    """

    @staticmethod
    def _to_customer(record: dict) -> Customer:
        """Converts a result row of the customer query into a Pydantic schema.

        Expects the projected properties of the customer node, extended by the account
        manager as an embedded map. No value conversion happens here — the node carries no
        money field, and the timestamps are already converted by the type of the read
        model.

        Args:
            record (dict): The projected result row of a customer query.

        Returns:
            Customer: The validated customer object.

        Raises:
            DatabaseError: When the node contains data the read model cannot map.
        """
        try:
            return Customer.model_validate(dict(record))
        except ValidationError as e:
            raise DatabaseError(
                f"Customer '{record.get('id')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    async def get_customers(session: AsyncSession, search: str | None = None) -> list[Customer]:
        """Fetches every customer, optionally filtered by a free-text search.

        The search term runs across name, customer number, city and VAT id. A term
        consisting of nothing but whitespace produces no filter: a cleared search field in
        the interface arrives as an empty string, and read as a filter that would be a
        "contains nothing" — the list would look empty although nothing was searched for.

        Only fragments hard-coded in this module ever reach the query string; the search
        term itself is bound as a parameter.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            search (str | None): Free text across name, number, city and VAT id.

        Returns:
            list[Customer]: The customers found, or an empty list.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        params: dict = {}
        where_clause = ""

        if search and search.strip():
            where_clause = """
            WHERE toLower(c.name)  CONTAINS toLower($search)
               OR toLower(c.id)    CONTAINS toLower($search)
               OR toLower(c.city)  CONTAINS toLower($search)
               OR toLower(c.vatId) CONTAINS toLower($search)
            """
            params["search"] = search.strip()

        try:
            query = f"""
            MATCH (c:Customer)
            {where_clause}
            {_ACCOUNT_MANAGER_MATCH}
            RETURN {_CUSTOMER_PROJECTION}
            ORDER BY c.id
            """
            records = await read_many(session, query, **params)
            return [CustomerRepository._to_customer(record["customer"]) for record in records]
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the customers: {e}") from e

    @staticmethod
    async def get_customer(id: str, session: AsyncSession) -> Customer | None:
        """Fetches a single customer with account manager and effective contract discount.

        Reports an unknown customer as `None` instead of through an exception. Whether
        that is an error is decided by the service.

        Reads, on top of what the list reads, the contract rates valid today and computes
        `effectiveDiscountPercent` from them through `_best_contract_rate`. Deliberately
        only here and not in `_CUSTOMER_PROJECTION`: `GET /api/customers` stays unchanged
        that way.

        Args:
            id (str): The customer number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Customer | None: The customer, or None when no node with that number exists.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            query = f"""
            MATCH (c:Customer {{id: $id}})
            {_ACCOUNT_MANAGER_MATCH}
            {_CUSTOMER_CONTRACT_RATES_MATCH}
            RETURN {_CUSTOMER_PROJECTION}, contractRates
            """
            record = await read_single(session, query, id=id)
            if record is None:
                return None
            customer = CustomerRepository._to_customer(record["customer"])
            customer.effectiveDiscountPercent = _best_contract_rate(record["contractRates"])
            return customer
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the customer: {e}") from e

    @staticmethod
    async def create_customer(customer_data: CustomerCreate, session: AsyncSession) -> Customer:
        """Creates a new customer node and assigns the customer number.

        The number is generated server-side as `C-<uuid4>`. A consecutive counter would
        have to read the previous maximum before every write and would already be stale at
        the moment of writing — two concurrent creations would compute the same number, and
        one of them would fail on the constraint for no reason a caller could act on. The
        uuid removes the read as well as the race.

        The customer number stays the business key and the path parameter; only its shape
        changed. Numbers that came in through an import (`C-1001`) keep their own value —
        nothing counts over them any more, so nothing can collide with them either.

        If an `accountManagerId` is given, the relationship to the employee comes into
        existence as well.

        Fields that were not supplied go into the query as `null`; `SET c += $props` does
        not create a property with that value in the first place. A customer of which only
        the name is known therefore gets no empty placeholders on its node.

        Args:
            customer_data (CustomerCreate): The master data of the new customer.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Customer: The created customer including the assigned number and timestamp.

        Raises:
            NotFoundError: When the given `accountManagerId` belongs to no employee.
            DuplicateKeyError: When the assigned customer number is already taken. Kept as
                a path although a uuid collision is not a case anyone will see — the
                constraint stays the authority, and swallowing its error would mean
                answering 500 on the one day it does fire.
            DatabaseError: When the node was not created.
        """
        props = customer_data.model_dump(exclude={"accountManagerId"})
        props["createdAt"] = datetime.now(UTC)
        params: dict = {
            "props":            props,
            "accountManagerId": customer_data.accountManagerId,
        }

        create_query = f"""
        CREATE (c:Customer {{id: $id}})
        SET c += $props
        WITH c
        OPTIONAL MATCH (e:Employee) WHERE toString(e.id) = $accountManagerId
        FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
            MERGE (e)-[:ACCOUNT_MANAGER_OF]->(c))
        WITH c
        {_ACCOUNT_MANAGER_MATCH}
        RETURN {_CUSTOMER_PROJECTION}
        """

        async def _create_customer(tx, params):
            """Checks the employee, forms the number and creates the customer — atomically."""
            await CustomerRepository._check_employee(
                tx, params["accountManagerId"], "accountManagerId"
            )

            result = await tx.run(
                create_query, {**params, "id": f"{_CUSTOMER_NUMBER_PREFIX}{uuid4()}"}
            )
            return await result.single()

        try:
            record = await session.execute_write(_create_customer, params)
            if record is None:
                raise DatabaseError("Node was not created, Neo4j returned an empty result.")
            return CustomerRepository._to_customer(record["customer"])
        except ConstraintError as e:
            raise DuplicateKeyError(
                "The assigned customer number is already taken. Please repeat the operation."
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while creating the customer: {e}") from e

    @staticmethod
    async def update_customer(
        id: str, customer_data: CustomerUpdate, session: AsyncSession
    ) -> Customer | None:
        """Updates individual properties of an existing customer.

        Only what the client actually sent is written (`exclude_unset=True`). Without that
        restriction every remaining field rides along as `null`, and `SET c += $props`
        deletes them — a PATCH on the name would then wipe the whole address, and the call
        would still report success.

        An `accountManagerId` that is sent replaces the existing relationship instead of
        putting a second one beside it. A deliberately sent `null` dissolves the
        assignment — it is the only way to run a customer without an account manager
        again, and matches the behaviour of the remaining fields.

        Args:
            id (str): The customer number.
            customer_data (CustomerUpdate): The fields to change.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Customer | None: The updated customer, or None when no node with that number
                exists.

        Raises:
            NotFoundError: When the given `accountManagerId` belongs to no employee.
            BusinessLogicError: When not a single field was handed over to change.
            DatabaseError: On unexpected errors during the query.
        """
        props = customer_data.model_dump(exclude_unset=True)
        manager_given = "accountManagerId" in props
        manager_id = props.pop("accountManagerId", None)

        if not props and not manager_given:
            raise BusinessLogicError("No fields to update were handed over.")

        props["updatedAt"] = datetime.now(UTC)
        params: dict = {"id": id, "props": props, "accountManagerId": manager_id}

        # The old edge falls away in every case, the new one only comes into existence when
        # an id was sent. A MERGE without the preceding DELETE would hang a second account
        # manager beside it, and the customer would appear twice.
        manager_query = """
        MATCH (c:Customer {id: $id})
        OPTIONAL MATCH (:Employee)-[old:ACCOUNT_MANAGER_OF]->(c)
        DELETE old
        WITH c
        OPTIONAL MATCH (e:Employee) WHERE toString(e.id) = $accountManagerId
        FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
            MERGE (e)-[:ACCOUNT_MANAGER_OF]->(c))
        """

        read_query = f"""
        MATCH (c:Customer {{id: $id}})
        {_ACCOUNT_MANAGER_MATCH}
        RETURN {_CUSTOMER_PROJECTION}
        """

        async def _update_customer(tx, params):
            """Sets properties and the account manager together, or neither."""
            await CustomerRepository._check_employee(
                tx, params["accountManagerId"], "accountManagerId"
            )

            result = await tx.run(
                "MATCH (c:Customer {id: $id}) SET c += $props RETURN c.id AS id", params
            )
            if await result.single() is None:
                return None

            if manager_given:
                await (await tx.run(manager_query, params)).consume()

            return await (await tx.run(read_query, params)).single()

        try:
            record = await session.execute_write(_update_customer, params)
            if record is None:
                return None
            return CustomerRepository._to_customer(record["customer"])
        except Neo4jError as e:
            raise DatabaseError(f"Database error while updating the customer: {e}") from e

    @staticmethod
    async def _check_employee(tx, employee_id: str | None, field: str) -> None:
        """Makes sure a given employee id exists in the graph.

        Runs inside the running write transaction. An unknown id is not a schema violation
        — Pydantic does not see the graph — and is therefore answered as a 404, not a 422.

        Args:
            tx: The running Neo4j transaction.
            employee_id (str | None): The id to check. `None` is skipped.
            field (str): The field name from the request, for the error message.

        Raises:
            NotFoundError: When no employee with that id exists.
        """
        if employee_id is None:
            return

        result = await tx.run(
            "MATCH (e:Employee) WHERE toString(e.id) = $employeeId RETURN e.id AS id",
            {"employeeId": employee_id},
        )
        if await result.single() is None:
            raise NotFoundError(
                f"An employee with the id '{employee_id}' does not exist ({field})."
            )

    @staticmethod
    async def assign_contract(id: str, contract_id: str, session: AsyncSession) -> Customer | None:
        """Assigns a framework contract to a customer (edge `HAS_CONTRACT`).

        Args:
            id (str): The customer number.
            contract_id (str): The id of the contract to assign.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Customer | None: The customer after the assignment, or None when no customer
                with that number exists.

        Raises:
            NotFoundError: When no contract with that id exists.
            BusinessLogicError: When the contract is global — it then already applies to
                every customer without an assignment.
        """
        params: dict = {"id": id, "contractId": contract_id}
        read_query = f"""
        MATCH (c:Customer {{id: $id}})
        {_ACCOUNT_MANAGER_MATCH}
        RETURN {_CUSTOMER_PROJECTION}
        """

        async def _assign_contract(tx, params):
            """Checks the contract and links it to the customer — atomically."""
            contract_result = await tx.run(
                "MATCH (ct:Contract {id: $contractId}) RETURN ct.isGlobal AS isGlobal", params
            )
            contract_record = await contract_result.single()
            if contract_record is None:
                raise NotFoundError(
                    f"A contract with the id '{params['contractId']}' does not exist."
                )
            if contract_record["isGlobal"]:
                raise BusinessLogicError(
                    f"Contract '{params['contractId']}' is global and already applies to "
                    "every customer without an assignment."
                )

            customer_result = await tx.run(
                """
                MATCH (c:Customer {id: $id})
                MATCH (ct:Contract {id: $contractId})
                MERGE (c)-[:HAS_CONTRACT]->(ct)
                RETURN c.id AS id
                """,
                params,
            )
            if await customer_result.single() is None:
                return None

            return await (await tx.run(read_query, params)).single()

        try:
            record = await session.execute_write(_assign_contract, params)
            if record is None:
                return None
            return CustomerRepository._to_customer(record["customer"])
        except Neo4jError as e:
            raise DatabaseError(f"Database error while assigning the contract: {e}") from e
