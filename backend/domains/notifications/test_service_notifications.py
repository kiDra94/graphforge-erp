"""Unit tests of the notification service — against mocks, without a database.

Tested is exactly what the service decides itself:

* **The annual run out of a reading call.** There is no scheduler; `get_notifications`
  triggers the creation of the current year before it reads. That is the one deliberate
  side effect of this domain and therefore the test this module exists for.
* the translation of a `None` from the repository into a `NotFoundError`
* which event goes out, and that none goes out when nothing came about

Not here: the role filter itself. It is a WHERE clause of the query, and which roles reach
the service is decided by the router out of the token.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from core.exceptions import DatabaseError, NotFoundError
from domains.notifications.schemas_notifications import (
    Notification,
    UnavailableLine,
    UnavailableReport,
)
from domains.notifications.service_notifications import NotificationService

# Base path for every mock: patch where the name is ACTUALLY used (service_notifications.py
# imports the repository via "from .repository_notifications import ..." -> the name
# therefore lives in the service_notifications module).
SERVICE_MODULE = "domains.notifications.service_notifications"
MANAGER_MODULE = "core.websocket"


def _notification(**overrides) -> Notification:
    data: dict = {
        "id": "shortage_GR-2026-0001_2",
        "type": "shortage",
        "done": False,
        "forRole": "Purchasing",
        "concerns": {
            "type": "DocumentLine",
            "id": "PO-2026-0001_2",
            "description": "Purchase order PO-2026-0001, line 2: Sealing Ring 40mm",
        },
        "createdAt": datetime(2026, 8, 28, 9, 15, tzinfo=UTC),
    }
    data.update(overrides)
    return Notification.model_validate(data)


def _report(**overrides) -> UnavailableReport:
    data: dict = {
        "documentNumber": "DN-2026-0001",
        "lines": [UnavailableLine(lineNumber=1, quantity=Decimal("2"))],
    }
    data.update(overrides)
    return UnavailableReport(**data)


@pytest.fixture
def sent_events(monkeypatch):
    """Collects every event instead of pushing it into a websocket."""
    events: list[dict] = []

    async def fake_send_event(event: dict) -> None:
        events.append(event)

    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", fake_send_event)
    return events


@pytest.fixture
def repository(monkeypatch):
    """Replaces the whole repository with mocks and hands them back by name."""
    mocks = {
        "create_annual_service_list": AsyncMock(return_value=0),
        "get_notifications": AsyncMock(return_value=[]),
        "get_target_role": AsyncMock(return_value="Purchasing"),
        "set_done": AsyncMock(return_value=_notification(done=True)),
        "report_unavailable": AsyncMock(return_value=[_notification(type="unavailable")]),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{SERVICE_MODULE}.NotificationRepository.{name}", mock)
    return mocks


# ==========================================
# The annual run out of a reading call
# ==========================================

@pytest.mark.asyncio
async def test_the_list_triggers_the_annual_run_of_the_current_year(repository):
    """No scheduler in the backend: the first request in the new year creates the rows by
    itself, every further call is a no-op thanks to MERGE."""
    session = AsyncMock()

    await NotificationService.get_notifications(["Purchasing"], None, session)

    repository["create_annual_service_list"].assert_awaited_once_with(
        datetime.now(UTC).year, session
    )


@pytest.mark.asyncio
async def test_the_annual_run_happens_before_the_read(repository):
    """The other way round the rows created would only appear on the next request — and the
    year would look empty exactly once."""
    order: list[str] = []
    repository["create_annual_service_list"].side_effect = lambda *a: order.append("run")
    repository["get_notifications"].side_effect = lambda *a: order.append("read") or []

    await NotificationService.get_notifications(None, None, AsyncMock())

    assert order == ["run", "read"]


@pytest.mark.asyncio
async def test_the_list_passes_filters_and_session_through(repository):
    session = AsyncMock()

    await NotificationService.get_notifications(["Sales"], True, session)

    repository["get_notifications"].assert_awaited_once_with(["Sales"], True, session)


@pytest.mark.asyncio
async def test_the_list_delivers_the_repository_rows(repository):
    repository["get_notifications"].return_value = [_notification()]

    result = await NotificationService.get_notifications(None, None, AsyncMock())

    assert [n.id for n in result] == ["shortage_GR-2026-0001_2"]


@pytest.mark.asyncio
async def test_the_list_does_not_swallow_a_database_error(repository):
    repository["get_notifications"].side_effect = DatabaseError("query failed")

    with pytest.raises(DatabaseError):
        await NotificationService.get_notifications(None, None, AsyncMock())


# ==========================================
# Target role and ticking off
# ==========================================

@pytest.mark.asyncio
async def test_the_target_role_is_passed_through(repository):
    assert await NotificationService.get_target_role("x", AsyncMock()) == "Purchasing"


@pytest.mark.asyncio
async def test_an_unknown_id_becomes_a_not_found_error_on_the_role(repository):
    """The router asks for the role before it checks the permission — a 404 for an id that
    does not exist has to come out of that read, not out of the tick-off afterwards."""
    repository["get_target_role"].return_value = None

    with pytest.raises(NotFoundError, match="nope"):
        await NotificationService.get_target_role("nope", AsyncMock())


@pytest.mark.asyncio
async def test_an_unknown_id_becomes_a_not_found_error_on_the_tick_off(
    repository, sent_events
):
    repository["set_done"].return_value = None

    with pytest.raises(NotFoundError, match="nope"):
        await NotificationService.set_done("nope", AsyncMock())


@pytest.mark.asyncio
async def test_ticking_off_sends_an_event(repository, sent_events):
    await NotificationService.set_done("shortage_GR-2026-0001_2", AsyncMock())

    assert sent_events[0]["entity"] == "notification"
    assert sent_events[0]["trigger"] == "ticked_off"
    assert sent_events[0]["ids"] == ["shortage_GR-2026-0001_2"]


@pytest.mark.asyncio
async def test_ticking_off_sends_no_event_for_an_unknown_id(repository, sent_events):
    repository["set_done"].return_value = None

    with pytest.raises(NotFoundError):
        await NotificationService.set_done("nope", AsyncMock())

    assert sent_events == []


# ==========================================
# Unavailable quantities
# ==========================================

@pytest.mark.asyncio
async def test_the_report_passes_document_and_lines_through(repository, sent_events):
    session = AsyncMock()
    report = _report()

    await NotificationService.report_unavailable(report, session)

    repository["report_unavailable"].assert_awaited_once_with(
        "DN-2026-0001", report.lines, session
    )


@pytest.mark.asyncio
async def test_the_report_sends_a_created_event(repository, sent_events):
    """A trigger of its own, because for purchasing a new report is unknown by definition —
    a client that only reloads keys it already knows would otherwise not reload at all."""
    repository["report_unavailable"].return_value = [
        _notification(id="unavailable_DN-2026-0001_1", type="unavailable")
    ]

    await NotificationService.report_unavailable(_report(), AsyncMock())

    assert sent_events[0]["trigger"] == "unavailable_created"
    assert sent_events[0]["reference"] == "DN-2026-0001"
    assert sent_events[0]["ids"] == ["unavailable_DN-2026-0001_1"]


@pytest.mark.asyncio
async def test_the_report_sends_no_event_for_an_unknown_line(repository, sent_events):
    repository["report_unavailable"].side_effect = NotFoundError("line missing")

    with pytest.raises(NotFoundError):
        await NotificationService.report_unavailable(_report(), AsyncMock())

    assert sent_events == []


# ==========================================
# Annual run as an endpoint
# ==========================================

@pytest.mark.asyncio
async def test_the_manual_annual_run_passes_the_year_through(repository, sent_events):
    session = AsyncMock()
    repository["create_annual_service_list"].return_value = 4

    count = await NotificationService.create_annual_service_list(2025, session)

    repository["create_annual_service_list"].assert_awaited_once_with(2025, session)
    assert count == 4


@pytest.mark.asyncio
async def test_the_annual_run_sends_an_event_with_the_year(repository, sent_events):
    repository["create_annual_service_list"].return_value = 4

    await NotificationService.create_annual_service_list(2026, AsyncMock())

    assert sent_events[0]["trigger"] == "annual_run"
    assert sent_events[0]["reference"] == "2026"
    assert sent_events[0]["scope"] == "many"


@pytest.mark.asyncio
async def test_the_annual_run_sends_no_event_without_rows(repository, sent_events):
    """The run is idempotent and executes on every list request. An event on every one of
    them would make every open client reload for nothing."""
    repository["create_annual_service_list"].return_value = 0

    await NotificationService.create_annual_service_list(2026, AsyncMock())

    assert sent_events == []


@pytest.mark.asyncio
async def test_the_automatic_annual_run_sends_no_event(repository, sent_events):
    """The list request runs the creation through the repository directly, past the service
    method that would send the event — a `GET` must not push a notification to everybody."""
    repository["create_annual_service_list"].return_value = 4

    await NotificationService.get_notifications(None, None, AsyncMock())

    assert sent_events == []
