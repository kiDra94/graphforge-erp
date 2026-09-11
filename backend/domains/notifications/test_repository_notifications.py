"""Unit tests of the notifications repository — without a database.

Three things no other test level covers:

* **The parameter binding.** The query helpers are intercepted and the generated Cypher
  inspected along with its parameters. What is checked
  is what gets bound — the wording only where it carries a rule, and those places are
  marked.
* **The business keys.** All three creation paths are idempotent through their id and
  nowhere else. That the id is assembled from exactly the parts that make it unique is
  therefore checked on the query text.
* **The comparison of reported against found lines.** A line that does not exist drops out
  of the MATCH silently; without the comparison the endpoint would report success for
  something it never created. That the comparison runs INSIDE the transaction function —
  and a half-written report is therefore rolled back — is checked as well.

Not here: whether the CASE in the projection picks the right branch, and whether the
annual run really finds the components falling due. Both need a real database.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from neo4j.exceptions import Neo4jError

from core.exceptions import DatabaseError, NotFoundError
from domains.notifications.repository_notifications import (
    NotificationRepository,
    create_quantity_deviation,
)
from domains.notifications.schemas_notifications import UnavailableLine

REPOSITORY_MODULE = "domains.notifications.repository_notifications"


def _raw_notification(**overrides) -> dict:
    """Builds the projected map of one notification as Cypher returns it."""
    data: dict = {
        "id": "shortage_GR-2026-0001_2",
        "type": "shortage",
        "done": False,
        "forRole": "Purchasing",
        "concerns": {
            "type": "DocumentLine",
            "id": "PO-2026-0001_2",
            "description": "Purchase order PO-2026-0001, line 2: Sealing Ring 40mm",
            "productNumber": "ACME-2001",
            "quantity": None,
        },
        "createdAt": datetime(2026, 8, 28, 9, 15, tzinfo=UTC),
        "doneAt": None,
    }
    data.update(overrides)
    return data


@pytest.fixture
def captured_read(monkeypatch):
    """Intercepts the next read_many/read_single query instead of running it."""
    record: dict = {"query": "", "params": {}, "records": [], "record": None}

    async def fake_read_many(session, query, **params):
        record["query"] = " ".join(query.split())
        record["params"] = params
        return record["records"]

    async def fake_read_single(session, query, **params):
        record["query"] = " ".join(query.split())
        record["params"] = params
        return record["record"]

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_many", fake_read_many)
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_single", fake_read_single)
    return record


@pytest.fixture
def captured_write(monkeypatch):
    """Intercepts the next write instead of running it.

    Covers both shapes this repository uses: the `write_single` helper, and the transaction
    function `report_unavailable` hands to `session.execute_write`. For the latter the
    stand-in session actually calls the function — the comparison of reported against found
    lines lives inside it, and that is exactly what the tests are after. `record["session"]`
    is the session to pass in for those.
    """
    record: dict = {"query": "", "params": {}, "record": None, "records": []}

    async def fake_write_single(session, query, **params):
        record["query"] = " ".join(query.split())
        record["params"] = params
        return record["record"]

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.write_single", fake_write_single)

    class Result:
        async def data(self):
            return record["records"]

    class Tx:
        async def run(self, query, params):
            record["query"] = " ".join(query.split())
            record["params"] = params
            return Result()

    class Session:
        async def execute_write(self, transaction_function, *args):
            return await transaction_function(Tx(), *args)

    record["session"] = Session()
    return record


# ==========================================
# Reading
# ==========================================

@pytest.mark.asyncio
async def test_the_list_binds_roles_and_status(captured_read):
    await NotificationRepository.get_notifications(["Purchasing"], False, AsyncMock())

    assert captured_read["params"] == {"roles": ["Purchasing"], "done": False}


@pytest.mark.asyncio
async def test_the_list_binds_none_for_admin(captured_read):
    """`Admin` sees everything, not only their own roles — the same line `has_role` draws.
    `None` is the value the WHERE reads as "do not filter"."""
    await NotificationRepository.get_notifications(None, None, AsyncMock())

    assert captured_read["params"] == {"roles": None, "done": None}


@pytest.mark.asyncio
async def test_the_filters_are_bound_and_not_inserted(captured_read):
    """Role names come out of a token. Were they inserted into the query text, a crafted
    claim would change the query.

    This test checks a piece of wording, because the wording is the rule here.
    """
    await NotificationRepository.get_notifications(["Sales"], True, AsyncMock())

    assert "$roles" in captured_read["query"]
    assert "Sales" not in captured_read["query"]


@pytest.mark.asyncio
async def test_the_list_converts_the_record(captured_read):
    captured_read["records"] = [{"notification": _raw_notification()}]

    notifications = await NotificationRepository.get_notifications(None, None, AsyncMock())

    assert notifications[0].id == "shortage_GR-2026-0001_2"
    assert notifications[0].concerns.type == "DocumentLine"


@pytest.mark.asyncio
async def test_the_list_sorts_newest_first(captured_read):
    """Without an ORDER BY the order would not be defined, and a notification list whose
    order changes on every reload is unusable.

    This test checks a piece of wording, because the wording is the rule here.
    """
    await NotificationRepository.get_notifications(None, None, AsyncMock())

    assert "ORDER BY n.createdAt DESC" in captured_read["query"]


@pytest.mark.asyncio
async def test_a_broken_record_becomes_a_database_error(captured_read):
    """A row the read model cannot map is a data problem of the server: 500, not 422."""
    captured_read["records"] = [{"notification": _raw_notification(type="unknown")}]

    with pytest.raises(DatabaseError):
        await NotificationRepository.get_notifications(None, None, AsyncMock())


@pytest.mark.asyncio
async def test_a_neo4j_error_becomes_a_database_error(monkeypatch):
    async def failing_read_many(session, query, **params):
        raise Neo4jError("connection lost")

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_many", failing_read_many)

    with pytest.raises(DatabaseError):
        await NotificationRepository.get_notifications(None, None, AsyncMock())


@pytest.mark.asyncio
async def test_the_target_role_delivers_the_role_name(captured_read):
    captured_read["record"] = {"targetRole": "Purchasing"}

    assert await NotificationRepository.get_target_role("x", AsyncMock()) == "Purchasing"


@pytest.mark.asyncio
async def test_the_target_role_of_an_unknown_id_is_none(captured_read):
    """Translating that into a 404 is the service's business."""
    captured_read["record"] = None

    assert await NotificationRepository.get_target_role("x", AsyncMock()) is None


