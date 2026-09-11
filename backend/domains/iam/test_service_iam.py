"""Unit tests of the IAM service layer — repository mocked, no Neo4j needed."""

from unittest.mock import AsyncMock

import pytest

from core.exceptions import BusinessLogicError, NotFoundError
from core.security import decode_token, hash_password
from domains.iam.schemas_iam import (
    EmployeeCreate,
    EmployeeOut,
    EmployeeUpdate,
    LoginRequest,
)
from domains.iam.service_iam import AuthService, EmployeeService

SERVICE_MODULE = "domains.iam.service_iam"


def _raw_employee(**overrides) -> dict:
    data = {
        "id":       "2",
        "name":     "Erika Musterfrau",
        "email":    "erika.musterfrau@acme.example",
        "roles":    ["Engineering"],
        "active":   True,
        "password": hash_password("demo1234"),
    }
    data.update(overrides)
    return data


def _sample_employee(**overrides) -> EmployeeOut:
    data = {
        "id": "2", "name": "Erika Musterfrau", "initials": "EM",
        "email": "erika.musterfrau@acme.example", "roles": ["Engineering"],
    }
    data.update(overrides)
    return EmployeeOut(**data)


# ==========================================
# AuthService.login
# ==========================================

@pytest.mark.asyncio
async def test_login_with_an_unknown_email_is_a_business_logic_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_by_email", AsyncMock(return_value=None)
    )

    with pytest.raises(BusinessLogicError):
        await AuthService.login(
            LoginRequest(identifier="unknown@acme.example", password="whatever"), AsyncMock()
        )


@pytest.mark.asyncio
async def test_login_with_a_locked_account_is_a_business_logic_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_by_email",
        AsyncMock(return_value=_raw_employee(active=False)),
    )

    with pytest.raises(BusinessLogicError):
        await AuthService.login(
            LoginRequest(identifier="erika.musterfrau@acme.example", password="demo1234"),
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_login_with_a_wrong_password_is_a_business_logic_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_by_email",
        AsyncMock(return_value=_raw_employee()),
    )

    with pytest.raises(BusinessLogicError):
        await AuthService.login(
            LoginRequest(identifier="erika.musterfrau@acme.example", password="wrong"), AsyncMock()
        )


