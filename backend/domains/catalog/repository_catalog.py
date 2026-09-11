"""Cypher queries of the catalog domain: product nodes and CONTAINS edges."""

from datetime import UTC, datetime
from decimal import Decimal

from neo4j import AsyncSession
from neo4j.exceptions import ConstraintError, Neo4jError
from pydantic import ValidationError

from core.exceptions import BusinessLogicError, DatabaseError, DuplicateKeyError, NotFoundError
from core.neo4j_query import read_many, read_single, write_single, write_summary

from .schemas_catalog import (
    BomLineCreate,
    BomLineDeleted,
    CategoryOption,
    Product,
    ProductCreate,
    ProductType,
    ProductUpdate,
)

# Map projection for every product node that is read. `.*` takes over all properties of
# the node, the following keys add the derived fields.
#
# The order matters: an explicitly set key wins over `.*`. That way the computed `hasBom`
# reliably displaces a same-named property from an import — the graph structure is the
# truth, not a stored flag.
#
# `type` and `hasBom` check the same condition and are therefore redundant; both are
# delivered separately because both appear in the API contract.
#
# category/productGroup/subcategory/suppliers use pattern comprehensions instead of a
# preceding OPTIONAL MATCH, because this projection is inserted as a pure expression into
# existing queries (see get_bom) where no WITH may precede it. `head()` yields null when
# the product is assigned to no group. A maintained `name` wins; otherwise a placeholder
# built from the id keeps the selection list usable.
#
# suppliers reads the same SUPPLIES_PRODUCT edge the procurement domain writes to —
# embedded on the product here so the comparison view in purchasing gets every supplier in
# one request instead of one request per product. purchasePriceCent stays in cents; the
# conversion to euro happens in _to_product, like every other price field.
_PRODUCT_PROJECTION = """{var}{
        .*,
        type: CASE WHEN EXISTS { ({var})-[:CONTAINS]->() } THEN 'Assembly' ELSE 'Part' END,
        hasBom: EXISTS { ({var})-[:CONTAINS]->() },
        active: coalesce({var}.active, true),
        stockEffect: coalesce({var}.stockEffect, 'direct'),
        subcategory: head([({var})-[:BELONGS_TO]->(s:Subcategory) | {id: s.id, name: coalesce(s.name, 'Subcategory ' + toString(s.id))}]),
        productGroup: head([({var})-[:BELONGS_TO]->(:Subcategory)-[:PART_OF]->(g:ProductGroup) | {id: g.id, name: coalesce(g.name, 'Group ' + toString(g.id))}]),
        category: head([({var})-[:BELONGS_TO]->(:Subcategory)-[:PART_OF]->(:ProductGroup)-[:BELONGS_TO_CATEGORY]->(c:Category) | {id: c.id, name: coalesce(c.name, 'Category ' + toString(c.id))}]),
        suppliers: [(sup:Supplier)-[sp:SUPPLIES_PRODUCT]->({var}) | {supplierId: sup.id, leadTimeDays: sp.leadTimeDays, purchasePriceCent: sp.purchasePriceCent, isPreferredSupplier: coalesce(sp.isPreferredSupplier, false)}]
    }"""


def product_projection(variable: str = "p") -> str:
    """Builds the product map projection for a concrete Cypher variable.

    Args:
        variable (str): Name of the node variable in the query (e.g. "p", "child").

    Returns:
        str: The projection fragment, ready to be inserted.
    """
    return _PRODUCT_PROJECTION.replace("{var}", variable)