# ==========================================
# Ticking off
# ==========================================

@pytest.mark.asyncio
async def test_ticking_off_binds_id_and_timestamp(captured_write):
    """`doneAt` comes from the server, not from the request — a client must not be able to
    claim when something was dealt with."""
    captured_write["record"] = {"notification": _raw_notification(done=True)}

    await NotificationRepository.set_done("shortage_GR-2026-0001_2", AsyncMock())

    assert captured_write["params"]["id"] == "shortage_GR-2026-0001_2"
    assert isinstance(captured_write["params"]["now"], datetime)


@pytest.mark.asyncio
async def test_ticking_off_an_unknown_id_delivers_none(captured_write):
    captured_write["record"] = None

    assert await NotificationRepository.set_done("nope", AsyncMock()) is None


@pytest.mark.asyncio
async def test_ticking_off_converts_the_record(captured_write):
    captured_write["record"] = {
        "notification": _raw_notification(
            done=True, doneAt=datetime(2026, 8, 29, 7, 0, tzinfo=UTC)
        )
    }

    notification = await NotificationRepository.set_done("x", AsyncMock())

    assert notification is not None
    assert notification.done is True
    assert notification.doneAt is not None


@pytest.mark.asyncio
async def test_ticking_off_again_overwrites_the_date(captured_write):
    """The same pattern as `ServiceEvent.completedAt`: ticking off again is no error, it is
    an updated report of the same matter. A `SET ... ON MATCH` would keep the first date.

    This test checks a piece of wording, because the wording is the rule here.
    """
    captured_write["record"] = {"notification": _raw_notification(done=True)}

    await NotificationRepository.set_done("x", AsyncMock())

    assert "SET n.done = true, n.doneAt = $now" in captured_write["query"]


# ==========================================
# Annual run
# ==========================================

@pytest.mark.asyncio
async def test_the_annual_run_binds_the_year(captured_write):
    captured_write["record"] = {"count": 3}

    count = await NotificationRepository.create_annual_service_list(2026, AsyncMock())

    assert captured_write["params"]["year"] == 2026
    assert count == 3


