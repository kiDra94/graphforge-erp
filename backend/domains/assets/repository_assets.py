"""Cypher queries of the assets domain: AssetInstance, release and digital twin."""

from neo4j import AsyncSession
from neo4j.exceptions import ConstraintError, Neo4jError
from pydantic import ValidationError

from core.exceptions import (
    BusinessLogicError,
    DatabaseError,
    DuplicateKeyError,
    NotFoundError,
)
from core.neo4j_query import read_many, read_single, write_single
from domains.inventory.repository_inventory import movement_params, post_movement

from .schemas_assets import (
    Asset,
    AssetCreate,
    AssetCreated,
    AssetDraft,
    AssetDraftConfirmation,
    AssetListItem,
    AssetRelease,
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

# Prefix of every serial number the server assigns. Kept as a constant because the
# creating query forms it and no reader may spell it out a second time.
_SERIAL_NUMBER_PREFIX = "SN-"

# Map projection for every `AssetInstance` node read. `.*` takes over all properties of the
# node, the explicit key adds the derived field.
#
# `status` is deliberately NO stored property. It follows from the course an asset takes
# and is derived here from the fields that document that course anyway.
#
# The order of the CASE branches is required by the business rules, not cosmetic: a
# shipped asset is always released as well. Both conditions then apply, and without this
# ranking the result would depend on the order of evaluation.
#
# coalesce() is needed because `bomReleased` can be missing on nodes that came about
# through MERGE. Without the default the CASE branch yields null instead of false and the
# asset would get no status at all.
_STATUS_EXPRESSION = """CASE
                  WHEN {var}.shippedOn IS NOT NULL THEN 'shipped'
                  WHEN coalesce({var}.bomReleased, false) THEN 'released'
                  ELSE 'planned'
                END"""

_ASSET_PROJECTION = (
    """{var}{
        .*,
        status: """
    + _STATUS_EXPRESSION
    + """
    }"""
)

# Counterpart of the projection for the WHERE clause. Filtering deliberately runs against
# the source fields and not against the projected `status`: a projected key does not yet
# exist in the same query stage, and the comparison on `shippedOn` additionally uses the
# index on that property.
#
# Every fragment actively excludes the higher-ranked cases. That makes the three
# conditions disjoint while together covering every node — exactly like the CASE cascade
# above. Drop that exclusion and ?status=released also returns the assets already shipped.
#
# The parentheses are mandatory: the fragments are appended to further filters with AND,
# and an unparenthesised "A AND NOT B" would tangle with a neighbouring OR.
_STATUS_CONDITIONS: dict[str, str] = {
    "shipped": "({var}.shippedOn IS NOT NULL)",
    "released": "({var}.shippedOn IS NULL AND coalesce({var}.bomReleased, false))",
    "planned": "({var}.shippedOn IS NULL AND NOT coalesce({var}.bomReleased, false))",
}


def status_expression(variable: str = "a") -> str:
    """Returns the CASE cascade that forms the asset status.

    The same cascade sits inside `asset_projection()`. It is additionally available on its
    own here, so a writing query can return the new status without projecting the whole
    map — and without stating the rule a second time. Rebuilding it in Python would be the
    more dangerous variant: it would drift apart as soon as the derivation changes.

    Args:
        variable (str): The name of the node variable in the query.

    Returns:
        str: The usable CASE expression.
    """
    return _STATUS_EXPRESSION.replace("{var}", variable)


def asset_projection(variable: str = "a") -> str:
    """Builds the asset map projection for a concrete Cypher variable.

    Args:
        variable (str): The name of the node variable in the query (for instance "a").

    Returns:
        str: The usable projection fragment including the calculated `status`.
    """
    return _ASSET_PROJECTION.replace("{var}", variable)


def status_condition(status: AssetStatus, variable: str = "a") -> str:
    """Translates a status value into the matching Cypher condition.

    Only a fragment hard-coded in the source ever reaches the query string. The value from
    the client selects the fragment, it is not written into the query.

    Args:
        status (AssetStatus): The desired state.
        variable (str): The name of the node variable in the query.

    Returns:
        str: The parenthesised condition fragment, appendable with AND.

    Raises:
        ValueError: On a status outside `AssetStatus`. That is a programming error — the
            system boundary already rejects unknown values with a 422 —, and a silent
            fallback would turn it into an inconspicuously wrong result list.
    """
    if status not in _STATUS_CONDITIONS:
        raise ValueError(
            f"Unknown asset status '{status}'. Allowed: {sorted(_STATUS_CONDITIONS)}"
        )
    return _STATUS_CONDITIONS[status].replace("{var}", variable)


def asset_list_projection(
    variable: str = "a",
    product: str = "p",
    customer: str = "c",
    document: str = "d",
) -> str:
    """Builds the map projection for one entry of the asset list.

    Delivers the field set of `AssetListItem`: the flat properties including the
    calculated `status` from `asset_projection()`, plus the three numbers behind the edges
    `BASED_ON`, `SOLD_TO` and `BASED_ON_DOCUMENT`.

    The edge targets have to be bound in the query already. `BASED_ON_DOCUMENT` does not
    sit on every asset — legacy stock does not carry it. The caller therefore has to use
    `OPTIONAL MATCH`, otherwise assets without a document silently drop out of the result
    without Cypher reporting anything.

    Args:
        variable (str): Name of the `AssetInstance` variable in the query.
        product (str): Name of the `Product` variable bound through `BASED_ON`.
        customer (str): Name of the `Customer` variable bound through `SOLD_TO`.
        document (str): Name of the `Document` variable bound through `BASED_ON_DOCUMENT`.

    Returns:
        str: The usable projection fragment.
    """
    core = asset_projection(variable).rstrip().removesuffix("}").rstrip()
    return (
        f"{core},\n"
        f"        productNumber: {product}.number,\n"
        f"        customerId: {customer}.id,\n"
        f"        documentNumber: {document}.number\n"
        f"    }}"
    )


def asset_detail_projection(
    variable: str = "a",
    product: str = "p",
    customer: str = "c",
    document: str = "d",
    employee: str = "e",
) -> str:
    """Builds the map projection for the detail answer of an asset.

    Delivers the field set of `Asset`: everything from `asset_projection()`, plus the edge
    fields, the customer as a nested object (`{id, name}`), the release block and the
    installed components.

    The `release` block is assembled here rather than passed through flat: it bundles the
    properties `bomReleased`, `releasedOn` and `releaseNote` with the name behind
    `RELEASED_BY`. `coalesce` on `bomReleased` is needed because the property can be
    missing on nodes that came about through `MERGE`.

    Everything comes from **one** query. A round trip per component would be exactly the
    N+1 problem this project deliberately works without an OGM to avoid.

    **The components come through a pattern comprehension, not through `OPTIONAL MATCH` +
    `collect()`.** Both deliver the same result, but the comprehension needs no `WITH`: a
    `collect()` beside several `OPTIONAL MATCH` clauses has to group by all remaining
    variables, and whoever forgets one there loses it silently from the result. The
    comprehension also yields `[]` by itself instead of one row per component.

    On `null` both forms behave correctly: a map projection on an unbound node yields
    `null`, a property access likewise. `customer` and `release.employee` are therefore
    safe without a case distinction.

    Precondition on the query: `product`, `customer`, `document` and `employee` have to be
    bound — the last three through `OPTIONAL MATCH`. An asset without a releaser is
    normal, one without a document as well.

    Args:
        variable (str): Name of the `AssetInstance` variable in the query.
        product (str): Name of the `Product` variable bound through `BASED_ON`.
        customer (str): Name of the `Customer` variable bound through `SOLD_TO`.
        document (str): Name of the `Document` variable bound through `BASED_ON_DOCUMENT`.
        employee (str): Name of the `Employee` variable bound through `RELEASED_BY`.

    Returns:
        str: The usable projection fragment.
    """
    core = asset_projection(variable).rstrip().removesuffix("}").rstrip()
    return (
        f"{core},\n"
        f"        productNumber: {product}.number,\n"
        f"        documentNumber: {document}.number,\n"
        f"        customer: {customer}{{.id, .name}},\n"
        f"        release: {{\n"
        f"            released: coalesce({variable}.bomReleased, false),\n"
        f"            releasedOn: {variable}.releasedOn,\n"
        f"            employee: {employee}.name,\n"
        f"            note: {variable}.releaseNote\n"
        f"        }},\n"
        f"        components: [\n"
        f"            ({variable})-[componentEdge:HAS_COMPONENT]->(componentInstance:ComponentInstance)\n"
        f"                        -[:IS_TYPE]->(componentProduct:Product)\n"
        f"            WHERE componentInstance.status = 'active' |\n"
        f"            {{\n"
        f"                productNumber: componentProduct.number,\n"
        f"                label:         componentProduct.label,\n"
        f"                quantity:      componentEdge.quantity,\n"
        f"                installedOn:   componentInstance.installedOn\n"
        f"            }}\n"
        f"        ]\n"
        f"    }}"
    )


def excess_over_document(
    components: list[ComponentLineCreate], document_lines: list[dict | None] | None
) -> list[tuple[str, float]]:
    """Determines per product by how much the confirmed bill of materials exceeds the
    document.

    On confirming an asset draft, engineering enters the bill of materials actually
    built. Whatever of it already stood on the document was reserved with that document's
    order confirmation; only the excess still needs a booking.

    Products occurring more than once are summed on BOTH sides before the comparison is
    made. On the document side, because the same product may appear several times on one
    document; on the bill-of-materials side, because otherwise every line competes singly
    against the full document quantity and thereby counts as covered — two lines of 2
    pieces each against a document quantity of 2 would have yielded no excess at all
    instead of the actual 2. `None` entries out of the `collect` of a query without hits
    are skipped.

    Args:
        components (list[ComponentLineCreate]): The confirmed bill of materials.
        document_lines (list[dict | None] | None): The non-cancelled lines of the
            document, each as `{productNumber, quantity}`.

    Returns:
        list[tuple[str, float]]: Product number and quantity to be reserved, only for
            products with a real excess. Equality and shortfall drop out — a shortfall is
            NOT released, the cancellation of the document is responsible for that.
    """
    from_document: dict[str, float] = {}
    for line in document_lines or []:
        if line is None:
            continue
        from_document[line["productNumber"]] = (
            from_document.get(line["productNumber"], 0.0) + float(line["quantity"])
        )

    confirmed: dict[str, float] = {}
    for component in components:
        confirmed[component.productNumber] = (
            confirmed.get(component.productNumber, 0.0) + float(component.quantity)
        )

    excess = []
    for product_number, quantity in confirmed.items():
        difference = quantity - from_document.get(product_number, 0.0)
        if difference > 0:
            excess.append((product_number, difference))
    return excess


class AssetRepository:
    """Repository for the asset master data and its digital twin.

    Encapsulates the Cypher queries around `AssetInstance`: list, detail, creation,
    bill-of-materials release and the maintenance of the process data `shippedOn` and
    `installedOn`.

    Three rules hold for every writing method here:

    - **Existence checks run in the same transaction as the write.** A check in a query of
      its own beforehand would be a TOCTOU window. That is a deliberate exception from the
      otherwise strict separation between repository and service.
    - **Uniqueness comes from the constraint, not from a check beforehand.** `CREATE`
      instead of `MERGE`, catch the `ConstraintError` and translate it into a
      `DuplicateKeyError`.
    - **No `session.run()`.** Exclusively the helpers from `core/neo4j_query.py`; where
      several steps have to be atomic, a transaction function of its own over
      `session.execute_write`.
    """

    @staticmethod
    def _to_asset(record: dict) -> Asset:
        """Converts the result of the detail projection into the read model.

        Expects the map from `asset_detail_projection()`, not the raw node properties:
        `status`, `customer`, `release` and `components` are already assembled in Cypher
        there. The nested blocks arrive as dictionaries and are validated by Pydantic into
        `AssetCustomer`, `AssetRelease` and `InstalledComponent`.

        Args:
            record (dict): The projected map of an `AssetInstance` node.

        Returns:
            Asset: The validated read model.

        Raises:
            DatabaseError: When the node holds data the read model cannot map. That is a
                server-side data problem, no client error, and is therefore treated as a
                500.
        """
        try:
            return Asset.model_validate(record)
        except ValidationError as e:
            raise DatabaseError(
                f"Asset '{record.get('serialNumber')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    def _to_list_item(record: dict) -> AssetListItem:
        """Converts the result of the list projection into a list entry.

        Args:
            record (dict): The projected map from `asset_list_projection()`.

        Returns:
            AssetListItem: The validated list entry.

        Raises:
            DatabaseError: As with `_to_asset` — a node the read model cannot map is a
                data problem of the server.
        """
        try:
            return AssetListItem.model_validate(record)
        except ValidationError as e:
            raise DatabaseError(
                f"Asset '{record.get('serialNumber')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    async def get_assets(
        session: AsyncSession,
        customerId: str | None = None,
        status: AssetStatus | None = None,
        search: str | None = None,
    ) -> list[AssetListItem]:
        """Fetches assets from the graph, optionally filtered.

        Assembles the WHERE clause dynamically from the filters that are set. Only
        fragments hard-coded in the source ever reach the query string — every value from
        the client is bound as a Cypher parameter. For `status` the fragment comes from
        `status_condition()`.

        The filters are additive; do they together match nothing, the empty list is the
        right result and no error.

        `search` runs against `serialNumber`, `internalNumber` and `projectNumber`.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            customerId (str | None): Narrows down to one customer through the edge
                `SOLD_TO`.
            status (AssetStatus | None): Filters on the calculated state. None delivers
                all three.
            search (str | None): Free text, case-insensitive against serial number,
                internal number and project number.

        Returns:
            list[AssetListItem]: The assets found, or an empty list.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        projection = asset_list_projection()
        conditions: list[str] = []
        params: dict = {}
        if status:
            conditions.append(status_condition(status))
        if customerId:
            conditions.append("c.id = $customerId")
            params["customerId"] = customerId
        if search:
            conditions.append(
                "(toLower(a.serialNumber) CONTAINS toLower($search)"
                " OR toLower(coalesce(a.internalNumber, '')) CONTAINS toLower($search)"
                " OR toLower(coalesce(a.projectNumber, '')) CONTAINS toLower($search))"
            )
            params["search"] = search
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        try:
            # All three edges through OPTIONAL MATCH: an ordinary MATCH would silently
            # throw an asset missing one of them out of the list — without an error and
            # without anyone noticing. A record with `null` in a field is the more honest
            # answer.
            #
            # The WITH in front is MANDATORY and not cosmetic: a WHERE directly after an
            # OPTIONAL MATCH belongs to its pattern. It then only decides whether the edge
            # is bound and removes not a single row — every filter would run without
            # effect and Cypher would report nothing. Only behind the WITH does a new query
            # stage begin in which WHERE discards rows again.
            #
            # Sorting runs on the projected alias, not on `a.serialNumber`: after the
            # RETURN it is available, and without an ORDER BY the order of the list would
            # not be defined.
            query = f"""
            MATCH (a:AssetInstance)
            OPTIONAL MATCH (a)-[:BASED_ON]->(p:Product)
            OPTIONAL MATCH (a)-[:SOLD_TO]->(c:Customer)
            OPTIONAL MATCH (a)-[:BASED_ON_DOCUMENT]->(d:Document)
            WITH a, p, c, d
            {where}
            RETURN {projection} AS asset
            ORDER BY asset.serialNumber
            """

            assets = await read_many(session, query, **params)

            return [AssetRepository._to_list_item(asset["asset"]) for asset in assets]
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the asset list: {e}") from e

    @staticmethod
    async def get_asset(serial_number: str, session: AsyncSession) -> Asset | None:
        """Fetches a single asset with all its detail data.

        Delivers customer, release block and the as-built state in **one** query. The
        components come over `HAS_COMPONENT` and `IS_TYPE`, filtered on
        `componentInstance.status = 'active'`.

        Args:
            serial_number (str): The business key of the asset.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Asset | None: The validated read model, or None when the serial number does
                not exist. Translating that into a 404 is the service's business.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        try:
            # The projection accesses p, c, d and e — they have to be bound. Everything
            # but the product through OPTIONAL MATCH: an asset without a document or
            # without a releaser is the normal case and must not lead to the detail query
            # delivering nothing at all, out of which the service would make a 404.
            query = f"""
            MATCH (a:AssetInstance {{serialNumber: $serialNumber}})
            OPTIONAL MATCH (a)-[:BASED_ON]->(p:Product)
            OPTIONAL MATCH (a)-[:SOLD_TO]->(c:Customer)
            OPTIONAL MATCH (a)-[:BASED_ON_DOCUMENT]->(d:Document)
            OPTIONAL MATCH (a)-[:RELEASED_BY]->(e:Employee)
            RETURN {asset_detail_projection()} AS asset
            """
            asset = await read_single(session, query, serialNumber=serial_number)

            if asset:
                return AssetRepository._to_asset(asset["asset"])
            return None
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the asset: {e}") from e

    @staticmethod
    async def _copy_bom(tx, serial_number: str) -> int:
        """Copies the standard bill of materials of the asset product flat into
        `HAS_COMPONENT`.

        Runs inside an already open write transaction, directly after creating the
        `AssetInstance`.

        **A copy, not a reference:** the bill of materials comes about as this one asset's
        own `HAS_COMPONENT` edges. Maintaining the standard BOM later changes nothing
        retroactively on a machine already built.

        **Leaves only:** `[:CONTAINS*0..]` with a filter on `NOT (leaf)-[:CONTAINS]->()`
        picks from every path only the maximal one (the one ending at a real leaf) — an
        intermediate node with children of its own is thereby skipped automatically and
        only caught over the longer path. `quantity` is the product of the edge quantities
        along the path, summed over all paths to the same leaf (the same leaf product can
        occur in several sub-assemblies).

        **All components, not only wear parts:** release and delivery note have to act on
        the *complete* bill of materials, the service forecast filters on `isWearPart`
        itself on top of that (`AssetServiceRepository.get_due_services`).

        `MERGE` on `ComponentInstance.id = serialNumber + '_' + productNumber` stays
        idempotent: a second call creates no duplicates and overwrites no `quantity`
        already changed by engineering (`ON CREATE SET` instead of `SET`).

        Args:
            tx: The running Neo4j write transaction.
            serial_number (str): The business key of the asset whose `BASED_ON` already
                points at the asset product.

        Returns:
            int: Number of component instances newly created by this call.
        """
        query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})-[:BASED_ON]->(root:Product)
        MATCH path = (root)-[:CONTAINS*0..]->(leaf:Product)
        WHERE NOT (leaf)-[:CONTAINS]->()
        WITH a, leaf, reduce(q = 1.0, rel IN relationships(path) | q * rel.quantity) AS pathQuantity
        WITH a, leaf, sum(pathQuantity) AS required
        MERGE (ci:ComponentInstance {id: a.serialNumber + '_' + leaf.number})
        ON CREATE SET ci.status = 'active', ci.installedOn = a.installedOn, ci.fresh = true
        MERGE (a)-[rel:HAS_COMPONENT]->(ci)
        ON CREATE SET rel.quantity = required
        MERGE (ci)-[:IS_TYPE]->(leaf)
        WITH ci, coalesce(ci.fresh, false) AS isFresh
        REMOVE ci.fresh
        RETURN count(CASE WHEN isFresh THEN 1 END) AS created
        """
        record = await (await tx.run(query, {"serialNumber": serial_number})).single()
        return record["created"] if record else 0

    @staticmethod
    async def _write_bom(
        tx, serial_number: str, components: list[ComponentLineCreate]
    ) -> int:
        """Writes a bill of materials adjusted by engineering before the confirmation
        directly into `HAS_COMPONENT`, instead of taking over the product's standard BOM
        the way `_copy_bom` does.

        Runs like `_copy_bom` inside an already open write transaction, directly after
        creating the `AssetInstance` (`_create_asset_in_tx`, called from `confirm_draft`).
        The preview engineering adjusts the list from (`GET /api/products/{number}/bom`)
        already delivers product numbers — unlike `_copy_bom` there is no tree structure
        left to resolve here, only a flat list to write.

        Args:
            tx: The running Neo4j write transaction.
            serial_number (str): The business key of the asset.
            components (list[ComponentLineCreate]): The list confirmed by engineering.

        Returns:
            int: Number of component instances newly created in the process.

        Raises:
            NotFoundError: When a product number given does not exist.
            BusinessLogicError: When a component with unit 'pcs' carries a fractional
                quantity.
        """
        component_params = [
            {"productNumber": c.productNumber, "quantity": float(c.quantity)}
            for c in components
        ]

        check_query = """
        UNWIND $components AS component
        OPTIONAL MATCH (p:Product {number: component.productNumber})
        RETURN collect(DISTINCT CASE WHEN p IS NULL THEN component.productNumber END) AS missing,
               collect(CASE WHEN p IS NOT NULL THEN {productNumber: p.number, unit: p.unit} END)
                   AS productRows
        """
        check = await (await tx.run(check_query, {"components": component_params})).single()
        missing = sorted(n for n in check["missing"] if n is not None) if check else []
        if missing:
            raise NotFoundError(f"Products not found: {', '.join(missing)}.")

        # The same rule as on document lines (`_fractional_quantity_violations` in
        # sales/repository_sales/rules.py) — a bill of materials edited through a draft would
        # otherwise be the only way to create an asset with a fractional piece count.
        unit_per_product = {
            row["productNumber"]: row["unit"]
            for row in check["productRows"] if row is not None
        }
        piece_violations = [
            f"{c.productNumber} (quantity {c.quantity})"
            for c in components
            if unit_per_product.get(c.productNumber) == "pcs" and c.quantity % 1 != 0
        ]
        if piece_violations:
            raise BusinessLogicError(
                "Quantity has to be a whole number for unit 'pcs': "
                + ", ".join(piece_violations) + "."
            )

        query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        UNWIND $components AS component
        MATCH (p:Product {number: component.productNumber})
        MERGE (ci:ComponentInstance {id: a.serialNumber + '_' + p.number})
        ON CREATE SET ci.status = 'active', ci.installedOn = a.installedOn, ci.fresh = true
        MERGE (a)-[rel:HAS_COMPONENT]->(ci)
        SET rel.quantity = component.quantity
        MERGE (ci)-[:IS_TYPE]->(p)
        WITH ci, coalesce(ci.fresh, false) AS isFresh
        REMOVE ci.fresh
        RETURN count(CASE WHEN isFresh THEN 1 END) AS created
        """
        record = await (await tx.run(
            query, {"serialNumber": serial_number, "components": component_params}
        )).single()
        return record["created"] if record else 0

    @staticmethod
    async def _create_asset_in_tx(
        tx, asset: AssetCreate, component_override: list[ComponentLineCreate] | None = None
    ) -> dict:
        """Checks the document, forms the number, creates the asset and copies its bill of
        materials — the core of `create_asset`, extracted as a method of its own.

        Runs inside an already open write transaction. Two callers share this one
        implementation, "the same repository path": `create_asset` (the manual route, which
        opens the transaction itself) and `confirm_draft` (confirmation of an asset draft
        by engineering).

        See `create_asset` for the full derivation of the serial number and the
        preconditions checked — only moved here, not changed.

        Args:
            tx: The running Neo4j write transaction.
            asset (AssetCreate): The validated input data.
            component_override (list[ComponentLineCreate] | None): Bill of materials
                adjusted by engineering before the confirmation, or optionally given along
                on a manual `create_asset`. `None` copies the standard BOM of
                `asset.productNumber` (`_copy_bom`), provided it is set; is `productNumber`
                `None` as well, the asset stays without components. Is the list set,
                `_write_bom` writes exactly these lines instead.

        Returns:
            dict: `serialNumber`, `internalNumber` and `createdComponents`.

        Raises:
            NotFoundError: When the document does not exist, or when `asset.productNumber`
                is set but matches no product.
            BusinessLogicError: When the document is no order confirmation, carries no
                customer, hangs off no order or has no date.
        """
        context_query = """
        MATCH (document:Document {number: $documentNumber})
        OPTIONAL MATCH (document)-[:BELONGS_TO_CUSTOMER]->(customer:Customer)
        OPTIONAL MATCH (document)-[:BELONGS_TO_ORDER]->(o:Order)
        OPTIONAL MATCH (product:Product {number: $productNumber})
        // Count over the order, not over the document: an order can carry several order
        // confirmations, but the running number applies to the whole case.
        OPTIONAL MATCH (o)<-[:BELONGS_TO_ORDER]-(:Document)
                       <-[:BASED_ON_DOCUMENT]-(existing:AssetInstance)
        RETURN document.type              AS documentType,
               document.date              AS documentDate,
               customer.id                AS customerId,
               o.projectNumber            AS projectNumber,
               product.number             AS productNumber,
               count(DISTINCT existing)   AS existingCount
        """

        # The product is optional: most assets come about without a catalogue product (see
        # asset draft), BASED_ON then does not come about at all — no placeholder, no edge
        # into the void.
        create_query = """
        MATCH (document:Document {number: $documentNumber})
        OPTIONAL MATCH (product:Product {number: $productNumber})
        MATCH (customer:Customer {id: $customerId})
        CREATE (a:AssetInstance {
            serialNumber:   $serialNumber,
            internalNumber: $internalNumber,
            projectNumber:  $projectNumber,
            orderedOn:      $orderedOn,
            bomReleased:    false,
            createdAt:      datetime()
        })
        FOREACH (p IN CASE WHEN product IS NOT NULL THEN [product] ELSE [] END |
            CREATE (a)-[:BASED_ON]->(p)
        )
        CREATE (a)-[:BASED_ON_DOCUMENT]->(document)
        CREATE (a)-[:SOLD_TO]->(customer)
        RETURN a.serialNumber AS serialNumber, a.internalNumber AS internalNumber
        """

        context = await (await tx.run(context_query, {
            "documentNumber": asset.documentNumber,
            "productNumber": asset.productNumber,
        })).single()

        if context is None:
            raise NotFoundError(f"Document '{asset.documentNumber}' does not exist.")
        if asset.productNumber is not None and context["productNumber"] is None:
            raise NotFoundError(f"Product '{asset.productNumber}' does not exist.")
        if context["documentType"] != "OrderConfirmation":
            raise BusinessLogicError(
                f"Document '{asset.documentNumber}' is of type '{context['documentType']}'. "
                "An asset can only come out of an order confirmation."
            )
        if context["customerId"] is None:
            raise BusinessLogicError(
                f"Document '{asset.documentNumber}' carries no customer. The asset needs "
                "the edge SOLD_TO and cannot be created this way."
            )
        if context["projectNumber"] is None:
            raise BusinessLogicError(
                f"Document '{asset.documentNumber}' hangs off no order. Without a project "
                "number no serial number can be formed."
            )
        if context["documentDate"] is None:
            raise BusinessLogicError(
                f"Document '{asset.documentNumber}' has no date. Without one no order date "
                "can be maintained on the asset."
            )

        serial_number = f"{_SERIAL_NUMBER_PREFIX}{context['projectNumber']}"
        if context["existingCount"]:
            serial_number += f"-{context['existingCount'] + 1}"

        record = await (await tx.run(create_query, {
            "documentNumber": asset.documentNumber,
            "productNumber": asset.productNumber,
            "customerId": context["customerId"],
            "serialNumber": serial_number,
            "internalNumber": asset.internalNumber,
            "projectNumber": context["projectNumber"],
            "orderedOn": context["documentDate"],
        })).single()
        if record is None:
            raise DatabaseError("Asset was not created, Neo4j returned an empty result.")

        if component_override is not None:
            created_components = await AssetRepository._write_bom(
                tx, record["serialNumber"], component_override
            )
        else:
            created_components = await AssetRepository._copy_bom(tx, record["serialNumber"])
        return {
            "serialNumber": record["serialNumber"],
            "internalNumber": record["internalNumber"],
            "createdComponents": created_components,
        }

    @staticmethod
    async def create_asset(asset: AssetCreate, session: AsyncSession) -> AssetCreated:
        """Creates an asset on the basis of an order confirmation.

        Creates the `AssetInstance` node and draws the three edges `BASED_ON` (product),
        `BASED_ON_DOCUMENT` (document) and `SOLD_TO` (customer) in the same transaction.

        The customer is not in the request, it is resolved through
        `Document -[:BELONGS_TO_CUSTOMER]-> Customer`. Were it sent separately, asset and
        document could point at different customers without anyone noticing.

        The document has to be of type `OrderConfirmation`. Checked against the property
        `document.type`, not against a label: the error message is meant to name the type
        actually found, and a label cannot be parameterised in Cypher.

        **The server assigns the serial number**, not the client — it is therefore not in
        `AssetCreate`. It is formed as `SN-{projectNumber}[-n]`:

        - `projectNumber` comes from `Document -[:BELONGS_TO_ORDER]-> Order`, the bracket
          over the whole case. Without that edge `400` instead of an invented number.
        - Does the same order already carry assets, a running number is appended (`-2`,
          `-3`, …). The first one stays without a suffix, so a key already assigned never
          has to be renamed afterwards.

        Counting runs over the order, not over the document: several order confirmations
        can belong to one order, and the running number is meant to be unique across the
        whole case.

        **Two simultaneous requests to the same order** can calculate the same number. The
        second `CREATE` then fails on the constraint and becomes a `DuplicateKeyError`.
        That is deliberately not caught: a retry would be more machinery than value here,
        and the alternative — drawing the number outside the transaction — would simply be
        wrong.

        **Copies the bill of materials right away** into `HAS_COMPONENT` (`_copy_bom`).
        The number of components created in the process is not in `AssetCreated`, because
        that answer contract stays narrow (only what the server contributed and the client
        would not otherwise know); readable through `GET /api/assets/{serialNumber}`.

        Args:
            asset (AssetCreate): The validated input data.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            AssetCreated: The assigned serial number, the internal number and the start
                state `planned`.

        Raises:
            DuplicateKeyError: When the serial number formed or `internalNumber` is already
                taken. Recognised through the `ConstraintError`, not through a check
                beforehand.
            NotFoundError: When product or document do not exist.
            BusinessLogicError: When the document is no order confirmation, carries no
                customer, hangs off no order or has no date.
            DatabaseError: On unexpected errors during the write.
        """
        try:
            record = await session.execute_write(AssetRepository._create_asset_in_tx, asset)
            return AssetCreated(
                serialNumber=record["serialNumber"],
                internalNumber=record["internalNumber"],
                status="planned",
            )
        except ConstraintError as e:
            raise DuplicateKeyError(
                f"No asset could be created for document '{asset.documentNumber}': serial "
                "number or internal number is already taken."
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while creating the asset: {e}") from e

    @staticmethod
    async def set_release(
        serial_number: str,
        release: AssetReleaseRequest,
        employee_id: str,
        session: AsyncSession,
    ) -> AssetReleaseResponse | None:
        """Sets the bill-of-materials release or takes it back.

        On `released: true` the properties `bomReleased`, `releasedOn` and `releaseNote`
        are set and the edge `RELEASED_BY` is drawn. On `released: false` the same fields
        are reset and the edge is removed.

        The status change happens without logic of its own — it follows from the
        derivation rule as soon as `bomReleased` changes.

        **Reserves against the components** — no longer without effect. A **real** release
        (transition `false -> true`) books one `Reservation` per `HAS_COMPONENT` edge
        against the main warehouse. **Does the stock not suffice for even one component,
        neither the release nor any booking comes about** — checked beforehand in the same
        transaction, against `quantity minus reserved`. Sending `released: true` again on
        an already released asset does **not** book a second time; the same holds
        symmetrically for a repeated withdrawal.

        Deliberately checked **locally** and not through the shared `_effect` of
        `post_movement` alone: its `Reservation` branch does cap at `quantity minus
        reserved` as well, but it does so with **partial reservation instead of
        rejection** — right for an order confirmation, which is meant to come about
        despite scarce stock and leave the rest standing as an open quantity. For the
        bill-of-materials release that would be wrong: a half-reserved asset is no sensible
        intermediate state, so this method checks **beforehand, for all components
        together, all or nothing** and only calls `post_movement` afterwards — at that
        point the availability is guaranteed sufficient, so the shared capping branch never
        fires here.

        A **real withdrawal** (transition `true -> false`) books the same quantities back
        as a `Correction` against `reserved` — the same mechanism as on a document
        cancellation, here inline because only the one reservation case occurs.

        **After shipping the withdrawal is locked.** The asset would otherwise stay at
        `shipped` — the shipping date wins the cascade — while the release fields are
        empty. In business terms a machine already delivered cannot be declared unchecked
        retroactively. The check belongs in the same transaction as the write.

        Args:
            serial_number (str): The business key of the asset.
            release (AssetReleaseRequest): Direction and note. That a note is inadmissible
                on a withdrawal is already checked by the schema.
            employee_id (str): Id of the releasing employee, from the auth token.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            AssetReleaseResponse | None: The new state, or None when the serial number does
                not exist.

        Raises:
            NotFoundError: When `employee_id` belongs to no employee.
            BusinessLogicError: On a withdrawal although `shippedOn` is set, or when the
                stock does not suffice for a component (no main warehouse included).
            DatabaseError: On unexpected errors during the write.
        """
        # Read state, employee, main warehouse and the current bill of materials in one
        # go. All of it has to stand in the same transaction as the write, otherwise a
        # shipping date could come about between check and write, or the stock could
        # change.
        context_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        OPTIONAL MATCH (e:Employee {id: $employeeId})
        OPTIONAL MATCH (w:Location {type: 'Warehouse'})
        OPTIONAL MATCH (a)-[rel:HAS_COMPONENT]->(ci:ComponentInstance)-[:IS_TYPE]->(p:Product)
        WHERE ci.status = 'active'
        RETURN a.shippedOn IS NOT NULL              AS isShipped,
               coalesce(a.bomReleased, false)       AS wasReleased,
               e.id                                 AS employeeId,
               collect(w.id)[0]                     AS mainWarehouseId,
               collect(CASE WHEN p IS NULL THEN null
                            ELSE {productNumber: p.number, quantity: rel.quantity} END) AS components
        """

        # Stock and reservation per component, against the main warehouse. A component
        # never booked has no stock level node — then 0/0 applies, and the check still
        # works correctly (no stock covers no demand).
        stock_query = """
        UNWIND $components AS component
        OPTIONAL MATCH (s:StockLevel {id: component.productNumber + '_' + $locationId})
        RETURN component.productNumber AS productNumber, component.quantity AS required,
               coalesce(s.quantity, 0.0) AS quantity, coalesce(s.reserved, 0.0) AS reserved
        """

        # The edge is always removed first: on a renewed release by a different employee no
        # second one may stand beside it, on a withdrawal it has to go.
        release_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        OPTIONAL MATCH (a)-[old:RELEASED_BY]->(:Employee)
        DELETE old
        WITH a
        SET a.bomReleased = true,
            a.releasedOn  = date(),
            a.releaseNote = $note,
            a.updatedAt   = datetime()
        WITH a
        MATCH (e:Employee {id: $employeeId})
        CREATE (a)-[:RELEASED_BY]->(e)
        RETURN a.serialNumber AS serialNumber,
               a.releasedOn AS releasedOn,
               a.releaseNote AS note,
               e.name AS employee,
               """ + status_expression() + """ AS status
        """

        # `= null` removes the property in Neo4j. An empty value would stay in place and
        # the asset would still look released in raw queries.
        withdrawal_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        OPTIONAL MATCH (a)-[old:RELEASED_BY]->(:Employee)
        DELETE old
        WITH a
        SET a.bomReleased = false,
            a.releasedOn  = null,
            a.releaseNote = null,
            a.updatedAt   = datetime()
        RETURN a.serialNumber AS serialNumber,
               null AS releasedOn, null AS note, null AS employee,
               """ + status_expression() + """ AS status
        """

        async def _set_release(tx):
            """Checks asset, employee and shipping state, books against the components
            where needed and writes — atomically."""
            context = await (await tx.run(context_query, {
                "serialNumber": serial_number,
                "employeeId": employee_id,
            })).single()

            if context is None:
                return None
            if context["employeeId"] is None:
                raise NotFoundError(f"Employee '{employee_id}' does not exist.")
            if not release.released and context["isShipped"]:
                raise BusinessLogicError(
                    f"The release of asset '{serial_number}' cannot be withdrawn: it has "
                    "already shipped."
                )

            # Only a real change triggers a booking — sending the same direction again
            # (already released -> release again, or already withdrawn -> withdraw again)
            # must not reserve, respectively reverse, twice.
            becomes_released = release.released and not context["wasReleased"]
            becomes_withdrawn = not release.released and context["wasReleased"]

            components = [c for c in context["components"] if c is not None]

            if becomes_released and components:
                if context["mainWarehouseId"] is None:
                    raise BusinessLogicError(
                        f"Asset '{serial_number}' has components, but no main warehouse is "
                        "on file. Without a location nothing can be reserved."
                    )
                stock_rows = await (await tx.run(stock_query, {
                    "components": components,
                    "locationId": context["mainWarehouseId"],
                })).data()
                insufficient = [
                    row["productNumber"] for row in stock_rows
                    if row["quantity"] - row["reserved"] < row["required"]
                ]
                if insufficient:
                    raise BusinessLogicError(
                        f"The bill of materials of asset '{serial_number}' cannot be "
                        "released: the available stock does not suffice for "
                        f"{', '.join(sorted(insufficient))}. Neither a release nor a "
                        "booking has come about."
                    )

            query = release_query if release.released else withdrawal_query
            record = await (await tx.run(query, {
                "serialNumber": serial_number,
                "employeeId": employee_id,
                "note": release.note,
            })).single()

            if becomes_released:
                for component in components:
                    await post_movement(
                        tx,
                        movement_params(
                            component["productNumber"],
                            context["mainWarehouseId"],
                            "Reservation",
                            component["quantity"],
                            note=(
                                "Reservation from the bill-of-materials release of asset "
                                f"{serial_number}"
                            ),
                        ),
                    )
            elif becomes_withdrawn:
                for component in components:
                    await post_movement(
                        tx,
                        movement_params(
                            component["productNumber"],
                            context["mainWarehouseId"],
                            "Correction",
                            -component["quantity"],
                            note=(
                                "Withdrawal of the bill-of-materials release of asset "
                                f"{serial_number}"
                            ),
                        ),
                        target_reserved=True,
                    )

            return record

        try:
            record = await session.execute_write(_set_release)
            if record is None:
                return None

            return AssetReleaseResponse(
                serialNumber=record["serialNumber"],
                status=record["status"],
                release=AssetRelease(
                    released=release.released,
                    releasedOn=record["releasedOn"],
                    employee=record["employee"],
                    note=record["note"],
                ),
            )
        except Neo4jError as e:
            raise DatabaseError(f"Database error during the release: {e}") from e

    @staticmethod
    async def update_asset(
        serial_number: str,
        update_data: AssetUpdate,
        session: AsyncSession,
    ) -> AssetUpdated | None:
        """Maintains the shipping and installation date of an asset.

        Sets only the fields actually sent in the request (`model_dump(exclude_unset=True)`).

        **Shipping no longer creates the bill of materials.** It comes about with the
        creation of the asset already (`_create_asset_in_tx` -> `_copy_bom`/`_write_bom`).
        The shipping date now only marks the beginning of the service interval.

        **Fallback for legacy assets:** does the asset carry not a single `HAS_COMPONENT`
        edge on shipping — because it was created through an older route without a copy of
        the bill of materials —, the same `_copy_bom` copies it afterwards, with the same
        "all components rather than only wear parts" rule as on creation. For every asset
        already filled (the normal case) that is a no-op: `_copy_bom` itself checks
        nothing, the call is skipped here beforehand, so a second copy run could never
        touch a `quantity` already changed by engineering.

        An `installedOn` sent along is carried over to all component instances of the asset
        already created.

        Args:
            serial_number (str): The business key of the asset.
            update_data (AssetUpdate): The fields to change.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            AssetUpdated | None: The complete asset including the number of components
                newly created by this call, or None when the serial number does not exist.
                The number is 0 in the normal case — the bill of materials exists since the
                asset was created; only in the fallback for a legacy asset without a copy
                is it positive.

        Raises:
            BusinessLogicError: When the request carries not a single field, or when a
                `shippedOn` already set is to be reset to null.
            DatabaseError: On unexpected errors during the write.
        """
        changes = update_data.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessLogicError("No fields to update were given.")

        # Only the fields actually sent are set. A field the client left out must not
        # overwrite the stored value — the SET clause is therefore built from
        # `exclude_unset` and not hard-wired. Only the field names hard-coded here reach
        # the string.
        _ALLOWED_FIELDS = {"shippedOn": "$shippedOn", "installedOn": "$installedOn"}
        set_clause = ", ".join(
            f"a.{field} = {_ALLOWED_FIELDS[field]}" for field in changes
        )

        context_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        RETURN a.shippedOn IS NOT NULL AS isShipped,
               EXISTS { (a)-[:HAS_COMPONENT]->(:ComponentInstance) } AS hasBom
        """

        update_query = f"""
        MATCH (a:AssetInstance {{serialNumber: $serialNumber}})
        SET {set_clause}, a.updatedAt = datetime()
        RETURN a.shippedOn IS NOT NULL AS isShipped
        """

        # An installation date supplied later is passed on to the components already
        # created — at shipping time it is usually not known yet.
        component_date_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})-[:HAS_COMPONENT]->(ci:ComponentInstance)
        SET ci.installedOn = a.installedOn
        """

        async def _update_asset(tx):
            """Updates the fields and copies the fallback where needed — atomically."""
            context = await (await tx.run(
                context_query, {"serialNumber": serial_number}
            )).single()
            if context is None:
                return None

            if (
                "shippedOn" in changes
                and changes["shippedOn"] is None
                and context["isShipped"]
            ):
                raise BusinessLogicError(
                    f"The shipping date of asset '{serial_number}' cannot be taken back: "
                    "the digital twin has already come about."
                )

            params = {"serialNumber": serial_number} | {
                field: changes.get(field) for field in _ALLOWED_FIELDS
            }
            after = await (await tx.run(update_query, params)).single()

            created = 0
            if after is not None and after["isShipped"] and not context["hasBom"]:
                created = await AssetRepository._copy_bom(tx, serial_number)

            if "installedOn" in changes:
                await (await tx.run(
                    component_date_query, {"serialNumber": serial_number}
                )).consume()

            return created

        try:
            created = await session.execute_write(_update_asset)
            if created is None:
                return None

            asset = await AssetRepository.get_asset(serial_number, session)
            if asset is None:
                raise DatabaseError(
                    f"Asset '{serial_number}' was no longer readable after the update."
                )
            return AssetUpdated(**asset.model_dump(), createdComponents=created)
        except Neo4jError as e:
            raise DatabaseError(f"Database error while updating the asset: {e}") from e

    @staticmethod
    async def get_bom(
        serial_number: str, session: AsyncSession
    ) -> list[InstalledComponent] | None:
        """Reads the bill of materials of a single asset.

        The same rows as `Asset.components` from `get_asset`, but without loading the rest
        of the asset — the endpoint `GET /api/assets/{serialNumber}/bom` needs only this
        one list.

        Args:
            serial_number (str): The business key of the asset.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[InstalledComponent] | None: The components, or `None` when the serial
                number does not exist. An asset without components delivers `[]`.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        # `a` through OPTIONAL MATCH, not through MATCH: over zero rows `collect()` already
        # returns one row holding an empty list (standard Cypher aggregation), so an unknown
        # serial number would otherwise be indistinguishable from an asset without
        # components.
        query = """
        OPTIONAL MATCH (a:AssetInstance {serialNumber: $serialNumber})
        OPTIONAL MATCH (a)-[rel:HAS_COMPONENT]->(ci:ComponentInstance)-[:IS_TYPE]->(p:Product)
        WHERE ci.status = 'active'
        RETURN a IS NOT NULL AS assetExists,
               collect(CASE WHEN p IS NULL THEN null ELSE
                   {productNumber: p.number, label: p.label,
                    quantity: rel.quantity, installedOn: ci.installedOn}
               END) AS components
        """
        try:
            record = await read_single(session, query, serialNumber=serial_number)
            if record is None or not record["assetExists"]:
                return None
            return [
                InstalledComponent.model_validate(c)
                for c in record["components"] if c is not None
            ]
        except Neo4jError as e:
            raise DatabaseError(
                f"Database error while fetching the bill of materials: {e}"
            ) from e

    @staticmethod
    async def set_component(
        serial_number: str, line: ComponentLineCreate, session: AsyncSession
    ) -> dict | None:
        """Adds a component or changes its quantity.

        `MERGE` on `ComponentInstance.id = serialNumber + '_' + productNumber` — the same
        key as the automatic copy (`_copy_bom`), so a call for a component already present
        hits exactly its node and only changes `quantity`.

        **Locked after the release:** the bill of materials could otherwise be changed
        after stock had already been reserved for it.

        Args:
            serial_number (str): The business key of the asset.
            line (ComponentLineCreate): Product number and quantity.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            dict | None: The fields of `InstalledComponent` plus `was_updated`, or `None`
                when the serial number does not exist. `was_updated` is `True` when the
                component already existed (the caller then answers with 200 instead of 201).

        Raises:
            NotFoundError: When the product does not exist.
            BusinessLogicError: When the bill of materials is already released.
            DatabaseError: On unexpected errors during the write.
        """
        context_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        OPTIONAL MATCH (p:Product {number: $productNumber})
        OPTIONAL MATCH (a)-[:HAS_COMPONENT]->(existing:ComponentInstance {id: $serialNumber + '_' + $productNumber})
        WHERE coalesce(existing.status, 'active') = 'active'
        RETURN coalesce(a.bomReleased, false) AS released,
               p.number IS NOT NULL           AS productFound,
               existing IS NOT NULL           AS alreadyExists
        """
        set_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        MATCH (p:Product {number: $productNumber})
        MERGE (ci:ComponentInstance {id: $serialNumber + '_' + $productNumber})
        ON CREATE SET ci.status = 'active', ci.installedOn = a.installedOn
        MERGE (a)-[rel:HAS_COMPONENT]->(ci)
        MERGE (ci)-[:IS_TYPE]->(p)
        SET rel.quantity = $quantity
        RETURN p.number AS productNumber, p.label AS label,
               rel.quantity AS quantity, ci.installedOn AS installedOn
        """

        async def _set_component(tx):
            context = await (await tx.run(context_query, {
                "serialNumber": serial_number, "productNumber": line.productNumber,
            })).single()
            if context is None:
                return None
            if not context["productFound"]:
                raise NotFoundError(f"Product '{line.productNumber}' does not exist.")
            if context["released"]:
                raise BusinessLogicError(
                    f"The bill of materials of asset '{serial_number}' is already released "
                    "and can no longer be changed."
                )
            component = await (await tx.run(set_query, {
                "serialNumber": serial_number,
                "productNumber": line.productNumber,
                "quantity": float(line.quantity),
            })).single()
            return {**dict(component), "was_updated": context["alreadyExists"]}

        try:
            return await session.execute_write(_set_component)
        except Neo4jError as e:
            raise DatabaseError(
                f"Database error while changing the bill of materials: {e}"
            ) from e

    @staticmethod
    async def delete_component(
        serial_number: str, product_number: str, session: AsyncSession
    ) -> ComponentLineDeleted | None:
        """Strikes a component from the bill of materials of an asset.

        Deletes the `ComponentInstance` node entirely including its edges — unlike a
        cancelled document line there is no sensible intermediate state "struck but still
        visible" here: before the release a struck line is a planning mistake, not a
        recordable event.

        **Locked after the release**, for the same reason as `set_component`.

        Args:
            serial_number (str): The business key of the asset.
            product_number (str): Product number of the component to strike.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ComponentLineDeleted | None: The result, or `None` when the serial number does
                not exist.

        Raises:
            NotFoundError: When the asset does not carry this component.
            BusinessLogicError: When the bill of materials is already released.
            DatabaseError: On unexpected errors during the write.
        """
        context_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        OPTIONAL MATCH (a)-[:HAS_COMPONENT]->(ci:ComponentInstance {id: $serialNumber + '_' + $productNumber})
        WHERE coalesce(ci.status, 'active') = 'active'
        RETURN coalesce(a.bomReleased, false) AS released,
               ci IS NOT NULL                 AS componentFound
        """
        delete_query = """
        MATCH (:AssetInstance {serialNumber: $serialNumber})-[:HAS_COMPONENT]
              ->(ci:ComponentInstance {id: $serialNumber + '_' + $productNumber})
        DETACH DELETE ci
        """
        remainder_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        OPTIONAL MATCH (a)-[:HAS_COMPONENT]->(ci:ComponentInstance)
        WHERE coalesce(ci.status, 'active') = 'active'
        RETURN count(ci) AS remaining
        """

        async def _delete_component(tx):
            context = await (await tx.run(context_query, {
                "serialNumber": serial_number, "productNumber": product_number,
            })).single()
            if context is None:
                return None
            if not context["componentFound"]:
                raise NotFoundError(
                    f"Asset '{serial_number}' carries no component '{product_number}'."
                )
            if context["released"]:
                raise BusinessLogicError(
                    f"The bill of materials of asset '{serial_number}' is already released "
                    "and can no longer be changed."
                )
            await (await tx.run(delete_query, {
                "serialNumber": serial_number, "productNumber": product_number,
            })).consume()
            remainder = await (await tx.run(
                remainder_query, {"serialNumber": serial_number}
            )).single()
            return remainder["remaining"]

        try:
            remaining = await session.execute_write(_delete_component)
            if remaining is None:
                return None
            return ComponentLineDeleted(
                serialNumber=serial_number,
                productNumber=product_number,
                remainingLines=remaining,
            )
        except Neo4jError as e:
            raise DatabaseError(f"Database error while deleting the component: {e}") from e

    @staticmethod
    async def get_asset_drafts(session: AsyncSession) -> list[AssetDraft]:
        """Fetches the open asset drafts: order confirmations with
        `Document.assetPurpose == 'newAsset'` that do not carry an `AssetInstance` yet
        (`BASED_ON_DOCUMENT`).

        There is no catalogue product representing a whole asset — the lines of the
        document itself are the proposal for the bill of materials that engineering takes
        over or adjusts on confirmation. A document becomes at most one asset.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[AssetDraft]: One row per open document, sorted by document number, or an
                empty list when there is currently nothing to confirm.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        query = """
        MATCH (d:Document {type: 'OrderConfirmation', assetPurpose: 'newAsset'})
        WHERE NOT EXISTS { MATCH (d)<-[:BASED_ON_DOCUMENT]-(:AssetInstance) }
        MATCH (d)-[:HAS_LINE]->(line:DocumentLine)-[:OF_PRODUCT]->(p:Product)
        WHERE coalesce(line.cancelled, false) = false
        WITH d, collect({productNumber: p.number, label: p.label, quantity: line.quantity}) AS lines
        WHERE size(lines) > 0
        OPTIONAL MATCH (d)-[:BELONGS_TO_CUSTOMER]->(c:Customer)
        RETURN d.number AS documentNumber,
               CASE WHEN c IS NULL THEN null ELSE {id: c.id, name: c.name} END AS customer,
               lines
        ORDER BY documentNumber
        """
        try:
            records = await read_many(session, query)
            return [AssetDraft.model_validate(record) for record in records]
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the asset drafts: {e}") from e

    @staticmethod
    async def confirm_draft(
        confirmation: AssetDraftConfirmation,
        employee_id: str,
        session: AsyncSession,
    ) -> list[AssetCreated]:
        """Confirms an asset draft: creates an `AssetInstance`, writes `components` as its
        bill of materials and marks it as released — in a single transaction.

        Merges what used to be two separate steps: the automatic creation on the order
        confirmation and the later, separate release (`set_release`).

        **Books only the excess quantity:** whatever stood on the document was already
        reserved regularly with its own order confirmation — booking for it again here
        would bind the same physical stock twice. Does engineering enter a component on
        confirmation that did not stand on the document, or a higher quantity, nobody has
        booked that difference yet: it is reserved against the main warehouse here, in the
        same transaction. Does the stock not suffice for it, no asset comes about either.

        Args:
            confirmation (AssetDraftConfirmation): Document, bill of materials and note.
            employee_id (str): Id of the confirming employee, from the auth token.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[AssetCreated]: The newly created, already released asset — always exactly
                one element (the same answer contract as before, when a confirmation could
                still create several assets at once).

        Raises:
            NotFoundError: When document, employee or a product number given does not
                exist.
            BusinessLogicError: When the document is no order confirmation with
                `assetPurpose == 'newAsset'`, already carries an asset, or a component with
                unit 'pcs' carries a fractional quantity.
            DatabaseError: On unexpected errors during the write.
        """
        # Besides type and state the document lines and the main warehouse: both are
        # needed for the difference reservation further down and have to be read in the
        # same transaction as the write.
        check_query = """
        MATCH (d:Document {number: $documentNumber})
        OPTIONAL MATCH (d)<-[:BASED_ON_DOCUMENT]-(existing:AssetInstance)
        OPTIONAL MATCH (w:Location {type: 'Warehouse'})
        OPTIONAL MATCH (d)-[:HAS_LINE]->(line:DocumentLine)-[:OF_PRODUCT]->(p:Product)
        WHERE coalesce(line.cancelled, false) = false
        RETURN d.type AS documentType, d.assetPurpose AS assetPurpose,
               count(DISTINCT existing) AS existingCount,
               collect(w.id)[0] AS mainWarehouseId,
               collect(CASE WHEN p IS NULL THEN null
                            ELSE {productNumber: p.number, quantity: line.quantity} END) AS documentLines
        """
        # Stock and reservation per excess quantity to be booked, against the main
        # warehouse. Counterpart to `stock_query` in `set_release`; a component never
        # booked has no stock level node, then 0/0 applies and the check still works
        # correctly.
        excess_stock_query = """
        UNWIND $lines AS line
        OPTIONAL MATCH (s:StockLevel {id: line.productNumber + '_' + $locationId})
        RETURN line.productNumber AS productNumber, line.required AS required,
               coalesce(s.quantity, 0.0) AS quantity, coalesce(s.reserved, 0.0) AS reserved
        """
        release_query = """
        MATCH (a:AssetInstance {serialNumber: $serialNumber})
        SET a.bomReleased = true,
            a.releasedOn  = date(),
            a.releaseNote = $note,
            a.updatedAt   = datetime()
        WITH a
        MATCH (e:Employee {id: $employeeId})
        CREATE (a)-[:RELEASED_BY]->(e)
        """

        async def _confirm(tx):
            context = await (await tx.run(check_query, {
                "documentNumber": confirmation.documentNumber,
            })).single()

            if context is None or context["documentType"] is None:
                raise NotFoundError(
                    f"Document '{confirmation.documentNumber}' does not exist."
                )
            if context["documentType"] != "OrderConfirmation":
                raise BusinessLogicError(
                    f"Document '{confirmation.documentNumber}' is of type "
                    f"'{context['documentType']}'. An asset draft can only be confirmed "
                    "against an order confirmation."
                )
            if context["assetPurpose"] != "newAsset":
                raise BusinessLogicError(
                    f"Document '{confirmation.documentNumber}' is not marked as 'newAsset' "
                    "(assetPurpose)."
                )
            if context["existingCount"]:
                raise BusinessLogicError(
                    f"An asset has already been confirmed for document "
                    f"'{confirmation.documentNumber}'."
                )

            created = await AssetRepository._create_asset_in_tx(
                tx,
                AssetCreate(documentNumber=confirmation.documentNumber),
                component_override=confirmation.components,
            )

            employee = await (await tx.run(
                "MATCH (e:Employee {id: $id}) RETURN e.id AS id", {"id": employee_id}
            )).single()
            if employee is None:
                raise NotFoundError(f"Employee '{employee_id}' does not exist.")

            await (await tx.run(release_query, {
                "serialNumber": created["serialNumber"],
                "note": confirmation.note,
                "employeeId": employee_id,
            })).consume()

            # Reserve only the excess. What stood on the document was already reserved
            # regularly with its order confirmation — booking for it again here would bind
            # the same physical stock twice. What engineering enters beyond that has been
            # booked by nobody so far: without this booking the component would stand in
            # the bill of materials of a released asset while the warehouse is free to keep
            # selling its stock.
            excess = excess_over_document(
                confirmation.components, context["documentLines"]
            )

            # Check before anything at all is booked — the same rule as on the
            # bill-of-materials release (`set_release`).
            #
            # Without it, `_effect` caps a reservation silently at the available stock
            # instead of rejecting it. On an order confirmation that is right: the
            # unreserved rest stays as `openQuantity` on the line and shows up in the
            # reorder suggestions. Here neither of those exists — the asset would come
            # about with components in its bill of materials that nobody has bound, and
            # nobody would learn of it. A rejected operation, by contrast, can be repeated
            # once the goods are there.
            if excess:
                if context["mainWarehouseId"] is None:
                    raise BusinessLogicError(
                        f"The asset draft for '{confirmation.documentNumber}' cannot be "
                        "confirmed: no main warehouse is on file, so the added quantity "
                        "cannot be reserved."
                    )
                stock_rows = await (await tx.run(excess_stock_query, {
                    "lines": [
                        {"productNumber": number, "required": quantity}
                        for number, quantity in excess
                    ],
                    "locationId": context["mainWarehouseId"],
                })).data()
                insufficient = [
                    row["productNumber"] for row in stock_rows
                    if row["quantity"] - row["reserved"] < row["required"]
                ]
                if insufficient:
                    raise BusinessLogicError(
                        f"The asset draft for '{confirmation.documentNumber}' cannot be "
                        "confirmed: the available stock does not suffice for the added "
                        f"quantity of {', '.join(sorted(insufficient))}. Neither an asset "
                        "nor a booking has come about."
                    )

            for product_number, quantity in excess:
                await post_movement(
                    tx,
                    movement_params(
                        product_number,
                        context["mainWarehouseId"],
                        "Reservation",
                        quantity,
                        note=(
                            "Reservation of the quantity added by engineering while "
                            f"confirming the asset draft for {confirmation.documentNumber}"
                        ),
                    ),
                )

            return [created]

        try:
            created = await session.execute_write(_confirm)
            return [
                AssetCreated(
                    serialNumber=c["serialNumber"],
                    internalNumber=c["internalNumber"],
                    status="released",
                )
                for c in created
            ]
        except ConstraintError as e:
            raise DuplicateKeyError(
                f"The draft for document '{confirmation.documentNumber}' could not be "
                "confirmed: serial number already taken."
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error during the draft confirmation: {e}") from e

    @staticmethod
    async def get_spare_parts_drafts(session: AsyncSession) -> list[SparePartsDraft]:
        """Fetches the open spare-parts drafts: order confirmations with
        `Document.assetPurpose == 'spareParts'` that do not carry a `SPARE_PART_FOR` edge
        to an existing `AssetInstance` yet.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[SparePartsDraft]: One row per open document, or an empty list when there
                is currently nothing to link.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        query = """
        MATCH (d:Document {type: 'OrderConfirmation', assetPurpose: 'spareParts'})
        WHERE NOT (d)-[:SPARE_PART_FOR]->(:AssetInstance)
        OPTIONAL MATCH (d)-[:BELONGS_TO_CUSTOMER]->(c:Customer)
        RETURN d.number AS documentNumber,
               CASE WHEN c IS NULL THEN null ELSE {id: c.id, name: c.name} END AS customer,
               d.date AS date
        ORDER BY d.date, d.number
        """
        try:
            records = await read_many(session, query)
            return [SparePartsDraft.model_validate(record) for record in records]
        except Neo4jError as e:
            raise DatabaseError(
                f"Database error while fetching the spare-parts drafts: {e}"
            ) from e

    @staticmethod
    async def link_spare_parts(
        document_number: str,
        link: SparePartsLink,
        session: AsyncSession,
    ) -> SparePartsLinkCreated | None:
        """Links a spare-parts document with one or more existing assets (edge
        `SPARE_PART_FOR`). Pure traceability — unlike with an asset draft no new
        `AssetInstance` and no reservation come about.

        Args:
            document_number (str): Number of the spare-parts document.
            link (SparePartsLink): Serial numbers of the target assets.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            SparePartsLinkCreated | None: The linked serial numbers, or `None` when the
                document does not exist.

        Raises:
            NotFoundError: When a serial number given belongs to no asset. All or nothing:
                is even one missing, not a single edge comes about.
            DatabaseError: On unexpected errors during the write.
        """
        check_query = """
        MATCH (d:Document {number: $documentNumber})
        UNWIND $serialNumbers AS serialNumber
        OPTIONAL MATCH (a:AssetInstance {serialNumber: serialNumber})
        RETURN collect(DISTINCT CASE WHEN a IS NULL THEN serialNumber END) AS missing
        """
        link_query = """
        MATCH (d:Document {number: $documentNumber})
        UNWIND $serialNumbers AS serialNumber
        MATCH (a:AssetInstance {serialNumber: serialNumber})
        MERGE (d)-[:SPARE_PART_FOR]->(a)
        """

        async def _link(tx):
            context = await (await tx.run(check_query, {
                "documentNumber": document_number,
                "serialNumbers": link.serialNumbers,
            })).single()
            if context is None:
                return None
            missing = sorted(s for s in context["missing"] if s is not None)
            if missing:
                raise NotFoundError(f"Asset(s) not found: {', '.join(missing)}.")
            await (await tx.run(link_query, {
                "documentNumber": document_number,
                "serialNumbers": link.serialNumbers,
            })).consume()
            return True

        try:
            result = await session.execute_write(_link)
            if result is None:
                return None
            return SparePartsLinkCreated(
                documentNumber=document_number, serialNumbers=link.serialNumbers
            )
        except Neo4jError as e:
            raise DatabaseError(f"Database error during the spare-parts link: {e}") from e


