"""Cypher queries of the notifications domain: service, shortage, unavailable quantity.

One node type, three creation paths:

    (:Notification {id, type, done, createdAt, doneAt})
            -[:FOR_ROLE]-> (:Role)
            -[:CONCERNS]-> (:ComponentInstance) | (:DocumentLine)

`id` is a business key, not a generated id — the same choice as on the `ServiceEvent`
node in `assets`. Creation therefore runs through `MERGE` everywhere, not `CREATE`:
idempotence is the goal here (the same annual run or the same goods receipt must never
create a second row), not the detection of a duplicate key — which is what makes this the
justified exception from "CREATE, not MERGE", rather than merely an asserted one.
"""

from datetime import UTC, datetime

from neo4j import AsyncManagedTransaction, AsyncSession
from neo4j.exceptions import Neo4jError
from pydantic import ValidationError

from core.exceptions import DatabaseError, NotFoundError
from core.neo4j_query import read_many, read_single, write_single

from .schemas_notifications import Notification, UnavailableLine

# Shared projection of the CONCERNS target: both cases stand next to each other as
# OPTIONAL MATCH, because a notification only ever carries one of the two types — the
# other one stays null and drops out of the CASE. Needed by the list as well as by the
# tick-off answer, hence a fragment of its own.
#
# Deliberately one OPTIONAL MATCH per hop rather than a single multi-part pattern
# (n)-[:CONCERNS]->(line)-[:OF_PRODUCT]->(lineProduct): on a multi-part pattern an OPTIONAL
# MATCH binds ALL its variables to null as soon as any part does not match — were the
# OF_PRODUCT edge missing, `line` itself would be null although the CONCERNS edge exists.
# The same trap as an aggregation in front of a WHERE, only on the multi-stage OPTIONAL
# MATCH.
#
# `receivedQuantity` resolves the same aggregation as the ordered/received comparison in
# `sales` for a 'shortage' notification — at read time, not on creation
# (`create_quantity_deviation` deliberately stores no quantity on the node, see there): a
# reader is meant to see the CURRENT state, even when a follow-up delivery was booked
# between creation and display. The WITH below therefore aggregates over ALL variables
# bound up to this point (those of the component branch included) — for a service
# notification `line`, and with it `fulfilling`/`receivedQuantity`, simply stays null
# throughout, without a second case distinction being needed.
_CONCERNS_FRAGMENT = """
OPTIONAL MATCH (n)-[:CONCERNS]->(ci:ComponentInstance)
OPTIONAL MATCH (ci)-[:IS_TYPE]->(ciProduct:Product)
OPTIONAL MATCH (ci)<-[:HAS_COMPONENT]-(asset:AssetInstance)
OPTIONAL MATCH (n)-[:CONCERNS]->(line:DocumentLine)
OPTIONAL MATCH (line)-[:OF_PRODUCT]->(lineProduct:Product)
OPTIONAL MATCH (lineDocument:Document)-[:HAS_LINE]->(line)
OPTIONAL MATCH (lineDocument)-[:BELONGS_TO_CUSTOMER]->(lineCustomer:Customer)
OPTIONAL MATCH (line)<-[:FULFILS]-(fulfilling:DocumentLine)
WITH n, r, ci, ciProduct, asset, line, lineProduct, lineDocument, lineCustomer,
     sum(CASE WHEN fulfilling IS NOT NULL AND NOT coalesce(fulfilling.cancelled, false)
              THEN fulfilling.quantity ELSE 0.0 END) AS receivedQuantity
"""