@pytest.mark.asyncio
async def test_the_annual_run_without_a_result_delivers_zero(captured_write):
    """No component falling due is a valid outcome, not an error."""
    captured_write["record"] = None

    assert await NotificationRepository.create_annual_service_list(2026, AsyncMock()) == 0


@pytest.mark.asyncio
async def test_the_annual_run_uses_the_year_inside_the_business_key(captured_write):
    """Without the year in the key the run of the following year would find the row of the
    previous one and create nothing.

    This test checks a piece of wording, because the wording is the rule here.
    """
    captured_write["record"] = {"count": 0}

    await NotificationRepository.create_annual_service_list(2026, AsyncMock())

    assert "'service_' + ci.id + '_' + toString($year)" in captured_write["query"]


@pytest.mark.asyncio
async def test_the_annual_run_creates_through_merge(captured_write):
    """There is no scheduler, so creation has to be repeatable any number of times. A
    CREATE would fail on the constraint the second time round.

    This test checks a piece of wording, because the wording is the rule here.
    """
    captured_write["record"] = {"count": 0}

    await NotificationRepository.create_annual_service_list(2026, AsyncMock())

    assert "MERGE (n:Notification" in captured_write["query"]
    assert "ON CREATE SET n.type = 'service'" in captured_write["query"]


@pytest.mark.asyncio
async def test_the_annual_run_filters_on_wear_parts(captured_write):
    """The digital twin holds every component of a bill of materials. Without the filter,
    screws would be reported as due for service as soon as they happened to carry an
    interval.

    This test checks a piece of wording, because the wording is the rule here.
    """
    captured_write["record"] = {"count": 0}

    await NotificationRepository.create_annual_service_list(2026, AsyncMock())

    assert "p.isWearPart = true" in captured_write["query"]
    assert "a.shippedOn IS NOT NULL" in captured_write["query"]


# ==========================================
# Unavailable quantities
# ==========================================

@pytest.mark.asyncio
async def test_the_report_binds_document_and_lines(captured_write):
    captured_write["records"] = [{"notification": _raw_notification(type="unavailable")}]

    await NotificationRepository.report_unavailable(
        "DN-2026-0001", [UnavailableLine(lineNumber=1, quantity=Decimal("2"))],
        captured_write["session"],
    )

    assert captured_write["params"]["documentNumber"] == "DN-2026-0001"
    assert captured_write["params"]["lines"] == [{"lineNumber": 1, "quantity": 2.0}]


@pytest.mark.asyncio
async def test_the_report_stores_the_quantity_on_the_node(captured_write):
    """Unlike the ordered/received comparison of a shortage, the missing quantity is not a
    derivable figure but a person's statement at a point in time.

    This test checks a piece of wording, because the wording is the rule here.
    """
    captured_write["records"] = [{"notification": _raw_notification(type="unavailable")}]

    await NotificationRepository.report_unavailable(
        "DN-2026-0001", [UnavailableLine(lineNumber=1, quantity=Decimal("2"))],
        captured_write["session"],
    )

    assert "SET n.quantity = input.quantity" in captured_write["query"]


@pytest.mark.asyncio
async def test_reporting_again_does_not_reopen_a_ticked_off_row(captured_write):
    """`done` is set under ON CREATE and not under SET: has purchasing dealt with the
    report, a repeated click must not tear it open again.

    This test checks a piece of wording, because the wording is the rule here.
    """
    captured_write["records"] = [{"notification": _raw_notification(type="unavailable")}]

    await NotificationRepository.report_unavailable(
        "DN-2026-0001", [UnavailableLine(lineNumber=1, quantity=Decimal("2"))],
        captured_write["session"],
    )

    assert "ON CREATE SET n.type = 'unavailable', n.done = false" in captured_write["query"]
    assert "SET n.done" not in captured_write["query"].replace(
        "ON CREATE SET n.type = 'unavailable', n.done = false", ""
    )


@pytest.mark.asyncio
async def test_a_line_that_was_not_found_becomes_a_not_found_error(captured_write):
    """A line that does not exist drops out of the MATCH silently. Without the comparison
    the endpoint would report success for something it never created."""
    captured_write["records"] = [{"notification": _raw_notification(type="unavailable")}]

    with pytest.raises(NotFoundError, match="DN-2026-0001"):
        await NotificationRepository.report_unavailable(
            "DN-2026-0001",
            [
                UnavailableLine(lineNumber=1, quantity=Decimal("2")),
                UnavailableLine(lineNumber=9, quantity=Decimal("1")),
            ],
            captured_write["session"],
        )