class AssetServiceRepository:
    """Repository for the service-relevant asset and component queries.

    Encapsulates the graph analysis in Neo4j that determines the parts falling due,
    including the link to assets, customers and account managers.
    """

    @staticmethod
    async def get_due_services(session: AsyncSession) -> list[ServiceForecast]:
        """Determines all components whose service interval expires within the next 30
        days.

        Traverses the graph starting from active component instances over their products
        (wear parts) to the assets, customers and responsible employees.

        The interval runs from the shipping date of the asset — unless a service has
        already been reported (`ServiceEvent.completedAt`, see `set_completion`): then the
        next cycle counts from the completion date and no longer from shipping. Without
        that switch the date would stay the same for ever after a report, and a part whose
        interval expires again after the swap would never be reported.

        **Additionally filters on `isWearPart = true`.** The digital twin holds every
        component of a bill of materials, not only wear parts (see
        `AssetRepository._copy_bom`) — without this filter screws and other non-wear parts
        would be reported as due for service as soon as they happened to carry a
        `serviceIntervalMonths`.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[ServiceForecast]: A list of validated service cases with account manager,
                customer, serial number, wear part, installation date and due date.

        Raises:
            DatabaseError: On unexpected errors during the graph query.
        """
        query = """
        MATCH (ci:ComponentInstance)-[:IS_TYPE]->(p:Product)
        WHERE ci.status = 'active'
          AND p.serviceIntervalMonths IS NOT NULL
          AND p.isWearPart = true

        // The asset is fetched before the interval is calculated, because the shipping
        // date hangs off it and not off the component. Assets without a shipping date
        // deliberately drop out instead of silently falling back to the installation date
        // and thereby reporting a wrong replacement date.
        MATCH (ci)<-[:HAS_COMPONENT*1..]-(a:AssetInstance)-[:SOLD_TO]->(c:Customer)
        WHERE a.shippedOn IS NOT NULL

        // Stored completion report, if there is one already (see set_completion). Has to
        // stand BEFORE the interval calculation: it supplies the anchor for the next cycle
        // (completedAt instead of shippedOn) as soon as one report has been made.
        OPTIONAL MATCH (event:ServiceEvent)-[:CONCERNS]->(ci)

        WITH ci, p, a, c, event,
             date(coalesce(event.completedAt, a.shippedOn))
               + duration({months: p.serviceIntervalMonths}) AS replacementDueOn
        WHERE replacementDueOn <= date() + duration({days: 30})

        // OPTIONAL MATCH keeps records without an assigned account manager from vanishing
        OPTIONAL MATCH (c)<-[:ACCOUNT_MANAGER_OF]-(e:Employee)

        RETURN
            ci.id AS componentInstanceId,
            coalesce(e.name, 'Unassigned') AS accountManager,
            c.name AS customer,
            a.serialNumber AS serialNumber,
            p.label AS wearPart,
            ci.installedOn AS installedOn,
            replacementDueOn,
            event.completedAt AS completedAt,
            event.technicianInitials AS technicianInitials,
            event.note AS completionNote
        ORDER BY replacementDueOn ASC
        """

        try:
            records = await read_many(session, query)
            return [
                ServiceForecast(
                    componentInstanceId=record["componentInstanceId"],
                    accountManager=record["accountManager"],
                    customer=record["customer"],
                    serialNumber=record["serialNumber"],
                    wearPart=record["wearPart"],
                    installedOn=record["installedOn"],
                    replacementDueOn=record["replacementDueOn"],
                    completion=ServiceCompletion(
                        completedAt=record["completedAt"],
                        technicianInitials=record["technicianInitials"],
                        note=record["completionNote"],
                    ) if record["completedAt"] else None,
                )
                for record in records
            ]
        except Neo4jError as e:
            raise DatabaseError(f"Internal database error during the service query: {e}") from e

    @staticmethod
    async def set_completion(
        component_instance_id: str,
        completion: ServiceCompletionRequest,
        session: AsyncSession,
    ) -> ServiceCompletion | None:
        """Reports a service as done and creates the `ServiceEvent` node for it.

        `MERGE` on `ServiceEvent.id = componentInstanceId` keeps the relation 1:1 to the
        `ComponentInstance`. `get_due_services` afterwards uses `completedAt` as the new
        anchor for the next cycle (instead of counting from the shipping date) — reporting
        again therefore deliberately overwrites the previous report: there is no history of
        several completions, only the current anchor point.

        Args:
            component_instance_id (str): Id of the `ComponentInstance`
                ('serialNumber_productNumber').
            completion (ServiceCompletionRequest): Technician initials and note from the
                request. `completedAt` is set by the server itself.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            ServiceCompletion | None: The stored state, or None when the component instance
                does not exist.

        Raises:
            DatabaseError: On unexpected errors during the write.
        """
        query = """
        MATCH (ci:ComponentInstance {id: $componentInstanceId})
        MERGE (event:ServiceEvent {id: $componentInstanceId})
        ON CREATE SET event.createdAt = datetime()
        SET event.completedAt        = date(),
            event.technicianInitials = $technicianInitials,
            event.note               = $note,
            event.updatedAt          = datetime()
        MERGE (event)-[:CONCERNS]->(ci)
        RETURN event.completedAt AS completedAt,
               event.technicianInitials AS technicianInitials,
               event.note AS note
        """
        try:
            record = await write_single(
                session,
                query,
                componentInstanceId=component_instance_id,
                technicianInitials=completion.technicianInitials,
                note=completion.note,
            )
        except Neo4jError as e:
            raise DatabaseError(f"Database error while reporting the service: {e}") from e

        if record is None:
            return None
        return ServiceCompletion(
            completedAt=record["completedAt"],
            technicianInitials=record["technicianInitials"],
            note=record["note"],
        )
