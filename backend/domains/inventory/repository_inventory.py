"""Cypher queries of the inventory domain: locations, stock levels and movements."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from neo4j import AsyncManagedTransaction, AsyncSession, Record
from neo4j.exceptions import Neo4jError
from pydantic import ValidationError

from core.exceptions import BusinessLogicError, DatabaseError, NotFoundError
from core.neo4j_query import read_many, read_single

from .schemas_inventory import (
    Location,
    MovementType,
    Stock,
    StockMovement,
    StockMovementCreate,
    StockMovementResponse,
)

# --- Shared booking logic ----------------------------------------------------
# The two queries and the transaction function live at module level, because
# `/api/stock-movements` is not the only writer: a document triggers its own stock effect
# and has to be able to hang the booking into its own transaction. A second copy of this
# Cypher would mean maintaining the invariant "quantity = f(movements)" in two places.

# Step 1: check existence and make sure the stock node exists.
STOCK_QUERY = """
MATCH (p:Product  {number: $productNumber})
MATCH (l:Location {id: $locationId})
MERGE (s:StockLevel {id: $stockId})
  ON CREATE SET s.quantity = 0.0, s.reserved = 0.0
MERGE (p)-[:HAS_STOCK]->(s)
MERGE (s)-[:AT_LOCATION]->(l)
WITH p, s
OPTIONAL MATCH (c:Customer {id: $customerId})
OPTIONAL MATCH (d:Document {number: $documentNumber})
RETURN coalesce(s.quantity, 0.0) AS quantity,
       coalesce(s.reserved, 0.0) AS reserved,
       p.unit                    AS unit,
       c IS NOT NULL             AS customerFound,
       d IS NOT NULL             AS documentFound
"""

# Step 2: create the movement, set the edges, advance the cache.
MOVEMENT_QUERY = """
MATCH (s:StockLevel {id: $stockId})
CREATE (m:StockMovement {
    id:                $movementId,
    type:              $type,
    quantity:          $quantity,
    createdAt:         $createdAt,
    note:              $note,
    documentNumber:    $documentNumber,
    lineNumber:        $lineNumber,
    purchasePriceCent: $purchasePriceCent
})
CREATE (m)-[:POSTED_TO]->(s)
SET s.quantity = $newQuantity,
    s.reserved = $newReserved
WITH m, s
OPTIONAL MATCH (c:Customer {id: $customerId})
FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END |
    MERGE (m)-[:CONCERNS_CUSTOMER]->(c))
WITH m, s
OPTIONAL MATCH (d:Document {number: $documentNumber})
FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [1] END |
    MERGE (m)-[:BASED_ON_DOCUMENT]->(d))