class ProductRepository:
    """Repository for product master data in Neo4j.

    Encapsulates the Cypher queries for all CRUD operations on single `Product` nodes.
    """

    @staticmethod
    def _to_product(node_props: dict) -> Product:
        """Converts the properties of a Neo4j product node into the Pydantic schema.

        Expects the result of the map projection from `product_projection`, not the raw
        node properties: `type`, `hasBom` and `active` are already computed in Cypher and
        present in the given dictionary. When they are missing — because a query did not
        use the projection — the read model's defaults apply.

        Converts the sales price, stored internally as integer cents (`listPriceCent`),
        into a euro Decimal. When the cent value is missing, `listPrice` is deliberately
        set to None instead of falling back to a euro float property that might still be
        stuck on the node: such a fallback would permanently legitimise two competing
        storage formats and hide a migration that never ran. A missing price is visible, a
        quietly wrong one is not.

        Args:
            node_props (dict): The raw properties of the product node from Neo4j.

        Returns:
            Product: The validated product object.

        Raises:
            DatabaseError: When the node holds data the read model cannot represent. That
                is a server-side data problem, not a client error, and is therefore
                treated as a 500.
        """
        props = dict(node_props)

        # Same cent/euro detour for all three price fields. They are stored as integer
        # cents so no rounding error can accumulate in the graph.
        for cent_field, euro_field in (
            ("listPriceCent", "listPrice"),
            ("laborRateCent", "laborRate"),
            ("costPriceCent", "costPrice"),
        ):
            cent = props.pop(cent_field, None)
            props[euro_field] = None if cent is None else Decimal(cent) / 100

        # The same cent/euro conversion per entry in suppliers. The pattern comprehension
        # in _PRODUCT_PROJECTION deliberately returns the raw cent value, so the
        # conversion happens in this one place instead of being duplicated in Cypher.
        supplier_list = []
        for supplier in props.get("suppliers") or []:
            supplier = dict(supplier)
            cent = supplier.pop("purchasePriceCent", None)
            supplier["purchasePrice"] = None if cent is None else Decimal(cent) / 100
            supplier_list.append(supplier)
        props["suppliers"] = supplier_list

        try:
            return Product.model_validate(props)
        except ValidationError as e:
            raise DatabaseError(
                f"Product '{props.get('number')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    async def get_product(number: str, session: AsyncSession) -> Product | None:
        """Fetches a single product by its number.

        Args:
            number (str): The unique product number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Product | None: The validated product object, or None when not found.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            query = f"""
            MATCH (p:Product {{number: $number}})
            RETURN {product_projection()} AS p
            """
            record = await read_single(session, query, number=number)

            if record:
                return ProductRepository._to_product(record["p"])
            return None
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the product: {e}") from e

    @staticmethod
    async def get_products(
        session: AsyncSession,
        search: str | None = None,
        type: ProductType | None = None,
        active: bool = True,
    ) -> list[Product]:
        """Fetches product nodes from the graph, optionally filtered.

        Assembles the WHERE clause dynamically from the filters that are set. Only
        fragments hard-coded here ever reach the query string — every value coming from
        the client is bound as a Cypher parameter.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            search (str | None): Free text, matched case-insensitively against `number`
                and `label`.
            type (ProductType | None): Restricts to parts or assemblies through the
                labels. None returns both.
            active (bool): Filters on the active status.

        Returns:
            list[Product]: The products found, or an empty list.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        conditions: list[str] = []
        params: dict = {}

        if search:
            # The parentheses are mandatory: without them the AND of the other filters
            # binds tighter than this OR, and a search would override the other conditions
            # instead of adding to them.
            conditions.append(
                "(toLower(p.number) CONTAINS toLower($search)"
                " OR toLower(p.label) CONTAINS toLower($search))"
            )
            params["search"] = search

        # Labels cannot be parameterised in Cypher. So it is not the client's value that
        # lands in the query, but one of two fixed fragments.
        if type == "Assembly":
            conditions.append("p:Assembly")
        elif type == "Part":
            conditions.append("p:Part")

        # A node that never had the `active` property set would compare against null
        # without the coalesce, and the list would stay empty forever.
        conditions.append("coalesce(p.active, true) = $active")
        params["active"] = active

        where_clause = "WHERE " + " AND ".join(conditions)

        try:
            query = f"""
            MATCH (p:Product)
            {where_clause}
            RETURN {product_projection()} AS p
            """
            records = await read_many(session, query, **params)
            return [ProductRepository._to_product(record["p"]) for record in records]
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the product list: {e}") from e

    @staticmethod
    def _product_props(product: ProductCreate) -> dict:
        """Builds the node properties to write from the input data.

        Cent conversion of the three price fields, `active: true` and the creation
        timestamp.
        """
        # `subcategoryId` is excluded: the category hangs off the BELONGS_TO edge, not off
        # a property on the node. Without the exclusion, model_dump() would write it as a
        # loose value onto the product, where no query reads it.
        props = product.model_dump(exclude={"subcategoryId"})
        props["createdAt"] = datetime.now(UTC)

        props["listPriceCent"] = int((product.listPrice * 100).to_integral_value())
        del props["listPrice"]
        props["laborRateCent"] = (
            None if product.laborRate is None
            else int((product.laborRate * 100).to_integral_value())
        )
        del props["laborRate"]
        props["costPriceCent"] = (
            None if product.costPrice is None
            else int((product.costPrice * 100).to_integral_value())
        )
        del props["costPrice"]

        # `active` deliberately has no place in the write model — a newly created product
        # is active by definition. The property is set anyway, so new nodes hold the same
        # state as imported ones instead of depending on the coalesce when read.
        props["active"] = True
        return props

    @staticmethod
    async def create_product(product: ProductCreate, session: AsyncSession) -> Product:
        """Creates a new product node in Neo4j.

        Args:
            product (ProductCreate): The product data to store.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Product: The created and validated product object.

        Raises:
            DuplicateKeyError: On a unique constraint violation in Neo4j.
            DatabaseError: On unexpected database failures.
        """
        try:
            props = ProductRepository._product_props(product)
            # A freshly created product has no bill of materials and is a part by
            # definition. The label is set here so the invariant "every product carries
            # exactly one of :Assembly or :Part" holds for products created through the
            # API too, not only for imported ones.
            #
            # The category edge is drawn in the same transaction. OPTIONAL MATCH rather
            # than MATCH: an unknown subcategory should not prevent the product; it simply
            # stays unassigned, the same behaviour as sending none.
            query = f"""
            CREATE (p:Product:Part)
            SET p += $props
            WITH p
            OPTIONAL MATCH (s:Subcategory {{id: $subcategoryId}})
            FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
                MERGE (p)-[:BELONGS_TO]->(s))
            RETURN {product_projection()} AS p
            """
            record = await write_single(
                session, query, props=props, subcategoryId=product.subcategoryId
            )

            if record is None:
                raise DatabaseError("Node was not created, Neo4j returned an empty result.")

            return ProductRepository._to_product(record["p"])

        except ConstraintError as e:
            raise DuplicateKeyError(
                f"A product with the number '{product.number}' already exists."
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while creating the product: {e}") from e

    @staticmethod
    async def update_product(
        number: str, update_data: ProductUpdate, session: AsyncSession
    ) -> Product | None:
        """Updates individual fields of a product node (PATCH behaviour).

        Ignores fields that were not sent, so partial updates work, and sets the
        `updatedAt` timestamp.

        Args:
            number (str): Number of the product to update.
            update_data (ProductUpdate): The new data for the product.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Product | None: The updated product, or None when it does not exist.

        Raises:
            BusinessLogicError: When no fields were given to update.
            DuplicateKeyError: When `number` is changed to an already taken product number.
            DatabaseError: On failures during the database call.
        """
        # Fields that were not sent are ignored; no null is written for them.
        properties_to_update = update_data.model_dump(exclude_unset=True)

        if not properties_to_update:
            raise BusinessLogicError("No fields given to update.")

        # None is a legitimate target state for the two optional prices (removing the
        # value), not just "not sent" — `SET p += $props` deletes the property then.
        for euro_field, cent_field in (
            ("listPrice", "listPriceCent"),
            ("laborRate", "laborRateCent"),
            ("costPrice", "costPriceCent"),
        ):
            if euro_field in properties_to_update:
                value = properties_to_update.pop(euro_field)
                properties_to_update[cent_field] = (
                    None if value is None else int((value * 100).to_integral_value())
                )

        # The category is an edge, not a property — so take it out of the properties and
        # handle it separately below. `None` means "not sent" here and leaves the
        # assignment untouched; clearing an assignment is not a use case, since a product
        # always belongs to a category.
        subcategory_id = properties_to_update.pop("subcategoryId", None)

        properties_to_update["updatedAt"] = datetime.now(UTC)
        try:
            # DELETE before MERGE: a product belongs to exactly ONE subcategory. Without
            # the deletion the edges would accumulate, and the projection's `head()` would
            # pick an arbitrary one of them.
            group_fragment = """
            WITH p
            OPTIONAL MATCH (s:Subcategory {id: $subcategoryId})
            FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
                FOREACH (old IN [(p)-[r:BELONGS_TO]->(:Subcategory) | r] | DELETE old)
                MERGE (p)-[:BELONGS_TO]->(s))
            """ if subcategory_id is not None else ""

            query = f"""
            MATCH (p:Product {{number: $number}})
            SET p += $props
            {group_fragment}
            RETURN {product_projection()} AS p
            """
            record = await write_single(
                session, query, number=number, props=properties_to_update,
                subcategoryId=subcategory_id,
            )

            if record:
                return ProductRepository._to_product(record["p"])
            return None

        # An update may change the product number — to fix a typo made at creation without
        # deleting the product and rebuilding its bill of materials. If the new number
        # hits an existing one, that violates the unique constraint. This is a client
        # error and belongs in a 409, not in a 500 like an unhandled Neo4jError.
        except ConstraintError as e:
            new_number = properties_to_update.get("number")
            raise DuplicateKeyError(
                f"A product with the number '{new_number}' already exists."
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while updating the product: {e}") from e

    @staticmethod
    async def delete_product(number: str, session: AsyncSession) -> bool:
        """Deletes a product node and all of its relationships (DETACH DELETE).

        Args:
            number (str): The unique product number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            bool: True when at least one node was deleted.

        Raises:
            DatabaseError: On unexpected database failures.
        """
        try:
            query = """
            MATCH (p:Product {number: $number})
            DETACH DELETE p
            """
            summary = await write_summary(session, query, number=number)

            return summary.counters.nodes_deleted > 0

        except Neo4jError as e:
            raise DatabaseError(f"Database error while deleting the product: {e}") from e

    @staticmethod
    async def check_product_exists(number: str, session: AsyncSession) -> bool:
        """Checks with a fast count query whether a product exists in the graph.

        Args:
            number (str): The unique product number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            bool: True when the product node exists, otherwise False.

        Raises:
            DatabaseError: On unexpected Neo4j failures.
        """
        query = "MATCH (p:Product {number: $number}) RETURN count(p) > 0 AS exists"

        try:
            record = await read_single(session, query, number=number)
            return record["exists"] if record else False
        except Neo4jError as e:
            raise DatabaseError(f"Database error during the existence check: {e}") from e


class BomRepository:
    """Repository for all bill-of-materials operations.

    Encapsulates the graph logic around the `CONTAINS` edges: linking products, resolving
    bill-of-materials trees and the label bookkeeping that goes with it.
    """

    @staticmethod
    async def add_component(
        number: str, line: BomLineCreate, session: AsyncSession
    ) -> dict:
        """Adds a component to a parent product as an edge.

        Creates a `CONTAINS` edge carrying the quantity between parent and child, and adds
        the `Assembly` label to the parent node.

        The cycle guard is part of the query, not a separate check: `OPTIONAL MATCH
        path = (child)-[:CONTAINS*0..]->(parent)` followed by `WHERE path IS NULL` drops
        the row whenever the child already reaches the parent — which covers the direct
        self-reference (`*0..` matches the zero-length path) as well as any longer cycle.
        A check running before the write could go stale between check and write.

        Args:
            number (str): Number of the parent product.
            line (BomLineCreate): The component and its quantity.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            dict: The bill-of-materials line plus `wasUpdated` — the router tells a newly
                created edge (201) from an updated one (200) by it.

        Raises:
            NotFoundError: When the parent product does not exist.
            BusinessLogicError: When the component does not exist or a cycle was detected.
            DatabaseError: On unexpected failures while creating the edge.
        """
        exists_query = "MATCH (p:Product {number: $number}) RETURN count(p) > 0 AS exists"
        merge_query = f"""
        MATCH (parent:Product {{number: $parentNumber}})
        MATCH (child:Product {{number: $childNumber}})
        OPTIONAL MATCH path = (child)-[:CONTAINS*0..]->(parent)
        WITH parent, child, path
        WHERE path IS NULL
        OPTIONAL MATCH (parent)-[existing:CONTAINS]->(child)
        WITH parent, child, existing IS NOT NULL AS wasUpdated
        MERGE (parent)-[e:CONTAINS]->(child)
        SET parent:Assembly,
            e.quantity = $quantity,
            e.unit = coalesce($unit, e.unit)
        REMOVE parent:Part
        RETURN e.quantity AS quantity, {product_projection("child")} AS component, wasUpdated
        """

        async def _add_component(tx, exists_query, merge_query, params):
            # Transaction function for session.execute_write: runs the existence check and
            # the merge in one transaction, so no concurrent write can slip between them.
            exists_result = await tx.run(exists_query, {"number": params["parentNumber"]})
            exists_record = await exists_result.single()
            if not exists_record["exists"]:
                raise NotFoundError(
                    f"An assembly with the number '{params['parentNumber']}' does not exist."
                )

            merge_result = await tx.run(merge_query, params)
            return await merge_result.single()

        try:
            record = await session.execute_write(
                _add_component,
                exists_query,
                merge_query,
                {
                    "parentNumber": number,
                    "childNumber": line.componentNumber,
                    "quantity": line.quantity,
                    "unit": line.unit,
                },
            )

            if not record:
                raise BusinessLogicError(
                    f"Component {line.componentNumber} could not be added. "
                    "A cycle was detected, or the component does not exist."
                )

            return {
                "quantity": record["quantity"],
                "component": ProductRepository._to_product(record["component"]),
                "subComponents": [],
                "wasUpdated": record["wasUpdated"],
            }
        except Neo4jError as e:
            raise DatabaseError(f"Database error while adding the component: {e}") from e

    @staticmethod
    async def delete_component(
        number: str, component_number: str, session: AsyncSession
    ) -> BomLineDeleted | None:
        """Removes a component from a product's bill of materials.

        Deletes the `CONTAINS` edge between parent and component only — both product nodes
        stay untouched. If it was the last component, the parent loses the `Assembly`
        label and becomes a `Part` again. Deletion, count and label correction run in one
        transaction, so no concurrent read can see a parent without a bill of materials
        that is still labelled as an assembly.

        Args:
            number (str): Number of the parent product.
            component_number (str): Number of the component to remove.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            BomLineDeleted | None: The result including the number of remaining lines, or
                None when the edge does not exist.

        Raises:
            DatabaseError: On unexpected failures during the deletion.
        """
        delete_query = """
        MATCH (p:Product {number: $number})-[e:CONTAINS]->(c:Product {number: $componentNumber})
        DELETE e
        RETURN p.number AS number, c.number AS componentNumber
        """
        count_query = """
        MATCH (p:Product {number: $number})-[e:CONTAINS]->()
        RETURN count(e) AS remainingLines
        """
        reset_label_query = """
        MATCH (p:Product {number: $number})
        REMOVE p:Assembly
        SET p:Part
        """

        async def _delete_component(tx):
            # Transaction function for session.execute_write: deletes the edge, counts the
            # remaining lines and corrects the parent's labels when needed.
            delete_result = await tx.run(
                delete_query, {"number": number, "componentNumber": component_number}
            )
            delete_record = await delete_result.single()

            if delete_record is None:
                return None

            count_result = await tx.run(count_query, {"number": number})
            count_record = await count_result.single()
            remaining_lines = count_record["remainingLines"]

            if remaining_lines == 0:
                await tx.run(reset_label_query, {"number": number})

            return {
                "number": delete_record["number"],
                "componentNumber": delete_record["componentNumber"],
                "remainingLines": remaining_lines,
            }

        try:
            result = await session.execute_write(_delete_component)

            if result is None:
                return None

            return BomLineDeleted.model_validate(result)

        except Neo4jError as e:
            raise DatabaseError(f"Database error while removing the component: {e}") from e

    @staticmethod
    async def get_bom(number: str, depth: int, session: AsyncSession) -> list[dict]:
        """Resolves the bill of materials recursively.

        Walks every `CONTAINS` edge below the given number up to the requested depth with
        a variable-length path, then builds the nested tree in Python. The flattening in
        Cypher and the tree building here are split on purpose: a single query returning
        the nested structure would either need one round trip per level or a proprietary
        procedure, while one flat edge list plus a local grouping stays a single round trip
        and one readable function.

        Args:
            number (str): The starting product number (root of the bill of materials).
            depth (int): The maximum search depth; -1 means unbounded.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[dict]: A hierarchical representation of the bill of materials.

        Raises:
            DatabaseError: On failures while querying or traversing.
        """
        # -1 means "unbounded" -> map to a large value. Cypher needs a literal here; the
        # value is derived from an int the router already validated with ge=-1, so nothing
        # the client sends reaches the query as text.
        effective_depth = 100 if depth < 0 else depth

        query = f"""
        MATCH (root:Product {{number: $number}})
        MATCH path = (root)-[:CONTAINS*1..{effective_depth}]->(:Product)
        WITH relationships(path) AS pathRels
        UNWIND pathRels AS rel
        WITH DISTINCT rel, startNode(rel) AS start, endNode(rel) AS child
        RETURN start.number AS parentNumber,
               rel.quantity AS quantity,
               {product_projection("child")} AS childProps
        """

        try:
            records = await read_many(session, query, number=number)
            edges = [
                {
                    "parentNumber": record["parentNumber"],
                    "quantity": record["quantity"],
                    "childNumber": record["childProps"]["number"],
                    "child": ProductRepository._to_product(record["childProps"]),
                }
                for record in records
            ]

            children_by_parent: dict[str, list[dict]] = {}
            for edge in edges:
                children_by_parent.setdefault(edge["parentNumber"], []).append(edge)

            def build_tree(parent_number: str, visited: set[str] | None = None) -> list[dict]:
                """Builds the bill-of-materials tree below a product recursively.

                `visited` breaks cycles: were the data to contain an assembly that contains
                itself, the recursion would otherwise never end. A copy is passed down per
                branch, so the same product may still appear in different branches — only
                repetition within one path is forbidden.
                """
                if visited is None:
                    visited = set()
                if parent_number in visited:
                    return []
                visited.add(parent_number)

                children = []
                for edge in children_by_parent.get(parent_number, []):
                    children.append({
                        "quantity": edge["quantity"],
                        "component": edge["child"],
                        "subComponents": build_tree(edge["childNumber"], visited.copy()),
                    })
                return children

            return build_tree(number)
        except Neo4jError as e:
            raise DatabaseError(
                f"Database error while resolving the bill of materials: {e}"
            ) from e


# Two-stage collect(): first gather every subcategory per (category, product group), then
# every product group per category together with its already gathered subcategories — a
# single MATCH across all three levels would multiply them crosswise.
_CATEGORY_HIERARCHY_QUERY = """
MATCH (c:Category)
OPTIONAL MATCH (c)<-[:BELONGS_TO_CATEGORY]-(g:ProductGroup)
OPTIONAL MATCH (g)<-[:PART_OF]-(s:Subcategory)
WITH c, g, collect(DISTINCT CASE WHEN s IS NULL THEN null ELSE
    {id: s.id, name: coalesce(s.name, 'Subcategory ' + toString(s.id))} END) AS rawSubcategories
WITH c, collect(DISTINCT CASE WHEN g IS NULL THEN null ELSE {
    id: g.id, name: coalesce(g.name, 'Group ' + toString(g.id)),
    subcategories: [x IN rawSubcategories WHERE x IS NOT NULL]
} END) AS rawGroups
RETURN c.id AS id, coalesce(c.name, 'Category ' + toString(c.id)) AS name,
       [x IN rawGroups WHERE x IS NOT NULL] AS productGroups
ORDER BY c.id
"""


class CategoryRepository:
    """Reads the category hierarchy (category, product group, subcategory)."""

    @staticmethod
    async def get_hierarchy(session: AsyncSession) -> list[CategoryOption]:
        """Reads every category with its product groups and subcategories.

        Levels without a maintained name fall back to a placeholder built from their id,
        so the selection list stays usable while the names are still being filled in.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[CategoryOption]: Every category, ordered by id.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            records = await read_many(session, _CATEGORY_HIERARCHY_QUERY)
            return [CategoryOption.model_validate(dict(record)) for record in records]
        except Neo4jError as e:
            raise DatabaseError(
                f"Database error while fetching the category hierarchy: {e}"
            ) from e
