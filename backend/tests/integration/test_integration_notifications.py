"""Integration tests of the notifications domain against a real Neo4j.

One node type, three creation paths. Checked is exactly what cannot be shown with mocks:

* the **annual run** — the due-date formula, the role assignment, idempotence through
  `MERGE` on a second call
* the **quantity deviation** — comes about exactly when `_delivery_status` returns anything
  other than `Complete`, in the same transaction as the goods receipt booking
* the **unavailable quantity** reported at the outgoing goods — all or nothing
* the **visibility per role**, and that a ticked-off notification leaves the default filter
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from core.exceptions import NotFoundError
from domains.notifications.repository_notifications import (
    NotificationRepository,
    create_quantity_deviation,
)
from domains.notifications.schemas_notifications import UnavailableLine
from domains.sales.repository_sales import DocumentRepository
from domains.sales.schemas_sales import GoodsReceiptCreate

# ==========================================
# HELPERS
# ==========================================

async def create_roles(session) -> None:
    await session.run("MERGE (:Role {name: 'Sales'}) MERGE (:Role {name: 'Purchasing'})")


async def count(session, cypher: str, **params) -> int:
    result = await session.run(cypher, **params)
    record = await result.single()
    return record[0] if record else 0


async def component_with_due_date(
    session,
    *,
    serialNumber: str,
    productNumber: str,
    serviceIntervalMonths: int,
    shippedOn: date | None,
    isWearPart: bool = True,
    status: str = "active",
) -> None:
    """Builds an asset with exactly one installed component relevant for the due date.

    `shippedOn` controls the due date directly (replacementDueOn = shippedOn +
    serviceIntervalMonths) — that way each test can produce "due this year" or "due next
    year" on purpose, independent of the actual test date.
    """
    await session.run(
        """
        MERGE (p:Product {number: $productNumber})
        SET p.label = 'Test part', p.isWearPart = $isWearPart,
            p.serviceIntervalMonths = $serviceIntervalMonths
        MERGE (a:AssetInstance {serialNumber: $serialNumber})
        SET a.shippedOn = $shippedOn
        MERGE (ci:ComponentInstance {id: $serialNumber + '_' + $productNumber})
        SET ci.status = $status
        MERGE (a)-[:HAS_COMPONENT {quantity: 1}]->(ci)
        MERGE (ci)-[:IS_TYPE]->(p)
        """,
        serialNumber=serialNumber,
        productNumber=productNumber,
        serviceIntervalMonths=serviceIntervalMonths,
        shippedOn=shippedOn,
        isWearPart=isWearPart,
        status=status,
    )


def due_this_year() -> date:
    """A shipping date that makes a twelve-month interval fall due in the current year."""
    return date(date.today().year - 1, 6, 1)


def due_next_year() -> date:
    """A shipping date that makes a twelve-month interval fall due next year."""
    return date(date.today().year, 6, 1)


async def create_purchase_order_with_line(
    session, *, number: str, productNumber: str, quantity: float
) -> None:
    """Creates a purchase order with one line, plus what a goods receipt needs around it:
    the main warehouse, the recording employee and a supplier."""
    await session.run(
        """
        MERGE (s:Supplier {id: 'S-001'}) ON CREATE SET s.name = 'Alpha Components'
        MERGE (e:Employee {id: '3'}) ON CREATE SET e.name = 'John Doe'
        MERGE (w:Location {id: '1'}) ON CREATE SET w.name = 'Central Warehouse', w.type = 'Warehouse'
        MERGE (p:Product {number: $productNumber})
          ON CREATE SET p.label = 'Vibration Sensor', p.unit = 'pcs'
        CREATE (d:Document:PurchaseOrder {number: $number, type: 'PurchaseOrder', status: 'open'})
        MERGE (d)-[:BELONGS_TO_SUPPLIER]->(s)
        CREATE (l:DocumentLine {
            id: $number + '_1', lineNumber: 1, quantity: $quantity, unitPriceCent: 500
        })
        CREATE (d)-[:HAS_LINE]->(l)
        CREATE (l)-[:OF_PRODUCT]->(p)
        """,
        number=number,
        productNumber=productNumber,
        quantity=quantity,
    )


async def receive(session, purchase_order: str, delivery_note: str, quantity: float):
    """Books a goods receipt of one line against the purchase order."""
    return await DocumentRepository.post_goods_receipt(
        purchase_order,
        GoodsReceiptCreate.model_validate(
            {"deliveryNoteNumber": delivery_note, "lines": [{"lineNumber": 1, "quantity": quantity}]}
        ),
        "3",
        session,
    )


# ==========================================
# ANNUAL RUN OF THE SERVICE LIST
# ==========================================

@pytest.mark.asyncio
async def test_the_annual_run_creates_a_notification_for_a_due_component(neo4j_session):
    await create_roles(neo4j_session)
    year = date.today().year
    await component_with_due_date(
        neo4j_session,
        serialNumber="SN-2025-001",
        productNumber="ACME-2003",
        serviceIntervalMonths=12,
        shippedOn=due_this_year(),
    )

    created = await NotificationRepository.create_annual_service_list(year, neo4j_session)

    assert created == 1
    notifications = await NotificationRepository.get_notifications(["Sales"], None, neo4j_session)
    assert len(notifications) == 1
    row = notifications[0]
    assert row.id == f"service_SN-2025-001_ACME-2003_{year}"
    assert row.type == "service"
    assert row.done is False
    assert row.forRole == "Sales"
    assert row.concerns.type == "ComponentInstance"
    assert row.concerns.id == "SN-2025-001_ACME-2003"
    assert row.concerns.productNumber == "ACME-2003"
    assert "SN-2025-001" in row.concerns.description


@pytest.mark.asyncio
async def test_the_annual_run_ignores_components_due_only_next_year(neo4j_session):
    await create_roles(neo4j_session)
    await component_with_due_date(
        neo4j_session,
        serialNumber="SN-2025-001",
        productNumber="ACME-2003",
        serviceIntervalMonths=12,
        shippedOn=due_next_year(),
    )

    created = await NotificationRepository.create_annual_service_list(
        date.today().year, neo4j_session
    )

    assert created == 0
    assert await count(neo4j_session, "MATCH (n:Notification) RETURN count(n)") == 0


@pytest.mark.asyncio
async def test_the_annual_run_ignores_parts_that_are_not_wear_parts(neo4j_session):
    # HAS_COMPONENT covers the whole bill of materials, not only wear parts — the annual run
    # has to carry the same filter rule as the service forecast, otherwise screws would
    # suddenly fall due.
    await create_roles(neo4j_session)
    await component_with_due_date(
        neo4j_session,
        serialNumber="SN-2025-001",
        productNumber="ACME-2001",
        serviceIntervalMonths=12,
        shippedOn=due_this_year(),
        isWearPart=False,
    )

    created = await NotificationRepository.create_annual_service_list(
        date.today().year, neo4j_session
    )

    assert created == 0


@pytest.mark.asyncio
async def test_the_annual_run_ignores_removed_components(neo4j_session):
    # A component that is no longer installed has nothing to be serviced.
    await create_roles(neo4j_session)
    await component_with_due_date(
        neo4j_session,
        serialNumber="SN-2025-001",
        productNumber="ACME-2003",
        serviceIntervalMonths=12,
        shippedOn=due_this_year(),
        status="removed",
    )

    created = await NotificationRepository.create_annual_service_list(
        date.today().year, neo4j_session
    )

    assert created == 0


@pytest.mark.asyncio
async def test_the_annual_run_ignores_assets_not_shipped_yet(neo4j_session):
    # The service obligation starts at the point of sale. Without a shipping date there is
    # nothing to count from — the asset drops out rather than falling back to another date.
    await create_roles(neo4j_session)
    await component_with_due_date(
        neo4j_session,
        serialNumber="SN-2025-001",
        productNumber="ACME-2003",
        serviceIntervalMonths=12,
        shippedOn=None,
    )

    created = await NotificationRepository.create_annual_service_list(
        date.today().year, neo4j_session
    )

    assert created == 0


@pytest.mark.asyncio
async def test_a_completed_service_moves_the_due_date_on(neo4j_session):
    # The ServiceEvent is the anchor of the next cycle: after a completion this year the
    # next replacement lies a full interval later — outside the current year.
    await create_roles(neo4j_session)
    year = date.today().year
    await component_with_due_date(
        neo4j_session,
        serialNumber="SN-2025-001",
        productNumber="ACME-2003",
        serviceIntervalMonths=12,
        shippedOn=due_this_year(),
    )
    await neo4j_session.run(
        """
        MATCH (ci:ComponentInstance {id: 'SN-2025-001_ACME-2003'})
        CREATE (:ServiceEvent {id: ci.id, completedAt: $completedAt})-[:CONCERNS]->(ci)
        """,
        completedAt=date(year, 1, 15),
    )

    created = await NotificationRepository.create_annual_service_list(year, neo4j_session)

    assert created == 0


@pytest.mark.asyncio
async def test_running_the_annual_run_twice_creates_no_duplicates(neo4j_session):
    await create_roles(neo4j_session)
    year = date.today().year
    await component_with_due_date(
        neo4j_session,
        serialNumber="SN-2025-001",
        productNumber="ACME-2003",
        serviceIntervalMonths=12,
        shippedOn=due_this_year(),
    )

    first_run = await NotificationRepository.create_annual_service_list(year, neo4j_session)
    second_run = await NotificationRepository.create_annual_service_list(year, neo4j_session)

    assert first_run == second_run == 1
    assert await count(neo4j_session, "MATCH (n:Notification) RETURN count(n)") == 1


@pytest.mark.asyncio
async def test_the_annual_run_leaves_a_ticked_off_notification_done(neo4j_session):
    # MERGE hits the same node on the second run and must not set the ON CREATE fields
    # again — otherwise a second annual run would tear an already ticked-off done=true back
    # to false.
    await create_roles(neo4j_session)
    year = date.today().year
    await component_with_due_date(
        neo4j_session,
        serialNumber="SN-2025-001",
        productNumber="ACME-2003",
        serviceIntervalMonths=12,
        shippedOn=due_this_year(),
    )
    await NotificationRepository.create_annual_service_list(year, neo4j_session)
    await NotificationRepository.set_done(f"service_SN-2025-001_ACME-2003_{year}", neo4j_session)

    await NotificationRepository.create_annual_service_list(year, neo4j_session)

    still_open = await NotificationRepository.get_notifications(["Sales"], False, neo4j_session)
    assert still_open == []


# ==========================================
# QUANTITY DEVIATION IN THE GOODS RECEIPT
# ==========================================

@pytest.mark.asyncio
async def test_a_partial_delivery_creates_a_deviation_for_purchasing(neo4j_session):
    await create_roles(neo4j_session)
    await create_purchase_order_with_line(
        neo4j_session, number="PO-2026-0001", productNumber="ACME-2007", quantity=25.0
    )

    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", 10)

    notifications = await NotificationRepository.get_notifications(
        ["Purchasing"], None, neo4j_session
    )
    assert len(notifications) == 1
    row = notifications[0]
    assert row.type == "shortage"
    assert row.forRole == "Purchasing"
    assert row.concerns.type == "DocumentLine"
    assert row.concerns.id == "PO-2026-0001_1"
    # The display text carries purchase order number, ordered and received quantity — without
    # an extra call against the purchase order, purchasing would otherwise have to guess
    # what it is about.
    assert "PO-2026-0001" in row.concerns.description
    assert "ordered 25" in row.concerns.description
    assert "received 10" in row.concerns.description


@pytest.mark.asyncio
async def test_a_complete_delivery_creates_no_deviation(neo4j_session):
    await create_roles(neo4j_session)
    await create_purchase_order_with_line(
        neo4j_session, number="PO-2026-0001", productNumber="ACME-2007", quantity=25.0
    )

    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", 25)

    assert await count(neo4j_session, "MATCH (n:Notification) RETURN count(n)") == 0


@pytest.mark.asyncio
async def test_a_second_partial_delivery_creates_another_deviation(neo4j_session):
    # Every goods receipt booking is a business operation of its own with a document key of
    # its own — a second partial delivery against the same purchase order line therefore
    # reports a second time, not as an update of the same row.
    await create_roles(neo4j_session)
    await create_purchase_order_with_line(
        neo4j_session, number="PO-2026-0001", productNumber="ACME-2007", quantity=25.0
    )
    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", 10)

    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-2", 10)

    assert await count(neo4j_session, "MATCH (n:Notification) RETURN count(n)") == 2


@pytest.mark.asyncio
async def test_the_deviation_shows_the_current_state_after_a_follow_up_delivery(neo4j_session):
    # The notification stores no quantity: the ordered/received comparison is resolved at
    # read time. A follow-up delivery booked afterwards therefore shows up in the text of the
    # FIRST notification too, instead of freezing the state of the first booking.
    await create_roles(neo4j_session)
    await create_purchase_order_with_line(
        neo4j_session, number="PO-2026-0001", productNumber="ACME-2007", quantity=25.0
    )
    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", 10)
    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-2", 15)

    notifications = await NotificationRepository.get_notifications(
        ["Purchasing"], None, neo4j_session
    )

    # The second delivery completed the line, so it raised no notification of its own.
    assert len(notifications) == 1
    assert "received 25" in notifications[0].concerns.description


@pytest.mark.asyncio
async def test_a_repeated_goods_receipt_reports_no_second_deviation(neo4j_session):
    # Idempotence over deliveryNoteNumber: the idempotency short circuit in
    # post_goods_receipt keeps the notification code from running at all.
    await create_roles(neo4j_session)
    await create_purchase_order_with_line(
        neo4j_session, number="PO-2026-0001", productNumber="ACME-2007", quantity=25.0
    )

    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", 10)
    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", 10)

    assert await count(neo4j_session, "MATCH (n:Notification) RETURN count(n)") == 1


@pytest.mark.asyncio
async def test_an_over_delivery_creates_a_deviation_as_well(neo4j_session):
    await create_roles(neo4j_session)
    await create_purchase_order_with_line(
        neo4j_session, number="PO-2026-0001", productNumber="ACME-2007", quantity=10.0
    )

    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", 15)

    notifications = await NotificationRepository.get_notifications(
        ["Purchasing"], None, neo4j_session
    )
    assert len(notifications) == 1


# ==========================================
# VISIBILITY AND TICKING OFF
# ==========================================

@pytest.mark.asyncio
async def test_visibility_is_limited_to_the_own_role(neo4j_session):
    await create_roles(neo4j_session)
    await create_purchase_order_with_line(
        neo4j_session, number="PO-2026-0001", productNumber="ACME-2007", quantity=25.0
    )
    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", 10)

    for_purchasing = await NotificationRepository.get_notifications(
        ["Purchasing"], None, neo4j_session
    )
    for_sales = await NotificationRepository.get_notifications(["Sales"], None, neo4j_session)
    for_admin = await NotificationRepository.get_notifications(None, None, neo4j_session)

    assert len(for_purchasing) == 1
    assert for_sales == []
    assert len(for_admin) == 1


@pytest.mark.asyncio
async def test_a_ticked_off_notification_leaves_the_default_filter(neo4j_session):
    await create_roles(neo4j_session)
    await create_purchase_order_with_line(
        neo4j_session, number="PO-2026-0001", productNumber="ACME-2007", quantity=25.0
    )
    # Called on its own, without the whole goods receipt flow: create_quantity_deviation
    # expects a running transaction, the way post_goods_receipt holds one open.
    await neo4j_session.execute_write(
        lambda tx: create_quantity_deviation(
            tx,
            goods_receipt_number="GR-2026-0001",
            purchase_order_number="PO-2026-0001",
            line_number=1,
            now=datetime.now(UTC),
        )
    )
    id_ = "shortage_GR-2026-0001_1"

    before_ticking_off = await NotificationRepository.get_notifications(
        ["Purchasing"], False, neo4j_session
    )
    assert len(before_ticking_off) == 1

    await NotificationRepository.set_done(id_, neo4j_session)

    after_ticking_off = await NotificationRepository.get_notifications(
        ["Purchasing"], False, neo4j_session
    )
    assert after_ticking_off == []
    done = await NotificationRepository.get_notifications(["Purchasing"], True, neo4j_session)
    assert len(done) == 1
    assert done[0].doneAt is not None


@pytest.mark.asyncio
async def test_ticking_off_an_unknown_notification_returns_none(neo4j_session):
    assert await NotificationRepository.set_done("service_unknown_2026", neo4j_session) is None


@pytest.mark.asyncio
async def test_the_target_role_is_read_for_the_role_check(neo4j_session):
    await create_roles(neo4j_session)
    await create_purchase_order_with_line(
        neo4j_session, number="PO-2026-0001", productNumber="ACME-2007", quantity=25.0
    )
    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", 10)

    role = await NotificationRepository.get_target_role("shortage_GR-2026-0001_1", neo4j_session)
    unknown = await NotificationRepository.get_target_role("shortage_unknown_1", neo4j_session)

    assert role == "Purchasing"
    assert unknown is None


# ==========================================
# UNAVAILABLE QUANTITY AT THE OUTGOING GOODS
# ==========================================

async def create_delivery_note_with_line(
    session, *, number: str, productNumber: str, quantity: float, lineNumber: int = 1
) -> None:
    """Creates a delivery note with one line and a customer."""
    await session.run(
        """
        MERGE (c:Customer {id: 'C-1001'}) ON CREATE SET c.name = 'Example Industries GmbH'
        MERGE (p:Product {number: $productNumber})
          ON CREATE SET p.label = 'O-Ring 10x2', p.unit = 'pcs'
        MERGE (d:Document:DeliveryNote {number: $number})
          ON CREATE SET d.type = 'DeliveryNote', d.status = 'open'
        MERGE (d)-[:BELONGS_TO_CUSTOMER]->(c)
        MERGE (l:DocumentLine {id: $number + '_' + toString($lineNumber)})
          ON CREATE SET l.lineNumber = $lineNumber, l.quantity = $quantity,
                        l.unitPriceCent = 20
        MERGE (d)-[:HAS_LINE]->(l)
        MERGE (l)-[:OF_PRODUCT]->(p)
        """,
        number=number, productNumber=productNumber, quantity=quantity,
        lineNumber=lineNumber,
    )


@pytest.mark.asyncio
async def test_an_unavailable_quantity_creates_a_notification_for_purchasing(neo4j_session):
    await create_roles(neo4j_session)
    await create_delivery_note_with_line(
        neo4j_session, number="DN-2026-0001", productNumber="ACME-2002", quantity=10.0
    )

    result = await NotificationRepository.report_unavailable(
        "DN-2026-0001",
        [UnavailableLine(lineNumber=1, quantity=Decimal("3"))],
        neo4j_session,
    )

    assert len(result) == 1
    notification = result[0]
    assert notification.id == "unavailable_DN-2026-0001_1"
    assert notification.type == "unavailable"
    assert notification.forRole == "Purchasing"
    assert notification.done is False
    # Product number and quantity stand as fields of their own — purchasing reorders from
    # them without having to take the display text apart.
    assert notification.concerns.productNumber == "ACME-2002"
    assert notification.concerns.quantity == Decimal("3")
    assert "3.0 pcs missing" in notification.concerns.description
    assert "Example Industries GmbH" in notification.concerns.description


@pytest.mark.asyncio
async def test_reporting_twice_creates_no_second_notification(neo4j_session):
    await create_roles(neo4j_session)
    await create_delivery_note_with_line(
        neo4j_session, number="DN-2026-0001", productNumber="ACME-2002", quantity=10.0
    )

    await NotificationRepository.report_unavailable(
        "DN-2026-0001", [UnavailableLine(lineNumber=1, quantity=Decimal("3"))], neo4j_session,
    )
    second = await NotificationRepository.report_unavailable(
        "DN-2026-0001", [UnavailableLine(lineNumber=1, quantity=Decimal("5"))], neo4j_session,
    )

    assert await count(
        neo4j_session, "MATCH (n:Notification {type: 'unavailable'}) RETURN count(n)"
    ) == 1
    # The quantity is carried forward — the newer statement applies.
    assert second[0].concerns.quantity == Decimal("5")


@pytest.mark.asyncio
async def test_reporting_again_does_not_reopen_a_ticked_off_notification(neo4j_session):
    # A deliberate decision: once purchasing has dealt with the report, a second click in
    # the warehouse must not tear it open again.
    await create_roles(neo4j_session)
    await create_delivery_note_with_line(
        neo4j_session, number="DN-2026-0001", productNumber="ACME-2002", quantity=10.0
    )
    await NotificationRepository.report_unavailable(
        "DN-2026-0001", [UnavailableLine(lineNumber=1, quantity=Decimal("3"))], neo4j_session,
    )
    await NotificationRepository.set_done("unavailable_DN-2026-0001_1", neo4j_session)

    again = await NotificationRepository.report_unavailable(
        "DN-2026-0001", [UnavailableLine(lineNumber=1, quantity=Decimal("4"))], neo4j_session,
    )

    assert again[0].done is True


@pytest.mark.asyncio
async def test_several_lines_result_in_several_notifications(neo4j_session):
    await create_roles(neo4j_session)
    await create_delivery_note_with_line(
        neo4j_session, number="DN-2026-0001", productNumber="ACME-2002", quantity=10.0,
        lineNumber=1,
    )
    await create_delivery_note_with_line(
        neo4j_session, number="DN-2026-0001", productNumber="ACME-2003", quantity=4.0,
        lineNumber=2,
    )

    result = await NotificationRepository.report_unavailable(
        "DN-2026-0001",
        [
            UnavailableLine(lineNumber=1, quantity=Decimal("3")),
            UnavailableLine(lineNumber=2, quantity=Decimal("1")),
        ],
        neo4j_session,
    )

    assert sorted(n.id for n in result) == [
        "unavailable_DN-2026-0001_1", "unavailable_DN-2026-0001_2",
    ]


@pytest.mark.asyncio
async def test_an_unknown_line_number_is_an_error(neo4j_session):
    # Without this check the row would drop out of the MATCH silently, and the endpoint
    # would report success for something it never created.
    await create_roles(neo4j_session)
    await create_delivery_note_with_line(
        neo4j_session, number="DN-2026-0001", productNumber="ACME-2002", quantity=10.0
    )

    with pytest.raises(NotFoundError):
        await NotificationRepository.report_unavailable(
            "DN-2026-0001",
            [UnavailableLine(lineNumber=99, quantity=Decimal("1"))],
            neo4j_session,
        )


@pytest.mark.asyncio
async def test_one_unknown_line_rolls_back_the_valid_ones_too(neo4j_session):
    # All or nothing: the comparison of reported against found lines runs inside the write
    # transaction. Were it to run afterwards, the valid line would already be committed and
    # the caller would get a 404 for an operation that half happened.
    await create_roles(neo4j_session)
    await create_delivery_note_with_line(
        neo4j_session, number="DN-2026-0001", productNumber="ACME-2002", quantity=10.0
    )

    with pytest.raises(NotFoundError):
        await NotificationRepository.report_unavailable(
            "DN-2026-0001",
            [
                UnavailableLine(lineNumber=1, quantity=Decimal("3")),
                UnavailableLine(lineNumber=99, quantity=Decimal("1")),
            ],
            neo4j_session,
        )

    assert await count(neo4j_session, "MATCH (n:Notification) RETURN count(n)") == 0


@pytest.mark.asyncio
async def test_an_unavailable_quantity_appears_for_purchasing_only(neo4j_session):
    # The same line as with the other two cases: a notification is visible to its target
    # role only.
    await create_roles(neo4j_session)
    await create_delivery_note_with_line(
        neo4j_session, number="DN-2026-0001", productNumber="ACME-2002", quantity=10.0
    )
    await NotificationRepository.report_unavailable(
        "DN-2026-0001", [UnavailableLine(lineNumber=1, quantity=Decimal("3"))], neo4j_session,
    )

    for_purchasing = await NotificationRepository.get_notifications(
        ["Purchasing"], None, neo4j_session
    )
    for_sales = await NotificationRepository.get_notifications(["Sales"], None, neo4j_session)

    assert [n.id for n in for_purchasing] == ["unavailable_DN-2026-0001_1"]
    assert for_sales == []