RETURN s.quantity AS newQuantity, s.reserved AS reserved
"""

# The unit for which a fractional quantity makes no sense. A piece cannot be moved in
# halves; metres and kilograms can.
_WHOLE_NUMBER_UNIT = "pcs"


def movement_params(
    product_number: str,
    location_id: str,
    type: MovementType,
    quantity: float,
    *,
    document_number: str | None = None,
    line_number: int | None = None,
    customer_id: str | None = None,
    note: str | None = None,
    purchase_price: Decimal | None = None,
) -> dict:
    """Builds the complete parameter set of one booking.

    The single place where the movement id, the stock key and the cent amount are formed.
    Every caller therefore binds the same keys — a second construction site would mean one
    domain forming the stock key differently and thereby creating a second stock node
    beside the existing one.

    The euro `Decimal` is converted to integer cents here: the driver rejects `Decimal` as
    a query parameter outright.

    Args:
        product_number (str): The product being booked.
        location_id (str): The location booked against.
        type (MovementType): The movement type.
        quantity (float): The quantity booked, signed for `Correction`.
        document_number (str | None): Document the booking refers to.
        line_number (int | None): Line number of the triggering document line. Needed
            because the same product can appear more than once on a sales document, so
            `product_number` alone no longer identifies the line — the basis for a
            line-level partial cancellation.
        customer_id (str | None): Customer the booking concerns.
        note (str | None): Free text for the booking.
        purchase_price (Decimal | None): Purchase price per unit in euro.

    Returns:
        dict: The bound parameters for `post_movement`.
    """
    return {
        "movementId":        f"mov-{uuid4()}",
        "stockId":           f"{product_number}_{location_id}",
        "productNumber":     product_number,
        "locationId":        location_id,
        "customerId":        customer_id,
        "type":              type,
        "quantity":          quantity,
        "documentNumber":    document_number,
        "lineNumber":        line_number,
        "note":              note,
        "purchasePriceCent": (
            None if purchase_price is None
            else int((purchase_price * 100).to_integral_value())
        ),
        "createdAt":         datetime.now(UTC),
    }


async def post_movement(
    tx: AsyncManagedTransaction, params: dict, *, target_reserved: bool = False
) -> Record:
    """Books a single stock movement inside a running transaction.

    Both steps run in the same transaction function: called separately, a gap would open
    between the stock check and the booking, in which a concurrent request could plan the
    same stock.

    Takes `tx` and opens no transaction of its own. Only that way can a document create its
    lines together with the bookings that belong to them — if the stock is not enough for
    one of the lines, the whole operation is rolled back.

    Args:
        tx (AsyncManagedTransaction): The running Neo4j write transaction.
        params (dict): The parameter set from `movement_params`.
        target_reserved (bool): Only effective for `type='Correction'` — see `_effect`.

    Returns:
        Record: The stock after the booking, with `newQuantity` and `reserved`.

    Raises:
        NotFoundError: When product or location do not exist (as the placeholder
            `__product_or_location__`, which the caller resolves), or when a given customer
            or document is missing.
        BusinessLogicError: When the stock would go negative, or when a product measured in
            whole pieces is asked to move a fractional quantity — except for `Correction`,
            which is exempt.
        DatabaseError: When the booking returns no result.
    """
    stock_result = await tx.run(STOCK_QUERY, params)
    stock = await stock_result.single()

    if stock is None:
        raise NotFoundError("__product_or_location__")
    if params["customerId"] is not None and not stock["customerFound"]:
        raise NotFoundError(f"A customer with the id '{params['customerId']}' does not exist.")
    if params["documentNumber"] is not None and not stock["documentFound"]:
        raise NotFoundError(
            f"A document with the number '{params['documentNumber']}' does not exist."
        )

    # A piece cannot be moved in halves. The rule sits on the booking itself and therefore
    # also covers POST /api/stock-movements.
    #
    # `Correction` is deliberately exempt: it is the only signed movement type and the
    # emergency exit for wrong stock figures. If 2.5 pieces sit in the system through bad
    # data, straightening it out needs exactly that -0.5 — a rule forbidding it would lock
    # the tool against the state it exists to remove.
    if (
        params["type"] != "Correction"
        and stock["unit"] == _WHOLE_NUMBER_UNIT
        and params["quantity"] % 1 != 0
    ):
        raise BusinessLogicError(
            f"Quantity has to be a whole number for unit '{_WHOLE_NUMBER_UNIT}': product "
            f"'{params['productNumber']}' with quantity {params['quantity']} on "
            f"'{params['type']}'."
        )

    new_quantity, new_reserved = StockMovementRepository._effect(
        params["type"], params["quantity"], stock["quantity"], stock["reserved"], target_reserved
    )
    # On a reservation `_effect` may have booked less than the requested `quantity`
    # (capped at the available stock). The movement therefore records the actual
    # difference, not the request — otherwise the event would diverge from `s.reserved`
    # and the invariant `quantity = f(movements)` would be broken. For every other
    # movement type the difference equals the request and this line changes nothing.
    actual_quantity = (
        new_reserved - stock["reserved"] if params["type"] == "Reservation"
        else params["quantity"]
    )
    movement_result = await tx.run(
        MOVEMENT_QUERY,
        {
            **params,
            "quantity": actual_quantity,
            "newQuantity": new_quantity,
            "newReserved": new_reserved,
        },
    )
    record = await movement_result.single()

    if record is None:
        raise DatabaseError("Booking was not carried out, Neo4j returned an empty result.")
    return record


async def post_transfer(
    tx: AsyncManagedTransaction, source_params: dict, target_params: dict
) -> tuple[Record, Record]:
    """Books a transfer as an outgoing and an incoming movement, in one go.

    Calls `post_movement` twice on the same `tx` rather than writing a query of its own —
    both bookings are already a signed 'Transfer' in their own right (see `_effect`); only
    the ordering and the shared transaction are new. If the incoming booking fails (because
    the destination location does not exist, say), the outgoing one is not committed yet:
    there is no intermediate state in which goods left the source and never arrived
    anywhere.

    Args:
        tx (AsyncManagedTransaction): The running Neo4j write transaction.
        source_params (dict): Parameter set for the outgoing booking, `quantity` negative.
        target_params (dict): Parameter set for the incoming booking, `quantity` positive.

    Returns:
        tuple[Record, Record]: Stock after the booking, source first then destination.

    Raises:
        NotFoundError: With `__source__` or `__target__` instead of the generic placeholder
            from `post_movement` — otherwise the caller cannot tell which of the two
            locations is missing.
        BusinessLogicError: When the source location would go negative.
    """
    try:
        source_record = await post_movement(tx, source_params)
    except NotFoundError as e:
        if str(e) != "__product_or_location__":
            raise
        raise NotFoundError("__source__") from e

    try:
        target_record = await post_movement(tx, target_params)
    except NotFoundError as e:
        if str(e) != "__product_or_location__":
            raise
        raise NotFoundError("__target__") from e

    return source_record, target_record


class LocationRepository:
    """Reads the location master data."""

    @staticmethod
    async def get_locations(session: AsyncSession) -> list[Location]:
        """Reads every `Location` node.

        The `id` is cast to a string in the projection: depending on origin it may sit in
        the graph as a number or as a string, while the read model carries it uniformly as
        a string.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[Location]: Every location, or an empty list.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            query = """
            MATCH (l:Location)
            RETURN l{.*, id: toString(l.id)} AS l
            ORDER BY l.id
            """
            records = await read_many(session, query)
            return [Location.model_validate(record["l"]) for record in records]
        except ValidationError as e:
            raise DatabaseError(f"A location could not be read from the graph: {e}") from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the locations: {e}") from e