# Two of the three cases hang off a DocumentLine and therefore have to be told apart over
# `n.type`, not over the node type: on `shortage` it is a purchase order line (ordered
# against received), on `unavailable` a delivery note line (what was not within reach at
# the location). The order of the CASE branches is thus required by the business rules —
# the specific branch stands before the general one.
_CONCERNS_PROJECTION = """
    CASE
        WHEN ci IS NOT NULL THEN {
            type: 'ComponentInstance',
            id: ci.id,
            productNumber: ciProduct.number,
            quantity: null,
            description: 'Asset ' + coalesce(asset.serialNumber, '?') + ' - '
                          + coalesce(ciProduct.label, ciProduct.number, '?')
        }
        WHEN line IS NOT NULL AND n.type = 'unavailable' THEN {
            type: 'DocumentLine',
            id: line.id,
            productNumber: lineProduct.number,
            quantity: n.quantity,
            description: 'Delivery note ' + coalesce(lineDocument.number, '?') + ', line '
                          + toString(coalesce(line.lineNumber, 0)) + ': '
                          + coalesce(lineProduct.label, lineProduct.number, '?')
                          + ' - ' + toString(coalesce(n.quantity, 0))
                          + ' ' + coalesce(lineProduct.unit, 'pcs') + ' missing'
                          + CASE WHEN lineCustomer IS NULL THEN ''
                                 ELSE ' for ' + coalesce(lineCustomer.name, lineCustomer.id) END
        }
        WHEN line IS NOT NULL THEN {
            type: 'DocumentLine',
            id: line.id,
            productNumber: lineProduct.number,
            quantity: null,
            description: 'Purchase order ' + coalesce(lineDocument.number, '?') + ', line '
                          + toString(coalesce(line.lineNumber, 0)) + ': '
                          + coalesce(lineProduct.label, lineProduct.number, '?')
                          + ' - ordered ' + toString(coalesce(line.quantity, 0))
                          + ', received ' + toString(receivedQuantity)
        }
        ELSE null
    END
"""

_NOTIFICATION_PROJECTION = f"""
n{{
    .id, .type, .done, .createdAt, .doneAt,
    forRole: r.name,
    concerns: {_CONCERNS_PROJECTION}
}}
"""