@pytest.mark.asyncio
async def test_an_unknown_line_aborts_before_the_transaction_ends(captured_write):
    """The comparison has to raise from inside the transaction function, so the rows of the
    lines that did match are rolled back with it. Ran it afterwards, a report naming one
    valid and one unknown line would leave the valid one behind and answer 404."""
    captured_write["records"] = [{"notification": _raw_notification(type="unavailable")}]
    completed: list[bool] = []

    class Result:
        async def data(self):
            return captured_write["records"]

    class Tx:
        async def run(self, query, params):
            return Result()

    async def execute_write(transaction_function, *args):
        result = await transaction_function(Tx(), *args)
        completed.append(True)
        return result

    session = AsyncMock()
    session.execute_write = execute_write

    with pytest.raises(NotFoundError):
        await NotificationRepository.report_unavailable(
            "DN-2026-0001",
            [
                UnavailableLine(lineNumber=1, quantity=Decimal("2")),
                UnavailableLine(lineNumber=9, quantity=Decimal("1")),
            ],
            session,
        )

    assert completed == []


@pytest.mark.asyncio
async def test_an_unknown_document_becomes_a_not_found_error(captured_write):
    captured_write["records"] = []

    with pytest.raises(NotFoundError):
        await NotificationRepository.report_unavailable(
            "DN-9999", [UnavailableLine(lineNumber=1, quantity=Decimal("2"))],
            captured_write["session"],
        )


@pytest.mark.asyncio
async def test_the_report_converts_every_record(captured_write):
    captured_write["records"] = [
        {"notification": _raw_notification(id="unavailable_DN-2026-0001_1", type="unavailable")},
        {"notification": _raw_notification(id="unavailable_DN-2026-0001_2", type="unavailable")},
    ]

    result = await NotificationRepository.report_unavailable(
        "DN-2026-0001",
        [
            UnavailableLine(lineNumber=1, quantity=Decimal("2")),
            UnavailableLine(lineNumber=2, quantity=Decimal("1")),
        ],
        captured_write["session"],
    )

    assert [n.id for n in result] == [
        "unavailable_DN-2026-0001_1", "unavailable_DN-2026-0001_2"
    ]


# ==========================================
# Shared creation out of the goods receipt
# ==========================================

@pytest.mark.asyncio
async def test_the_quantity_deviation_binds_its_business_key():
    """The goods receipt number carries the key, not the purchase order number: a second
    delivery against the same purchase order is a case of its own and needs a row of its
    own."""
    captured: dict = {}

    class Result:
        async def consume(self):
            return None

    class Tx:
        async def run(self, query, params):
            captured["query"] = " ".join(query.split())
            captured["params"] = params
            return Result()

    await create_quantity_deviation(
        Tx(),  # type: ignore[arg-type]
        goods_receipt_number="GR-2026-0001",
        purchase_order_number="PO-2026-0001",
        line_number=2,
        now=datetime(2026, 8, 28, 9, 15, tzinfo=UTC),
    )

    assert captured["params"]["goodsReceiptNumber"] == "GR-2026-0001"
    assert captured["params"]["purchaseOrderNumber"] == "PO-2026-0001"
    assert captured["params"]["lineNumber"] == 2
    assert "'shortage_' + $goodsReceiptNumber + '_' + toString($lineNumber)" in captured["query"]


@pytest.mark.asyncio
async def test_the_quantity_deviation_stores_no_quantity():
    """The ordered/received comparison is resolved at read time, so a follow-up delivery
    booked afterwards is reflected in the display text instead of freezing the state of the
    first booking."""
    captured: dict = {}

    class Result:
        async def consume(self):
            return None

    class Tx:
        async def run(self, query, params):
            captured["query"] = " ".join(query.split())
            return Result()

    await create_quantity_deviation(
        Tx(),  # type: ignore[arg-type]
        goods_receipt_number="GR-2026-0001",
        purchase_order_number="PO-2026-0001",
        line_number=2,
        now=datetime(2026, 8, 28, 9, 15, tzinfo=UTC),
    )

    assert "quantity" not in captured["query"]
