"""Cypher queries of the procurement domain: suppliers, supply ranges and reorder suggestions."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from neo4j import AsyncSession
from neo4j.exceptions import ConstraintError, Neo4jError
from pydantic import ValidationError

from core.exceptions import BusinessLogicError, DatabaseError, DuplicateKeyError, NotFoundError
from core.neo4j_query import read_many, read_single, write_single

from .schemas_procurement import (
    ReorderSuggestion,
    Supplier,
    SupplierCreate,
    SupplierProductCreate,
    SupplierUpdate,
)


class SupplierRepository:
    """Repository for supplier master data and supply ranges in Neo4j.

    Encapsulates the Cypher queries maintaining the `Supplier` nodes as well as the
    `SUPPLIES_PRODUCT` edge the conditions hang off. The conditions deliberately sit on
    the edge and not on the product: the same product is available from several suppliers
    at different prices and lead times.
    """

    @staticmethod
    def _to_supplier(node_props: dict) -> Supplier:
        """Converts the properties of a Neo4j supplier node into a Pydantic schema.

        Expects a flat dictionary, the way the map projection `s{.*}` of the queries
        delivers it. No value conversion happens here: the node carries no money field,
        and the timestamps are already converted by the `Neo4jDatetime` type of the read
        model. Additional properties on the node are ignored by Pydantic on its own.

        Args:
            node_props (dict): The projected properties of the supplier node.

        Returns:
            Supplier: The validated supplier object.

        Raises:
            DatabaseError: When the node contains data the read model cannot map. That is
                a server-side data problem, not a client error, and is therefore treated
                as a 500.
        """
        props = dict(node_props)
        try:
            return Supplier.model_validate(props)
        except ValidationError as e:
            raise DatabaseError(
                f"Supplier '{props.get('id')}' '{props.get('name')}' could not be read "
                f"from the graph: {e}"
            ) from e

    @staticmethod
    async def get_supplier(id: str, session: AsyncSession) -> Supplier | None:
        """Runs a Cypher query to fetch a single supplier.

        Reports an unknown supplier as `None` instead of through an exception. Whether
        that is an error is decided by the service — were it raised here, no caller could
        handle the case differently.

        Args:
            id (str): The unique id of the supplier.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Supplier | None: The validated supplier object, or None when no node with
                that id exists.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            query = """
            MATCH (s:Supplier {id: $id})
            RETURN s{.*}
            """
            supplier = await read_single(session, query, id=id)
            if supplier:
                return SupplierRepository._to_supplier(supplier["s"])
            return None
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the supplier: {e}") from e

    @staticmethod
    async def get_suppliers(session: AsyncSession, search: str | None = None) -> list[Supplier]:
        """Fetches every supplier, optionally filtered by a free-text search.

        Builds the WHERE clause dynamically. Only fragments hard-coded in this module
        ever reach the query string — the search term itself is bound as a Cypher
        parameter and is therefore no attack surface.

        A search term consisting of nothing but whitespace produces no filter: a cleared
        search field in the interface arrives as an empty string, and read as a filter
        that would be a "contains nothing" — the list would look empty although nothing
        was searched for.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            search (str | None): Free text across name and city, case-insensitive.
                Without a value nothing is filtered.

        Returns:
            list[Supplier]: The suppliers found, or an empty list.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        conditions: list[str] = []
        params: dict = {}

        if search and search.strip():
            conditions.append(
                "(toLower(s.name) CONTAINS toLower($search)"
                " OR toLower(s.city) CONTAINS toLower($search))"
            )
            params["search"] = search.strip()

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        try:
            query = f"""
            MATCH (s:Supplier)
            {where_clause}
            RETURN s{{.*}}
            """
            records = await read_many(session, query, **params)
            return [SupplierRepository._to_supplier(record["s"]) for record in records]
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the suppliers: {e}") from e

    @staticmethod
    async def create_supplier(supplier_data: SupplierCreate, session: AsyncSession) -> Supplier:
        """Creates a new supplier node and assigns its id.

        The id is generated server-side as `S-<uuid4>`. A consecutive number would have to
        read the previous maximum before every write and would already be stale at the
        moment of writing — two concurrent creations would compute the same value, and one
        of them would fail on the constraint for a reason the caller cannot do anything
        about except try again.

        **One key, not two.** A separate display number used to sit beside the id, a
        consecutive `SUP-004` meant for the interface. Once the counter behind it became a
        uuid, the field said nothing the id did not already say — so it is gone. Suppliers
        that came in through an import keep whatever number their node carries; no schema
        reads it any more.

        Optional fields that were not supplied go into the query as `null`;
        `SET s += $props` does not create a property with that value in the first place.
        A supplier of which only the name is known therefore gets no empty placeholders
        on its node.

        Args:
            supplier_data (SupplierCreate): The master data of the new supplier.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Supplier: The created supplier including its id and the creation timestamp.

        Raises:
            DuplicateKeyError: When the assigned id is already taken. Kept as a path
                although a uuid collision is not a case anyone will see — the constraint
                stays the authority, and swallowing its error would mean answering 500 on
                the one day it does fire.
            DatabaseError: When the node was not created or the query fails unexpectedly.
        """
        try:
            props = supplier_data.model_dump()
            props["createdAt"] = datetime.now(UTC)
            props["id"] = f"S-{uuid4()}"
            query = """
            CREATE (s:Supplier)
            SET s += $props
            RETURN s{.*}
            """
            record = await write_single(session, query, props=props)
            if record is None:
                raise DatabaseError("Node was not created, Neo4j returned an empty result.")
            return SupplierRepository._to_supplier(record["s"])
        except ConstraintError as e:
            raise DuplicateKeyError(
                "The assigned supplier id is already taken. Please repeat the operation."
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while creating the supplier: {e}") from e

    @staticmethod
    async def update_supplier(
        id: str, supplier_data: SupplierUpdate, session: AsyncSession
    ) -> Supplier | None:
        """Updates individual properties of an existing supplier node.

        Builds the fields to be written through `model_dump(exclude_unset=True)`: only
        what the client actually sent gets set. Without that restriction every remaining
        field rides along as `null`, and `SET s += $props` deletes them — a PATCH on the
        contact would then wipe the entire remaining address, and the call would still
        report success.

        Args:
            id (str): The unique id of the supplier.
            supplier_data (SupplierUpdate): The fields to change.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Supplier | None: The updated supplier, or None when no node with that id
                exists.

        Raises:
            BusinessLogicError: When not a single field was handed over to change.
            DatabaseError: On unexpected errors during the query.
        """
        props_to_update = supplier_data.model_dump(exclude_unset=True)

        if not props_to_update:
            raise BusinessLogicError("No fields to update were handed over.")
        props_to_update["updatedAt"] = datetime.now(UTC)
        try:
            query = """
            MATCH (s:Supplier {id: $id})
            SET s += $props
            RETURN s{.*}
            """
            supplier = await write_single(session, query, id=id, props=props_to_update)
            if supplier:
                return SupplierRepository._to_supplier(supplier["s"])
            return None
        except Neo4jError as e:
            raise DatabaseError(f"Database error while updating the supplier: {e}") from e

    @staticmethod
    async def add_supplied_product(
        id: str, product_data: SupplierProductCreate, session: AsyncSession
    ) -> dict:
        """Sets the `SUPPLIES_PRODUCT` edge between a supplier and a product.

        Both nodes are looked up with `MATCH`, the `MERGE` runs over the edge alone. A
        `MERGE` across the whole path would create missing nodes: a call with an unknown
        id would then produce a supplier carrying nothing but an id and hang the edge off
        a freshly invented product — instead of a 404 a 201 on two ghost nodes would come
        back.

        The `MERGE` also makes the call repeatable: a second call for the same
        combination does not create a second edge but overwrites the conditions. Whether
        it was created or updated is revealed by comparing the creation timestamp with
        the time of the call; the `coalesce` catches imported edges that carry no
        `createdAt` yet.

        Args:
            id (str): The unique id of the supplier.
            product_data (SupplierProductCreate): Product number and conditions.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            dict: The condition that was set, with the purchase price in euro, extended
                by `wasUpdated` for the choice of the status code in the router.

        Raises:
            NotFoundError: When the supplier or the product does not exist.
            DatabaseError: When the edge was not set or the transaction fails
                unexpectedly.
        """
        exists_query = """
        OPTIONAL MATCH (s:Supplier {id: $id})
        OPTIONAL MATCH (p:Product  {number: $productNumber})
        RETURN s IS NOT NULL AS supplier, p IS NOT NULL AS product
        """
        merge_query = """
        MATCH (s:Supplier {id: $id})
        MATCH (p:Product  {number: $productNumber})
        MERGE (s)-[sp:SUPPLIES_PRODUCT]->(p)
            ON CREATE SET sp.createdAt = $now
            ON MATCH  SET sp.updatedAt = $now
        SET sp += $props
        RETURN
            s.id                   AS supplierId,
            p.number               AS productNumber,
            sp.leadTimeDays        AS leadTimeDays,
            sp.purchasePriceCent   AS purchasePriceCent,
            sp.isPreferredSupplier AS isPreferredSupplier,
            coalesce(sp.createdAt <> $now, true) AS wasUpdated
        """
        props = product_data.model_dump(exclude={"productNumber", "purchasePrice"})
        props["purchasePriceCent"] = int((product_data.purchasePrice * 100).to_integral_value())
        params: dict = {
            "id":            id,
            "productNumber": product_data.productNumber,
            "now":           datetime.now(UTC),
            "props":         props,
        }

        async def _add_supplied_product(tx, params):
            """Checks both nodes and sets the edge inside a single transaction.

            Nodes checked separately could disappear between the check and the write. The
            check also reveals WHICH of the two is missing — with two keys in the request,
            "something is missing" is not a usable answer.

            Args:
                tx: The running Neo4j transaction.
                params (dict): The bound query parameters.

            Returns:
                dict: The condition that was set, with the purchase price in euro.

            Raises:
                NotFoundError: When the supplier or the product does not exist.
                DatabaseError: When the MERGE returns nothing despite existing nodes.
            """
            exists_result = await tx.run(exists_query, params)
            result = await exists_result.single()

            if not result["product"]:
                raise NotFoundError(
                    f"A product with the number '{params['productNumber']}' does not exist."
                )
            if not result["supplier"]:
                raise NotFoundError(f"A supplier with the id '{params['id']}' does not exist.")

            merge_result = await tx.run(merge_query, params)
            record = await merge_result.single()

            if record is None:
                raise DatabaseError("Edge was not created, Neo4j returned an empty result.")

            return {
                "supplierId":          record["supplierId"],
                "productNumber":       record["productNumber"],
                "leadTimeDays":        record["leadTimeDays"],
                "purchasePrice":       Decimal(record["purchasePriceCent"]) / 100,
                "isPreferredSupplier": record["isPreferredSupplier"],
                "wasUpdated":          record["wasUpdated"],
            }

        try:
            return await session.execute_write(_add_supplied_product, params)
        except Neo4jError as e:
            raise DatabaseError(
                f"Database error while taking the product into the supply range: {e}"
            ) from e


class ReorderSuggestionRepository:
    """Repository for the weekly reorder analysis.

    Encapsulates the one query bringing together the product master, the stock levels and
    the supply ranges, plus the conversion of its result. There is no write operation
    here: the reorder analysis suggests, it does not order.
    """

    @staticmethod
    def _to_reorder_suggestion(record: dict) -> ReorderSuggestion:
        """Converts one result row of the reorder analysis into a Pydantic schema.

        Expects the raw result of the query with `productNumber`, `label`, `totalStock`,
        `minStock`, `targetStock`, `supplier`, `purchasePriceCent` and `leadTimeDays`.

        Converts the purchase price, stored internally as integer cents, into a euro
        Decimal. When the cent value is missing, `unitPrice` stays None — a product
        without a stored supplier has no price. A value of 0, by contrast, is taken over
        as a recorded 0.00 EUR and not swallowed into None.

        Also calculates the order quantity as the difference to the target stock. It must
        become neither negative nor empty: negative it would become on inconsistently
        maintained bounds, empty on a missing target stock. A missing target stock
        therefore counts as 0 and the difference is clamped at 0 — the product then
        appears with quantity 0 and stands out as incompletely maintained, instead of
        disappearing from the analysis.

        Args:
            record (dict): The raw result of one row of the reorder analysis.

        Returns:
            ReorderSuggestion: The validated suggestion with quantity and source of supply.

        Raises:
            DatabaseError: When the row contains data the read model cannot map. That is
                a server-side data problem, not a client error, and is therefore treated
                as a 500.
        """
        props = dict(record)

        cent = props.pop("purchasePriceCent", None)
        props["unitPrice"] = None if cent is None else Decimal(cent) / 100
        stock = props.pop("totalStock", None) or 0.0
        props["currentStock"] = stock

        target_stock = props.pop("targetStock", None) or 0
        props["suggestedQuantity"] = max(target_stock - stock, 0)

        try:
            return ReorderSuggestion.model_validate(props)
        except ValidationError as e:
            raise DatabaseError(
                f"Reorder suggestion for product '{props.get('productNumber')}' could not "
                f"be read from the graph: {e}"
            ) from e

    @staticmethod
    async def get_reorder_suggestions(session: AsyncSession) -> list[ReorderSuggestion]:
        """Determines every product whose stock has fallen below its minimum stock.

        Four rules sit inside the query itself, because they cannot be applied
        afterwards: the exclusion of replaced products, the mandatory filter on a
        maintained minimum stock, the stock determination even without a stock record,
        and the selection of the supplier.

        The supplier selection sorts by `isPreferredSupplier` descending and only then by
        the purchase price ascending — the preferred supplier therefore beats the cheaper
        offer. Sorting runs through `coalesce`, because `null` in Cypher sorts **before**
        `true` on a descending sort: an edge without the flag set would otherwise
        displace the actual preferred supplier.

        Nothing is calculated here — `targetStock` and `totalStock` go up raw and are
        turned into the order quantity in `_to_reorder_suggestion`.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[ReorderSuggestion]: The suggestions with quantity and source of supply,
                largest quantity first, or an empty list.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            query = """
            MATCH (p:Product)
            WHERE p.minStock IS NOT NULL
            // OPTIONAL MATCH is mandatory: a product without any stock record has an
            // effective stock of 0 and is the most urgent case there is. A plain MATCH
            // would drop exactly that one.
            OPTIONAL MATCH (p)-[:HAS_STOCK]->(s:StockLevel)
            WITH p, coalesce(sum(s.quantity), 0.0) AS totalStock
            WHERE totalStock < p.minStock
            OPTIONAL MATCH (sup:Supplier)-[sp:SUPPLIES_PRODUCT]->(p)
            WITH p, totalStock, sup, sp
            ORDER BY coalesce(sp.isPreferredSupplier, false) DESC, sp.purchasePriceCent ASC
            WITH p, totalStock, collect({supplier: sup, conditions: sp})[0] AS best
            RETURN
                p.number                          AS productNumber,
                p.label                           AS label,
                totalStock                        AS totalStock,
                p.minStock                        AS minStock,
                p.targetStock                     AS targetStock,
                best.supplier.name                AS supplier,
                best.conditions.purchasePriceCent AS purchasePriceCent,
                best.conditions.leadTimeDays      AS leadTimeDays
            ORDER BY coalesce(p.targetStock, 0) - totalStock DESC
            """
            records = await read_many(session, query)
            return [
                ReorderSuggestionRepository._to_reorder_suggestion(record)
                for record in records
            ]

        except Neo4jError as e:
            raise DatabaseError(
                f"Database error while determining the reorder suggestions: {e}"
            ) from e