class NotificationRepository:
    """Repository for reading, ticking off and the creation paths of the notifications."""

    @staticmethod
    async def get_notifications(
        roles: list[str] | None, done: bool | None, session: AsyncSession
    ) -> list[Notification]:
        """Lists notifications, filtered by target role and status.

        `roles=None` delivers every notification unfiltered — that is `Admin`'s case
        (`has_role` lets every role check pass for `Admin`, and the list draws the same
        line: Admin sees everything, not only their own roles). `done=None` does not filter
        on the status.

        Args:
            roles (list[str] | None): The roles from the caller's token, or None for an
                unfiltered list (Admin).
            done (bool | None): Only ticked-off/open notifications, or None for both.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[Notification]: The notifications found, newest first.

        Raises:
            DatabaseError: When a record does not fit the read model.
        """
        query = f"""
        MATCH (n:Notification)-[:FOR_ROLE]->(r:Role)
        WHERE ($roles IS NULL OR r.name IN $roles)
          AND ($done IS NULL OR n.done = $done)
        {_CONCERNS_FRAGMENT}
        RETURN {_NOTIFICATION_PROJECTION} AS notification
        ORDER BY n.createdAt DESC
        """
        try:
            records = await read_many(session, query, roles=roles, done=done)
            return [
                Notification.model_validate(record["notification"]) for record in records
            ]
        except ValidationError as e:
            raise DatabaseError(
                f"Notification could not be read from the graph: {e}"
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while fetching the notifications: {e}") from e

    @staticmethod
    async def get_target_role(id: str, session: AsyncSession) -> str | None:
        """Reads the target role of a notification, for the role check before the tick-off.

        A quick read of its own beforehand rather than a check in the TOCTOU sense:
        `FOR_ROLE` never changes after creation (no endpoint moves it), so a gap between
        this read and the actual tick-off cannot make the answer stale — unlike, say, a
        stock check.

        Args:
            id (str): Business key of the notification.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            str | None: The name of the target role, or None when the id does not exist.

        Raises:
            DatabaseError: On unexpected errors during the query.
        """
        query = """
        MATCH (n:Notification {id: $id})-[:FOR_ROLE]->(r:Role)
        RETURN r.name AS targetRole
        """
        try:
            record = await read_single(session, query, id=id)
            return record["targetRole"] if record else None
        except Neo4jError as e:
            raise DatabaseError(f"Database error while reading the target role: {e}") from e

    @staticmethod
    async def set_done(id: str, session: AsyncSession) -> Notification | None:
        """Ticks a notification off.

        Sets `doneAt` afresh on every call and therefore overwrites a date already set —
        the same pattern as `ServiceEvent.completedAt`
        (`AssetServiceRepository.set_completion`): ticking off again is no error, it is an
        updated report of the same matter.

        Args:
            id (str): Business key of the notification.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            Notification | None: The updated notification, or None when the id does not
                exist.

        Raises:
            DatabaseError: On unexpected errors during the write.
        """
        query = f"""
        MATCH (n:Notification {{id: $id}})
        SET n.done = true, n.doneAt = $now
        WITH n
        MATCH (n)-[:FOR_ROLE]->(r:Role)
        {_CONCERNS_FRAGMENT}
        RETURN {_NOTIFICATION_PROJECTION} AS notification
        """
        try:
            record = await write_single(session, query, id=id, now=datetime.now(UTC))
            return Notification.model_validate(record["notification"]) if record else None
        except ValidationError as e:
            raise DatabaseError(
                f"Notification could not be read from the graph: {e}"
            ) from e
        except Neo4jError as e:
            raise DatabaseError(f"Database error while ticking off the notification: {e}") from e

    @staticmethod
    async def create_annual_service_list(year: int, session: AsyncSession) -> int:
        """Creates the service list of the given year.

        The same due-date derivation as `AssetServiceRepository.get_due_services` (shipping
        date, respectively last report, plus the service interval; wear parts only, active
        and shipped assets only) — only the time horizon differs: the whole calendar year
        instead of the rolling thirty-day window. The two endpoints deliberately answer
        different questions and therefore share only the formula, not the query.

        Idempotent through the business key `service_{componentInstanceId}_{year}`: a
        second call in the same year creates no second row — there is no scheduler, so
        creation has to be repeatable any number of times.

        Args:
            year (int): The calendar year the list is created for.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            int: Number of notifications belonging to that year (newly created and already
                present alike).

        Raises:
            DatabaseError: On unexpected errors during the write.
        """
        query = """
        MATCH (ci:ComponentInstance)-[:IS_TYPE]->(p:Product)
        WHERE ci.status = 'active'
          AND p.serviceIntervalMonths IS NOT NULL
          AND p.isWearPart = true
        MATCH (ci)<-[:HAS_COMPONENT*1..]-(a:AssetInstance)
        WHERE a.shippedOn IS NOT NULL
        OPTIONAL MATCH (event:ServiceEvent)-[:CONCERNS]->(ci)
        WITH ci, date(coalesce(event.completedAt, a.shippedOn))
                  + duration({months: p.serviceIntervalMonths}) AS replacementDueOn
        WHERE replacementDueOn >= date({year: $year, month: 1, day: 1})
          AND replacementDueOn <= date({year: $year, month: 12, day: 31})
        MATCH (r:Role {name: 'Sales'})
        MERGE (n:Notification {id: 'service_' + ci.id + '_' + toString($year)})
        ON CREATE SET n.type = 'service', n.done = false, n.createdAt = $now
        MERGE (n)-[:FOR_ROLE]->(r)
        MERGE (n)-[:CONCERNS]->(ci)
        RETURN count(DISTINCT n) AS count
        """
        try:
            record = await write_single(session, query, year=year, now=datetime.now(UTC))
            return record["count"] if record else 0
        except Neo4jError as e:
            raise DatabaseError(
                f"Database error during the annual run of the service list: {e}"
            ) from e

    @staticmethod
    async def report_unavailable(
        document_number: str, lines: list[UnavailableLine], session: AsyncSession
    ) -> list[Notification]:
        """Reports quantities of a delivery note missing at the location to `Purchasing`.

        Unlike the other two creation paths this one hangs off an action rather than a
        rule: somebody stands in front of the outgoing goods and finds that the stock does
        not suffice. Hence an endpoint of its own instead of a side effect.

        Idempotent through the business key `unavailable_{documentNumber}_{lineNumber}` — a
        second click on "report everything missing" creates no second row, it only carries
        the quantity forward. **A `done` already ticked off stays in place:** has purchasing
        dealt with the report, a repeated click must not tear it open again. Does something
        turn out to be missing again later, that comes about from a new delivery note with a
        number of its own.

        Args:
            document_number (str): Number of the delivery note whose lines are missing.
            lines (list[UnavailableLine]): The lines concerned with their missing quantity.
            session (AsyncSession): The asynchronous Neo4j database session.

        **All or nothing:** a line number that does not exist drops out of the MATCH
        silently, so the comparison of reported against found lines has to run inside the
        write transaction. Were it to run afterwards, the rows of the lines that did match
        would already be committed, and the caller would get a 404 for an operation that
        half happened.

        Args:
            document_number (str): Number of the delivery note whose lines are missing.
            lines (list[UnavailableLine]): The lines concerned with their missing quantity.
            session (AsyncSession): The asynchronous Neo4j database session.

        Returns:
            list[Notification]: The reports created, respectively carried forward.

        Raises:
            NotFoundError: When the document does not exist or a line number given does not
                belong to it. Nothing is written in that case.
            DatabaseError: On unexpected errors during the write.
        """
        query = f"""
        MATCH (r:Role {{name: 'Purchasing'}})
        UNWIND $lines AS input
        MATCH (d:Document {{number: $documentNumber}})-[:HAS_LINE]->(line:DocumentLine)
        WHERE line.lineNumber = input.lineNumber
        MERGE (n:Notification {{
            id: 'unavailable_' + $documentNumber + '_' + toString(input.lineNumber)
        }})
        ON CREATE SET n.type = 'unavailable', n.done = false, n.createdAt = $now
        SET n.quantity = input.quantity
        MERGE (n)-[:FOR_ROLE]->(r)
        MERGE (n)-[:CONCERNS]->(line)
        WITH DISTINCT n, r
        {_CONCERNS_FRAGMENT}
        RETURN {_NOTIFICATION_PROJECTION} AS notification
        """
        inputs = [
            {"lineNumber": line.lineNumber, "quantity": float(line.quantity)}
            for line in lines
        ]

        async def _report(tx):
            """Writes the reports and compares them against the input — atomically.

            The comparison stands inside the transaction function on purpose: raising here
            rolls the whole write back, so a report naming one valid and one unknown line
            leaves nothing behind.
            """
            result = await tx.run(query, {
                "documentNumber": document_number,
                "lines": inputs,
                "now": datetime.now(UTC),
            })
            records = await result.data()
            if len(records) != len(lines):
                raise NotFoundError(
                    f"{len(lines)} lines were reported for '{document_number}', "
                    f"{len(records)} were found. Document or line number does not exist."
                )
            return records

        try:
            records = await session.execute_write(_report)
        except Neo4jError as e:
            raise DatabaseError(
                f"Database error while reporting the unavailable quantities: {e}"
            ) from e

        try:
            return [
                Notification.model_validate(record["notification"]) for record in records
            ]
        except ValidationError as e:
            raise DatabaseError(
                f"The unavailable-quantity report could not be read: {e}"
            ) from e


# --- Shared creation ------------------------------------------------------------------
# At module level like `post_movement` in `inventory`: `post_goods_receipt` (sales) calls
# this inside its own write transaction — outside it the notification could be lost while
# the goods are already booked in.

# The id is a business key rather than a UUID: `shortage_<goods receipt>_<line>` makes
# every creation path MERGE-idempotent, so a repeated call cannot produce a second
# notification for the same line.
#
# The notification is addressed to a ROLE, not to a person. Whoever is on duty in
# purchasing has to see it — an employee-bound message would sit unread in the inbox of
# someone on holiday.
_SHORTAGE_QUERY = """
MATCH (r:Role {name: 'Purchasing'})
MATCH (line:DocumentLine {id: $purchaseOrderNumber + '_' + toString($lineNumber)})
MERGE (n:Notification {id: 'shortage_' + $goodsReceiptNumber + '_' + toString($lineNumber)})
ON CREATE SET n.type = 'shortage', n.done = false, n.createdAt = $now
MERGE (n)-[:FOR_ROLE]->(r)
MERGE (n)-[:CONCERNS]->(line)
"""


async def create_quantity_deviation(
    tx: AsyncManagedTransaction,
    *,
    goods_receipt_number: str,
    purchase_order_number: str,
    line_number: int,
    now: datetime,
) -> None:
    """Reports a quantity deviation of a goods receipt line to `Purchasing`.

    Runs inside the already open write transaction of
    `DocumentRepository.post_goods_receipt` — no `session.execute_write` of its own, for
    the same reason as `post_movement`: the stock booking and the notification have to
    commit together or fail together.

    Idempotent through its business key. The idempotency short circuit in
    `post_goods_receipt` (over `deliveryNoteNumber`) already keeps this function from
    running on a repeat, so a second notification for the same goods receipt never comes
    into existence.

    **Stores no quantity on the node.** The ordered/received comparison is resolved by
    `_CONCERNS_FRAGMENT` at read time, so a follow-up delivery booked afterwards is
    reflected in the display text rather than freezing the state of the first booking.

    Args:
        tx (AsyncManagedTransaction): The running Neo4j write transaction.
        goods_receipt_number (str): Number of the newly created goods receipt.
        purchase_order_number (str): Number of the purchase order whose line deviates.
        line_number (int): Line number of the deviating purchase order line.
        now (datetime): Time of creation, set server-side.
    """
    await (await tx.run(
        _SHORTAGE_QUERY,
        {
            "goodsReceiptNumber":   goods_receipt_number,
            "purchaseOrderNumber":  purchase_order_number,
            "lineNumber":           line_number,
            "now":                  now,
        },
    )).consume()