# Shared by both stock queries: the aggregation over every location of one product plus
# the per-location breakdown. OPTIONAL MATCH makes a product without any stock node appear
# with quantity 0 instead of dropping out of the result.
_STOCK_AGGREGATION = """
OPTIONAL MATCH (p)-[:HAS_STOCK]->(s:StockLevel)-[:AT_LOCATION]->(l:Location)
WITH p,
    sum(coalesce(s.quantity, 0.0)) AS totalStock,
    sum(coalesce(s.reserved, 0.0)) AS reserved,
    collect(CASE WHEN s IS NULL THEN NULL ELSE {
        locationId:   toString(l.id),
        locationName: l.name,
        quantity:     coalesce(s.quantity, 0.0),
        reserved:     coalesce(s.reserved, 0.0),
        available:    coalesce(s.quantity, 0.0) - coalesce(s.reserved, 0.0)
    } END) AS byLocation
"""

_STOCK_PROJECTION = """
{
    productNumber: p.number,
    label:         p.label,
    totalStock:    totalStock,
    reserved:      reserved,
    available:     totalStock - reserved,
    byLocation:    byLocation
} AS stock
"""


class StockRepository:
    """Repository for the stock queries across Product, StockLevel and Location."""

    @staticmethod
    async def get_stock(product_number: str, session: AsyncSession) -> Stock | None:
        """Reads one product's stock, summed and broken down per location.

        Returns `None` when the product does not exist — the distinction between "unknown"
        and "stock 0" is drawn by the service.

        Args:
            product_number (str): The product number.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Stock | None: The aggregated stock, or None when the product does not exist.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            query = f"""
            MATCH (p:Product {{number: $productNumber}})
            {_STOCK_AGGREGATION}
            RETURN {_STOCK_PROJECTION}
            """
            record = await read_single(session, query, productNumber=product_number)
            if record:
                return Stock.model_validate(record["stock"])
            return None
        except ValidationError as e:
            raise DatabaseError(f"Stock could not be read from the graph: {e}") from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the stock: {e}") from e

    @staticmethod
    async def get_stock_list(
        session: AsyncSession, below_min_stock: bool = False
    ) -> list[Stock]:
        """Reads the stock of every product, ordered by product number.

        With `below_min_stock=True` only products remain whose total stock falls below a
        maintained minimum. The filter sits behind the `WITH`, because it reads the sum
        across all locations — before the aggregation that sum does not exist yet.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            below_min_stock (bool): Restricts to products below their minimum stock.

        Returns:
            list[Stock]: The stock of all (filtered) products.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        try:
            query = f"""
            MATCH (p:Product)
            {_STOCK_AGGREGATION}
            WHERE NOT $belowMinStock
               OR (p.minStock IS NOT NULL AND totalStock < p.minStock)
            RETURN {_STOCK_PROJECTION}
            ORDER BY p.number
            """
            records = await read_many(session, query, belowMinStock=below_min_stock)
            return [Stock.model_validate(record["stock"]) for record in records]
        except ValidationError as e:
            raise DatabaseError(f"Stock could not be read from the graph: {e}") from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the stock list: {e}") from e


