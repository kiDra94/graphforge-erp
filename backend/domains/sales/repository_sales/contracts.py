"""Cypher queries for framework contracts and their conditions."""

from uuid import uuid4

from neo4j import AsyncSession
from neo4j.exceptions import Neo4jError
from pydantic import ValidationError

from core.exceptions import (
    DatabaseError,
    NotFoundError,
)
from core.neo4j_query import read_many, read_single, write_single

from ..schemas_sales import (
    ConditionCreate,
    Contract,
    ContractCreate,
    ContractDetail,
)
from ._shared import _cent, _euro

# `active` is calculated, not stored — a contract ends through its validity dates, not
# through a switch. `date()` is the reference day "today", not the request date of a
# calculation: the list shows the current state of the contract, regardless of which day
# is currently being calculated for.
_CONTRACT_ACTIVE_FRAGMENT = "date() >= ct.validFrom AND date() <= ct.validTo"

# The customers are collected before the aggregation, filtering out the null row of an
# empty OPTIONAL MATCH chain — otherwise a contract without customers would carry the list
# `[null]` instead of `[]`.
_CONTRACT_CUSTOMERS_FRAGMENT = (
    "[cn IN customerNodes WHERE cn IS NOT NULL | {id: cn.id, name: cn.name}]"
)

_CONTRACT_LIST_QUERY = f"""
MATCH (ct:Contract)
OPTIONAL MATCH (c:Customer)-[:HAS_CONTRACT]->(ct)
WITH ct, collect(c) AS customerNodes
RETURN ct{{.*,
    active:    {_CONTRACT_ACTIVE_FRAGMENT},
    customers: {_CONTRACT_CUSTOMERS_FRAGMENT}
}} AS contract
ORDER BY ct.id
"""

_CONTRACT_DETAIL_QUERY = f"""
MATCH (ct:Contract {{id: $id}})
OPTIONAL MATCH (c:Customer)-[:HAS_CONTRACT]->(ct)
WITH ct, collect(c) AS customerNodes
OPTIONAL MATCH (ct)-[cf:CONDITION_FOR]->(p:Product)
WITH ct, customerNodes, collect(CASE WHEN p IS NULL THEN null ELSE {{
    productNumber:  p.number,
    label:          p.label,
    fixedPriceCent: cf.fixedPriceCent
}} END) AS rawConditions
RETURN ct{{.*,
    active:     {_CONTRACT_ACTIVE_FRAGMENT},
    customers:  {_CONTRACT_CUSTOMERS_FRAGMENT},
    conditions: [cd IN rawConditions WHERE cd IS NOT NULL]
}} AS contract
"""


class ContractRepository:
    """Data access for framework contracts and their conditions."""

    @staticmethod
    def _to_contract(record: dict) -> Contract:
        """Converts a result row of the contract list into a Pydantic schema."""
        try:
            return Contract.model_validate(dict(record))
        except ValidationError as e:
            raise DatabaseError(
                f"Contract '{record.get('id')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    def _to_contract_detail(record: dict) -> ContractDetail:
        """Converts a result row of the contract detail query.

        `fixedPriceCent` is converted into `fixedPrice` in euro here — the only place a
        condition leaves the graph.
        """
        data = dict(record)
        data["conditions"] = [
            {
                "productNumber": condition["productNumber"],
                "label":         condition.get("label"),
                "fixedPrice":    _euro(condition.get("fixedPriceCent")),
            }
            for condition in data.get("conditions", [])
        ]
        try:
            return ContractDetail.model_validate(data)
        except ValidationError as e:
            raise DatabaseError(
                f"Contract '{data.get('id')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    async def get_contracts(session: AsyncSession) -> list[Contract]:
        """Fetches every framework contract with the customers assigned to it.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[Contract]: The existing contracts, sorted by id.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            records = await read_many(session, _CONTRACT_LIST_QUERY)
            return [ContractRepository._to_contract(record["contract"]) for record in records]
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the contracts: {e}") from e

    @staticmethod
    async def get_contract(id: str, session: AsyncSession) -> ContractDetail | None:
        """Fetches a framework contract with all its conditions.

        Args:
            id (str): The id of the contract.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ContractDetail | None: The complete contract, or None when no contract with
                that id exists.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            record = await read_single(session, _CONTRACT_DETAIL_QUERY, id=id)
            if record:
                return ContractRepository._to_contract_detail(record["contract"])
            return None
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the contract: {e}") from e

    @staticmethod
    async def create_contract(
        contract_data: ContractCreate, session: AsyncSession
    ) -> ContractDetail:
        """Creates a new framework contract.

        The id is generated server-side as `contract-<uuid4>`. Existing contract ids are
        left untouched — the id format is no business contract, and rewriting them would
        only break existing references.

        Args:
            contract_data (ContractCreate): The contract data.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ContractDetail: The created contract, still without customers or conditions.

        Raises:
            DatabaseError: When the node was not created.
        """
        props = contract_data.model_dump()
        props["id"] = f"contract-{uuid4()}"

        query = f"""
        CREATE (ct:Contract)
        SET ct += $props
        RETURN ct{{.*, active: {_CONTRACT_ACTIVE_FRAGMENT}, customers: [], conditions: []}} AS contract
        """
        try:
            record = await write_single(session, query, props=props)
            if record is None:
                raise DatabaseError("Contract was not created, Neo4j returned an empty result.")
            return ContractRepository._to_contract_detail(record["contract"])
        except Neo4jError as e:
            raise DatabaseError(f"Database error while creating the contract: {e}") from e

    @staticmethod
    async def add_condition(
        contract_id: str, condition_data: ConditionCreate, session: AsyncSession
    ) -> ContractDetail | None:
        """Stores a condition for a product inside a contract.

        Idempotent through `MERGE` on the `CONDITION_FOR` edge: a second call for the same
        product overwrites the condition instead of putting a second one beside it.

        Args:
            contract_id (str): The id of the contract.
            condition_data (ConditionCreate): Product number and fixed price.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ContractDetail | None: The contract with the new condition, or None when no
                contract with that id exists.

        Raises:
            NotFoundError: When no product with the given number exists.
        """
        params: dict = {
            "contractId":     contract_id,
            "productNumber":  condition_data.productNumber,
            "fixedPriceCent": _cent(condition_data.fixedPrice),
        }

        async def _add_condition(tx, params):
            """Checks contract and product and sets the condition — atomically."""
            contract_result = await tx.run(
                "MATCH (ct:Contract {id: $contractId}) RETURN ct.id AS id", params
            )
            if await contract_result.single() is None:
                return None

            product_result = await tx.run(
                "MATCH (p:Product {number: $productNumber}) RETURN p.number AS number", params
            )
            if await product_result.single() is None:
                raise NotFoundError(
                    f"A product with the number '{params['productNumber']}' does not exist."
                )

            await (
                await tx.run(
                    """
                    MATCH (ct:Contract {id: $contractId})
                    MATCH (p:Product {number: $productNumber})
                    MERGE (ct)-[c:CONDITION_FOR]->(p)
                    SET c.fixedPriceCent = $fixedPriceCent
                    """,
                    params,
                )
            ).consume()

            return await (
                await tx.run(_CONTRACT_DETAIL_QUERY, {"id": params["contractId"]})
            ).single()

        try:
            record = await session.execute_write(_add_condition, params)
            if record is None:
                return None
            return ContractRepository._to_contract_detail(record["contract"])
        except Neo4jError as e:
            raise DatabaseError(f"Database error while storing the condition: {e}") from e
