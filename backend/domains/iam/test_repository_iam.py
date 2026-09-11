"""Unit tests of the IAM repository — without a database.

Roles hang off the `HAS_ROLE` edge, not off a property. Every read query therefore returns
two parts: `properties(e)` (without `roles`) and, separately, `roles` from a
`collect(r.name)`. The fakes here reproduce exactly that shape.

`create()` and `update()` are deliberately absent: they run inside an `execute_write`
transaction function with `tx.run()` directly and therefore bypass `read_single` /
`write_summary` — there is nothing to intercept. Their behaviour is covered by the
integration tests. The one exception is the guard clause in `update()`, which fires before
any session is touched.
"""

from unittest.mock import AsyncMock

import pytest

from core.exceptions import DatabaseError
from domains.iam.repository_iam import EmployeeRepository
from domains.iam.schemas_iam import EmployeeUpdate

REPOSITORY_MODULE = "domains.iam.repository_iam"


def _properties(**overrides) -> dict:
    """What `properties(e)` returns — without `roles`, since the edge is not a property."""
    data = {
        "id":       "2",
        "name":     "Erika Musterfrau",
        "initials": None,
        "email":    "erika.musterfrau@acme.example",
        "active":   True,
        "password": "$2b$12$hashed",
    }
    data.update(overrides)
    return data


def _record(roles: list[str] | None = None, **property_overrides) -> dict:
    """A record in the shape the read queries produce: `e` plus a separate `roles`."""
    return {"e": _properties(**property_overrides), "roles": [] if roles is None else roles}


# ==========================================
# get_by_email — including the password hash for the login check
# ==========================================

@pytest.mark.asyncio
async def test_get_by_email_returns_the_raw_dict_including_the_password(monkeypatch):
    async def fake_read_single(session, query, **params):
        assert params == {"email": "erika.musterfrau@acme.example"}
        return _record(roles=["Engineering"])

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_single", fake_read_single)

    result = await EmployeeRepository.get_by_email("erika.musterfrau@acme.example", AsyncMock())

    assert result is not None
    assert result["password"] == "$2b$12$hashed"
    assert result["roles"] == ["Engineering"]


@pytest.mark.asyncio
async def test_get_by_email_without_a_hit_returns_none(monkeypatch):
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_single", AsyncMock(return_value=None))

    result = await EmployeeRepository.get_by_email("unknown@acme.example", AsyncMock())

    assert result is None


@pytest.mark.asyncio
async def test_get_by_initials_passes_the_identifier_through(monkeypatch):
    async def fake_read_single(session, query, **params):
        assert params == {"initials": "em"}
        # The query lowercases both sides, so a lowercase identifier still matches.
        assert "toLower" in query
        return _record(roles=["Engineering"])

    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_single", fake_read_single)

    result = await EmployeeRepository.get_by_initials("em", AsyncMock())

    assert result is not None
    assert result["id"] == "2"


# ==========================================
# get_all / get_by_id — mapping to EmployeeOut, roles from the edge
# ==========================================

@pytest.mark.asyncio
async def test_get_all_maps_every_row(monkeypatch):
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_many",
        AsyncMock(return_value=[_record(roles=["Engineering"]), _record(roles=[], id="3")]),
    )

    result = await EmployeeRepository.get_all(AsyncMock())

    assert [e.id for e in result] == ["2", "3"]


@pytest.mark.asyncio
async def test_get_all_shows_an_employee_without_a_role_with_an_empty_list(monkeypatch):
    """OPTIONAL MATCH rather than MATCH: otherwise the row drops out of the result set."""
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_many", AsyncMock(return_value=[_record(roles=[])])
    )

    result = await EmployeeRepository.get_all(AsyncMock())

    assert result[0].roles == []


@pytest.mark.asyncio
async def test_get_all_never_exposes_the_password(monkeypatch):
    """The read model has no password field — a hash in the record must not survive."""
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_many", AsyncMock(return_value=[_record(roles=["Sales"])])
    )

    result = await EmployeeRepository.get_all(AsyncMock())

    assert "password" not in result[0].model_dump()


@pytest.mark.asyncio
async def test_get_by_id_returns_the_roles_from_the_edge(monkeypatch):
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_single",
        AsyncMock(return_value=_record(roles=["Sales", "Purchasing"])),
    )

    result = await EmployeeRepository.get_by_id("1", AsyncMock())

    assert result is not None
    assert result.roles == ["Sales", "Purchasing"]


@pytest.mark.asyncio
async def test_get_by_id_without_a_hit_returns_none(monkeypatch):
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.read_single", AsyncMock(return_value=None))

    result = await EmployeeRepository.get_by_id("999", AsyncMock())

    assert result is None


@pytest.mark.asyncio
async def test_a_node_the_read_model_cannot_map_becomes_a_database_error(monkeypatch):
    """A broken node is a server-side data problem — a 500, not a 422."""
    monkeypatch.setattr(
        f"{REPOSITORY_MODULE}.read_single",
        AsyncMock(return_value=_record(roles=[], email="not-an-email-address")),
    )

    with pytest.raises(DatabaseError):
        await EmployeeRepository.get_by_id("2", AsyncMock())


# ==========================================
# update — guard clause without touching the database at all
# ==========================================

@pytest.mark.asyncio
async def test_update_without_any_field_is_a_database_error():
    with pytest.raises(DatabaseError):
        await EmployeeRepository.update("2", EmployeeUpdate(), AsyncMock())


# ==========================================
# delete — success hangs off the deleted node, not off a status code
# ==========================================

@pytest.mark.asyncio
async def test_delete_reports_success_via_the_deleted_nodes(monkeypatch):
    summary = AsyncMock()
    summary.counters.nodes_deleted = 1
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.write_summary", AsyncMock(return_value=summary))

    assert await EmployeeRepository.delete("2", AsyncMock()) is True


@pytest.mark.asyncio
async def test_delete_without_a_hit_returns_false(monkeypatch):
    summary = AsyncMock()
    summary.counters.nodes_deleted = 0
    monkeypatch.setattr(f"{REPOSITORY_MODULE}.write_summary", AsyncMock(return_value=summary))

    assert await EmployeeRepository.delete("999", AsyncMock()) is False
