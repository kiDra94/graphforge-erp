"""API tests of the notification routes.

Two decisions of this domain live in the router and nowhere else:

* **The role filter comes out of the token.** `Admin` gets `None` and therefore sees
  everything; everybody else sees only the notifications addressed to a role they hold.
* **Who may tick off is decided per row**, not per endpoint: the permission is the target
  role of that one notification, which the router has to read first.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from core.exceptions import NotFoundError
from domains.notifications.schemas_notifications import Notification

SERVICE = "domains.notifications.router_notifications.NotificationService"


def _notification(**overrides) -> Notification:
    data: dict = {
        "id": "shortage_GR-2026-0001_2",
        "type": "shortage",
        "done": False,
        "forRole": "Purchasing",
        "concerns": {
            "type": "DocumentLine",
            "id": "PO-2026-0001_2",
            "description": "Purchase order PO-2026-0001, line 2: Screw M6x20",
        },
        "createdAt": datetime(2026, 8, 28, 9, 15, tzinfo=UTC),
    }
    data.update(overrides)
    return Notification.model_validate(data)


# ==========================================
# The role filter out of the token
# ==========================================

@pytest.mark.asyncio
async def test_the_roles_from_the_token_become_the_filter(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE}.get_notifications", service)

    await async_client.get("/api/notifications", headers=auth("Purchasing", "User"))

    assert service.await_args is not None
    assert service.await_args.args[0] == ["Purchasing", "User"]


@pytest.mark.asyncio
async def test_admin_gets_no_filter_at_all(async_client, auth, monkeypatch):
    """`None` is what the query reads as "do not filter" — Admin sees everything, not only
    the notifications of its own roles. The same line `has_role` draws."""
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE}.get_notifications", service)

    await async_client.get("/api/notifications", headers=auth("Admin"))

    assert service.await_args is not None
    assert service.await_args.args[0] is None


@pytest.mark.asyncio
async def test_a_caller_without_a_role_gets_an_empty_filter(async_client, auth, monkeypatch):
    """An empty list is not the same as `None`: it filters on "no role" and therefore
    matches nothing — which is the right answer for somebody who holds no role."""
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE}.get_notifications", service)

    await async_client.get("/api/notifications", headers=auth())

    assert service.await_args is not None
    assert service.await_args.args[0] == []


@pytest.mark.asyncio
async def test_the_done_filter_is_passed_on(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{SERVICE}.get_notifications", service)

    await async_client.get("/api/notifications?done=false", headers=auth("Purchasing"))

    assert service.await_args is not None
    assert service.await_args.args[1] is False


@pytest.mark.asyncio
async def test_the_list_needs_a_token(async_client):
    response = await async_client.get("/api/notifications")

    assert response.status_code == 401


# ==========================================
# Ticking off, per row
# ==========================================

@pytest.mark.asyncio
async def test_the_target_role_of_the_row_decides(async_client, auth, monkeypatch):
    """Not a fixed role for the endpoint: whoever holds the target role of this one
    notification may tick it off."""
    monkeypatch.setattr(f"{SERVICE}.get_target_role", AsyncMock(return_value="Purchasing"))
    monkeypatch.setattr(f"{SERVICE}.set_done", AsyncMock(return_value=_notification(done=True)))

    allowed = await async_client.put(
        "/api/notifications/shortage_GR-2026-0001_2/done", headers=auth("Purchasing")
    )
    refused = await async_client.put(
        "/api/notifications/shortage_GR-2026-0001_2/done", headers=auth("Sales")
    )

    assert allowed.status_code == 200
    assert refused.status_code == 403
    assert "Purchasing" in refused.json()["message"]


@pytest.mark.asyncio
async def test_a_service_notification_needs_the_sales_role(async_client, auth, monkeypatch):
    """Same endpoint, different row, different role — which is the whole point of reading the
    target role first."""
    monkeypatch.setattr(f"{SERVICE}.get_target_role", AsyncMock(return_value="Sales"))
    monkeypatch.setattr(
        f"{SERVICE}.set_done",
        AsyncMock(return_value=_notification(type="service", forRole="Sales", done=True)),
    )

    response = await async_client.put(
        "/api/notifications/service_SN-2026-0001_ACME-2003_2026/done", headers=auth("Sales")
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_admin_may_tick_off_anything(async_client, auth, monkeypatch):
    monkeypatch.setattr(f"{SERVICE}.get_target_role", AsyncMock(return_value="Purchasing"))
    monkeypatch.setattr(f"{SERVICE}.set_done", AsyncMock(return_value=_notification(done=True)))

    response = await async_client.put(
        "/api/notifications/shortage_GR-2026-0001_2/done", headers=auth("Admin")
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_an_unknown_id_answers_404_before_the_role_check(async_client, auth, monkeypatch):
    """The 404 comes out of reading the target role. A 403 for a row that does not exist
    would tell the caller less than it should."""
    monkeypatch.setattr(
        f"{SERVICE}.get_target_role", AsyncMock(side_effect=NotFoundError("no such notification"))
    )

    response = await async_client.put("/api/notifications/nope/done", headers=auth("Purchasing"))

    assert response.status_code == 404


# ==========================================
# Reporting unavailable quantities
# ==========================================

@pytest.mark.asyncio
async def test_reporting_needs_backoffice_or_warehouse(async_client, auth):
    """Whoever handles the outgoing goods reports what was not there — the same finding
    arises while picking at the shelf."""
    response = await async_client.post(
        "/api/notifications/unavailable",
        json={"documentNumber": "DN-2026-0001", "lines": [{"lineNumber": 1, "quantity": 2}]},
        headers=auth("Sales"),
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_reporting_answers_201(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE}.report_unavailable",
        AsyncMock(return_value=[_notification(
            id="unavailable_DN-2026-0001_1", type="unavailable", forRole="Purchasing"
        )]),
    )

    response = await async_client.post(
        "/api/notifications/unavailable",
        json={"documentNumber": "DN-2026-0001", "lines": [{"lineNumber": 1, "quantity": 2}]},
        headers=auth("Warehouse"),
    )

    assert response.status_code == 201
    assert response.json()[0]["id"] == "unavailable_DN-2026-0001_1"


@pytest.mark.asyncio
async def test_the_same_line_twice_answers_422(async_client, auth):
    """Both entries would yield the same notification through the business key, and which
    quantity survived would be decided by the order inside the UNWIND."""
    response = await async_client.post(
        "/api/notifications/unavailable",
        json={
            "documentNumber": "DN-2026-0001",
            "lines": [{"lineNumber": 1, "quantity": 2}, {"lineNumber": 1, "quantity": 3}],
        },
        headers=auth("BackOffice"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_an_empty_report_answers_422(async_client, auth):
    response = await async_client.post(
        "/api/notifications/unavailable",
        json={"documentNumber": "DN-2026-0001", "lines": []},
        headers=auth("BackOffice"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_line_answers_404(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE}.report_unavailable",
        AsyncMock(side_effect=NotFoundError("line number does not exist")),
    )

    response = await async_client.post(
        "/api/notifications/unavailable",
        json={"documentNumber": "DN-2026-0001", "lines": [{"lineNumber": 9, "quantity": 2}]},
        headers=auth("BackOffice"),
    )

    assert response.status_code == 404


# ==========================================
# The annual run
# ==========================================

@pytest.mark.asyncio
async def test_the_manual_annual_run_belongs_to_admin(async_client, auth):
    response = await async_client.post(
        "/api/notifications/annual-run?year=2025", headers=auth("Sales")
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_annual_run_answers_the_count(async_client, auth, monkeypatch):
    monkeypatch.setattr(f"{SERVICE}.create_annual_service_list", AsyncMock(return_value=3))

    response = await async_client.post(
        "/api/notifications/annual-run?year=2025", headers=auth("Admin")
    )

    assert response.status_code == 200
    assert response.json() == 3


@pytest.mark.asyncio
async def test_the_annual_run_demands_a_year(async_client, auth):
    """Without one the endpoint would have to guess which year is meant — and the current
    one already happens by itself on every list request."""
    response = await async_client.post("/api/notifications/annual-run", headers=auth("Admin"))

    assert response.status_code == 422