@pytest.mark.asyncio
async def test_login_without_a_stored_password_is_a_business_logic_error(monkeypatch):
    """A node whose `password` property is missing entirely must not sign anyone in.

    `verify_password` would raise on an empty hash rather than return False, so the guard
    has to come before it.
    """
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_by_email",
        AsyncMock(return_value=_raw_employee(password="")),
    )

    with pytest.raises(BusinessLogicError):
        await AuthService.login(
            LoginRequest(identifier="erika.musterfrau@acme.example", password="demo1234"),
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_a_successful_login_returns_a_token_with_the_right_claims(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_by_email",
        AsyncMock(return_value=_raw_employee()),
    )

    result = await AuthService.login(
        LoginRequest(identifier="erika.musterfrau@acme.example", password="demo1234"), AsyncMock()
    )

    claims = decode_token(result.access_token)
    assert claims["sub"] == "2"
    assert claims["email"] == "erika.musterfrau@acme.example"
    assert claims["roles"] == ["Engineering"]


@pytest.mark.asyncio
async def test_the_token_never_carries_the_password_hash(monkeypatch):
    """A JWT is signed, not encrypted — anyone holding it can read every claim."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_by_email",
        AsyncMock(return_value=_raw_employee()),
    )

    result = await AuthService.login(
        LoginRequest(identifier="erika.musterfrau@acme.example", password="demo1234"), AsyncMock()
    )

    assert "password" not in decode_token(result.access_token)


@pytest.mark.asyncio
async def test_login_by_initials_queries_the_initials_repository(monkeypatch):
    """`login` decides on the '@' alone which lookup runs.

    Every other login test passes an e-mail address and therefore only ever walks the one
    branch; without this test the other would stay unchecked.
    """
    by_initials = AsyncMock(return_value=_raw_employee())
    by_email = AsyncMock(return_value=None)
    monkeypatch.setattr(f"{SERVICE_MODULE}.EmployeeRepository.get_by_initials", by_initials)
    monkeypatch.setattr(f"{SERVICE_MODULE}.EmployeeRepository.get_by_email", by_email)

    result = await AuthService.login(
        LoginRequest(identifier="EM", password="demo1234"), AsyncMock()
    )

    assert decode_token(result.access_token)["sub"] == "2"
    assert by_initials.await_count == 1
    assert by_email.await_count == 0


# ==========================================
# EmployeeService — thin shell over the repository, plus the 404 translation
# ==========================================

@pytest.mark.asyncio
async def test_get_by_id_not_found_is_a_not_found_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_by_id", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await EmployeeService.get_by_id("999", AsyncMock())


@pytest.mark.asyncio
async def test_get_by_id_found_is_passed_through(monkeypatch):
    expected = _sample_employee()
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_by_id", AsyncMock(return_value=expected)
    )

    assert await EmployeeService.get_by_id("2", AsyncMock()) is expected


@pytest.mark.asyncio
async def test_create_is_passed_through(monkeypatch):
    expected = _sample_employee()
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.create", AsyncMock(return_value=expected)
    )

    data = EmployeeCreate(
        name="Erika Musterfrau", initials="EM", email="erika.musterfrau@acme.example",
        password="demo1234", roles=["Engineering"],
    )
    assert await EmployeeService.create(data, AsyncMock()) is expected


@pytest.mark.asyncio
async def test_update_not_found_is_a_not_found_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.update", AsyncMock(return_value=None)
    )

    with pytest.raises(NotFoundError):
        await EmployeeService.update("999", EmployeeUpdate(name="X"), AsyncMock())


@pytest.mark.asyncio
async def test_update_found_is_passed_through(monkeypatch):
    expected = _sample_employee(name="New Name")
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.update", AsyncMock(return_value=expected)
    )

    result = await EmployeeService.update("2", EmployeeUpdate(name="New Name"), AsyncMock())

    assert result is expected


@pytest.mark.asyncio
async def test_delete_not_found_is_a_not_found_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.delete", AsyncMock(return_value=False)
    )

    with pytest.raises(NotFoundError):
        await EmployeeService.delete("999", AsyncMock())


@pytest.mark.asyncio
async def test_a_successful_delete_raises_nothing(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.delete", AsyncMock(return_value=True)
    )

    await EmployeeService.delete("2", AsyncMock())


# ==========================================
# change_password
# ==========================================

@pytest.mark.asyncio
async def test_change_password_with_a_wrong_current_password_is_a_business_logic_error(monkeypatch):
    """Without this check a stolen token alone would be enough to take the account over."""
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_raw_by_id",
        AsyncMock(return_value=_raw_employee()),
    )
    update = AsyncMock()
    monkeypatch.setattr(f"{SERVICE_MODULE}.EmployeeRepository.update", update)

    from domains.iam.schemas_iam import PasswordChangeRequest

    with pytest.raises(BusinessLogicError):
        await EmployeeService.change_password(
            "2", PasswordChangeRequest(currentPassword="wrong", newPassword="newpassword1"),
            AsyncMock(),
        )

    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_password_for_an_unknown_employee_is_a_not_found_error(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_raw_by_id", AsyncMock(return_value=None)
    )

    from domains.iam.schemas_iam import PasswordChangeRequest

    with pytest.raises(NotFoundError):
        await EmployeeService.change_password(
            "999", PasswordChangeRequest(currentPassword="demo1234", newPassword="newpassword1"),
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_change_password_writes_the_new_password(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.get_raw_by_id",
        AsyncMock(return_value=_raw_employee()),
    )
    update = AsyncMock()
    monkeypatch.setattr(f"{SERVICE_MODULE}.EmployeeRepository.update", update)

    from domains.iam.schemas_iam import PasswordChangeRequest

    await EmployeeService.change_password(
        "2", PasswordChangeRequest(currentPassword="demo1234", newPassword="newpassword1"),
        AsyncMock(),
    )

    assert update.await_count == 1
    assert update.await_args is not None
    assert update.await_args.args[1].password == "newpassword1"


# ==========================================
# REAL-TIME EVENTS
# ==========================================
# manager.send_event is patched at its definition site (core.websocket) — the same
# instance service_iam imports as `manager`. One event per operation, none on a rollback.
# `employee` as an event entity is harmless: it carries the id only, no content.

MANAGER_MODULE = "core.websocket"


@pytest.mark.asyncio
async def test_create_sends_an_event(monkeypatch):
    expected = _sample_employee()
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.create", AsyncMock(return_value=expected)
    )
    mock_send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", mock_send_event)

    data = EmployeeCreate(
        name="Erika Musterfrau", initials="EM", email="erika.musterfrau@acme.example",
        password="demo1234", roles=["Engineering"],
    )
    await EmployeeService.create(data, AsyncMock())

    mock_send_event.assert_awaited_once_with({
        "type": "event", "entity": "employee", "trigger": "employee_created",
        "reference": "2", "ids": ["2"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_update_sends_an_event(monkeypatch):
    expected = _sample_employee(name="New Name")
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.update", AsyncMock(return_value=expected)
    )
    mock_send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", mock_send_event)

    await EmployeeService.update("2", EmployeeUpdate(name="New Name"), AsyncMock())

    mock_send_event.assert_awaited_once_with({
        "type": "event", "entity": "employee", "trigger": "employee_updated",
        "reference": "2", "ids": ["2"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_update_sends_no_event_for_an_unknown_employee(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.update", AsyncMock(return_value=None)
    )
    mock_send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", mock_send_event)

    with pytest.raises(NotFoundError):
        await EmployeeService.update("999", EmployeeUpdate(name="X"), AsyncMock())

    mock_send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_sends_an_event(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.delete", AsyncMock(return_value=True)
    )
    mock_send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", mock_send_event)

    await EmployeeService.delete("2", AsyncMock())

    mock_send_event.assert_awaited_once_with({
        "type": "event", "entity": "employee", "trigger": "employee_deleted",
        "reference": "2", "ids": ["2"], "scope": "list",
    })


@pytest.mark.asyncio
async def test_delete_sends_no_event_for_an_unknown_employee(monkeypatch):
    monkeypatch.setattr(
        f"{SERVICE_MODULE}.EmployeeRepository.delete", AsyncMock(return_value=False)
    )
    mock_send_event = AsyncMock()
    monkeypatch.setattr(f"{MANAGER_MODULE}.manager.send_event", mock_send_event)

    with pytest.raises(NotFoundError):
        await EmployeeService.delete("999", AsyncMock())

    mock_send_event.assert_not_awaited()