class StockMovementRepository:
    """Repository for booking and reading stock movements.

    Every booking writes a `StockMovement` node as an audit entry **and** advances the
    stock — both in one transaction, so the log and the stock cannot drift apart. The
    transaction function for it lives at module level, because document handling hooks it
    into its own transaction.
    """

    @staticmethod
    def _to_movement(node_props: dict) -> StockMovement:
        """Converts the properties of a movement node into the Pydantic schema.

        Expects a flat dictionary as the map projection of the read queries delivers it:
        `locationName` and `customerName` are already the names behind the `POSTED_TO` and
        `CONCERNS_CUSTOMER` edges, not nodes. Missing keys are not an error — apart from
        `id`, everything in the read model is optional.

        Converts the purchase price stored as integer cents into a euro Decimal. When the
        cent value is missing, `purchasePrice` stays None: only a receipt carries a price,
        for the other movement types its absence is the normal case. A value of 0 is taken
        over as a recorded 0.00 EUR and not swallowed into None.

        Args:
            node_props (dict): The projected properties of the movement node.

        Returns:
            StockMovement: The validated movement object.

        Raises:
            DatabaseError: When the node holds data the read model cannot represent.
        """
        props = dict(node_props)
        cent = props.pop("purchasePriceCent", None)
        props["purchasePrice"] = None if cent is None else Decimal(cent) / 100

        try:
            return StockMovement.model_validate(props)
        except ValidationError as e:
            raise DatabaseError(
                f"Stock movement '{props.get('id')}' could not be read from the graph: {e}"
            ) from e

    @staticmethod
    async def post_movement(
        data: StockMovementCreate, session: AsyncSession
    ) -> StockMovementResponse:
        """Books a single movement and returns the new stock.

        Transfers are handled by `post_transfer`; those need two bookings with opposite
        signs and do not fit this flow.

        If product or location are missing, the query reports that jointly as
        `__product_or_location__`. Which of the two is missing is only resolved by a
        targeted follow-up query in the error path — the normal case therefore stays at two
        round trips.

        Args:
            data (StockMovementCreate): The movement to book.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            StockMovementResponse: Stock and reservation after the booking.

        Raises:
            NotFoundError: When product, location, customer or document do not exist.
            BusinessLogicError: When the stock would go negative.
            DatabaseError: On unexpected database failures.
        """
        params = movement_params(
            data.productNumber,
            data.locationId,
            data.type,
            data.quantity,
            document_number=data.documentNumber,
            customer_id=data.customerId,
            note=data.note,
            purchase_price=data.purchasePrice,
        )
        movement_id = params["movementId"]

        try:
            # The transaction function lives at module level so document handling can hang
            # it into its own transaction.
            record = await session.execute_write(post_movement, params)

            if record is None:
                raise DatabaseError(
                    "Booking was not carried out, Neo4j returned an empty result."
                )

            return StockMovementResponse(
                newQuantity=record["newQuantity"],
                reserved=record["reserved"],
                movementId=movement_id,
            )
        except NotFoundError as e:
            if str(e) != "__product_or_location__":
                raise
            raise await StockMovementRepository._missing_node(
                data.productNumber, data.locationId, session
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while booking the movement: {e}") from e

    @staticmethod
    async def post_transfer(
        data: StockMovementCreate, session: AsyncSession
    ) -> StockMovementResponse:
        """Books a transfer as two movements in the same transaction.

        A path of its own rather than a `type` branch in `post_movement`: a transfer needs
        two parameter sets with opposite signs and returns two movement ids — neither fits
        the single-booking flow without threading a case distinction through every step.

        Args:
            data (StockMovementCreate): The transfer; `targetLocationId` is assumed to be
                set (enforced by the schema validator).
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            StockMovementResponse: Stock and movement id per location, source in the base
                fields, destination in `targetNewQuantity`/`targetMovementId`.

        Raises:
            NotFoundError: When product, source or destination location do not exist.
            BusinessLogicError: When the source location would go negative.
        """
        assert data.targetLocationId is not None  # enforced by the schema validator

        source_params = movement_params(
            data.productNumber,
            data.locationId,
            "Transfer",
            -data.quantity,
            document_number=data.documentNumber,
            note=data.note,
        )
        target_params = movement_params(
            data.productNumber,
            data.targetLocationId,
            "Transfer",
            data.quantity,
            document_number=data.documentNumber,
            note=data.note,
        )

        try:
            source_record, target_record = await session.execute_write(
                post_transfer, source_params, target_params
            )

            return StockMovementResponse(
                newQuantity=source_record["newQuantity"],
                reserved=source_record["reserved"],
                movementId=source_params["movementId"],
                targetNewQuantity=target_record["newQuantity"],
                targetMovementId=target_params["movementId"],
            )
        except NotFoundError as e:
            if str(e) == "__source__":
                raise await StockMovementRepository._missing_node(
                    data.productNumber, data.locationId, session
                ) from e
            if str(e) == "__target__":
                raise await StockMovementRepository._missing_node(
                    data.productNumber, data.targetLocationId, session
                ) from e
            raise
        except Neo4jError as e:
            raise DatabaseError(f"Database error while booking the transfer: {e}") from e

    @staticmethod
    def _effect(
        type: str, quantity: float, stock: float, reserved: float,
        target_reserved: bool = False,
    ) -> tuple[float, float]:
        """Computes how a booking changes stock and reservation.

        A pure function without database access, so the truth table of the five movement
        types can be tested in isolation.

        An `Issue` releases the matching reservation in the same operation, lowering both
        values. `reserved` is floored at 0: an issue without a preceding reservation is
        legitimate — a walk-in withdrawal, say — and must not drive the counter negative.

        The check runs against `quantity`, not against `quantity - reserved`. Otherwise a
        fully reserved line could no longer be shipped: the very issue the reservation was
        made for would fail against its own reservation.

        A `Reservation` is capped at the available stock instead of being rejected
        outright: an order confirmation for more than is available should still come into
        existence (otherwise it never shows up in the reorder analysis), it just must not
        reserve more than is actually there. What gets reserved is therefore
        `min(quantity, available)` with `available = stock - reserved`; the remainder stays
        on the ordering line as an open quantity. If `available` is already 0 or negative
        there is nothing left to reserve, and the whole booking — and with it the whole
        document, since everything runs in one transaction — is rejected.

        `target_reserved` gives a `Correction` a second target: by default it corrects
        `stock` (the sign of `quantity` carries the direction) — with
        `target_reserved=True` it corrects `reserved` instead, under the same sign rule.
        Needed as the counter-booking of a `Reservation` on a cancellation: a reservation
        never changes `stock`, so its counter-booking must not either — an ordinary
        `Correction` (targeting `stock`) would be the wrong booking here, not merely an
        imprecise one.

        Args:
            type (str): One of the five movement types.
            quantity (float): The quantity booked. Signed for `Correction`. An upper bound
                rather than a firm promise for `Reservation` — see above.
            stock (float): The stock before the booking.
            reserved (float): The reserved quantity before the booking.
            target_reserved (bool): Only effective for `Correction` — corrects `reserved`
                instead of `stock`.

        Returns:
            tuple[float, float]: Stock and reserved quantity after the booking.

        Raises:
            BusinessLogicError: When the stock would go negative, or when a `Reservation`
                finds nothing available any more.
        """
        if type == "Receipt":
            new_quantity, new_reserved = stock + quantity, reserved
        elif type == "Reservation":
            available = stock - reserved
            if available <= 0:
                raise BusinessLogicError(
                    f"A reservation of {quantity} is not possible: nothing is available "
                    f"any more (stock {stock}, already reserved {reserved})."
                )
            new_quantity, new_reserved = stock, reserved + min(quantity, available)
        elif type == "Issue":
            new_quantity, new_reserved = stock - quantity, max(reserved - quantity, 0.0)
        elif type == "Transfer":
            # Like a correction against stock: the sign of quantity carries the direction
            # (negative at the source, positive at the destination), reserved is untouched.
            # A transfer is not a reservation matter — it moves physical goods between our
            # own locations, not between free and planned.
            new_quantity, new_reserved = stock + quantity, reserved
        elif target_reserved:  # Correction against reserved
            new_quantity, new_reserved = stock, max(reserved + quantity, 0.0)
        else:  # Correction against stock — the sign of quantity carries the direction
            new_quantity, new_reserved = stock + quantity, reserved

        if new_quantity < 0:
            raise BusinessLogicError(
                f"A booking of type '{type}' over {quantity} is not possible: the stock is "
                f"{stock} and would fall to {new_quantity}."
            )
        return new_quantity, new_reserved

    @staticmethod
    async def _missing_node(
        product_number: str, location_id: str, session: AsyncSession
    ) -> NotFoundError:
        """Determines which of the two nodes is missing and builds the matching message.

        Runs exclusively in the error path. The alternative — querying both existences up
        front — would cost the normal path two extra round trips to obtain information it
        never needs.
        """
        record = await read_single(
            session,
            """
            OPTIONAL MATCH (p:Product  {number: $productNumber})
            OPTIONAL MATCH (l:Location {id: $locationId})
            RETURN p IS NOT NULL AS product, l IS NOT NULL AS location
            """,
            productNumber=product_number,
            locationId=location_id,
        )
        if record is not None and not record["product"]:
            return NotFoundError(f"A product with the number '{product_number}' does not exist.")
        if record is not None and not record["location"]:
            return NotFoundError(f"A location with the id '{location_id}' does not exist.")
        return NotFoundError(
            f"Product '{product_number}' or location '{location_id}' does not exist."
        )

    @staticmethod
    async def get_movements(
        session: AsyncSession,
        product_number: str | None = None,
        location_id: str | None = None,
        type: MovementType | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[StockMovement]:
        """Fetches the movement history, optionally filtered.

        Assembles the WHERE clause dynamically from the filters that are set. Only
        fragments hard-coded here ever reach the query string — every value from the client
        is bound as a Cypher parameter.

        `productNumber` and `locationId` are not properties of the movement; they are
        resolved through `POSTED_TO` and the stock node. That traversal is optional: an
        audit log must not swallow a row just because an edge is missing.

        Args:
            session (AsyncSession): The asynchronous Neo4j database session.
            product_number (str | None): Only movements of this product.
            location_id (str | None): Only movements in or out of this location.
            type (MovementType | None): Only movements of this type.
            from_date (date | None): Lower bound of the booking period, inclusive.
            to_date (date | None): Upper bound of the booking period, inclusive.

        Returns:
            list[StockMovement]: The movements found, newest first, or an empty list.

        Raises:
            DatabaseError: On unexpected failures during the query.
        """
        conditions: list[str] = []
        params: dict = {}

        if product_number:
            conditions.append("p.number = $productNumber")
            params["productNumber"] = product_number
        if location_id:
            conditions.append("toString(l.id) = $locationId")
            params["locationId"] = location_id
        if type:
            conditions.append("m.type = $type")
            params["type"] = type
        # The bounds arrive as dates, createdAt is a timestamp. The comparison still runs
        # against createdAt itself and not against date(createdAt): a function wrapped
        # around the property makes an index on it unusable.
        if from_date:
            conditions.append(
                "m.createdAt >= datetime({date: $fromDate, time: time('00:00:00Z')})"
            )
            params["fromDate"] = from_date
        if to_date:
            conditions.append(
                "m.createdAt < datetime({date: $toDate, time: time('00:00:00Z')})"
                " + duration({days: 1})"
            )
            params["toDate"] = to_date

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        try:
            query = f"""
            MATCH (m:StockMovement)
            OPTIONAL MATCH (p:Product)-[:HAS_STOCK]->(s:StockLevel)<-[:POSTED_TO]-(m)
            OPTIONAL MATCH (s)-[:AT_LOCATION]->(l:Location)
            OPTIONAL MATCH (m)-[:CONCERNS_CUSTOMER]->(c:Customer)
            WITH m, p, l, c
            {where_clause}
            RETURN {{
                id:                m.id,
                productNumber:     p.number,
                quantity:          m.quantity,
                type:              m.type,
                locationName:      l.name,
                documentNumber:    m.documentNumber,
                customerName:      c.name,
                purchasePriceCent: m.purchasePriceCent,
                createdAt:         m.createdAt
            }} AS movement
            ORDER BY m.createdAt DESC, m.id
            """
            records = await read_many(session, query, **params)
            return [
                StockMovementRepository._to_movement(record["movement"])
                for record in records
            ]
        except Neo4jError as e:
            raise DatabaseError(
                f"Database error while fetching the movement history: {e}"
            ) from e
