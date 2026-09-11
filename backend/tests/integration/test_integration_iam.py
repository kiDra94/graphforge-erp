"""Integration tests of the IAM domain against a real Neo4j.

These tests answer one question: **does the Cypher do what the code claims?**
`create()` and `update()` run several statements inside one `session.execute_write()`
transaction function — their behaviour cannot be mocked in any meaningful way, which is why
they are tested here rather than in `test_repository_iam.py`.

Three things are at the centre:

1. **Id assignment in `EmployeeRepository.create()`.** New employees get a server-side
   `E-<uuid4>` id, while the employees of the demo data keep their sequential string ids
   (`"1"`, `"2"`, …). The tests build exactly that mixed state and check that `create()`
   neither reads nor continues the sequential ids.
2. **Ids are strings throughout.** `MATCH (e:Employee {id: $id})` with an integer parameter
   never matches a string property, so the tests pass the id returned by `create()` on
   unchanged.
3. **Roles hang off the `HAS_ROLE` edge, not off a property.** The tests create `Role` nodes
   separately, the way the seed does — without them `MATCH (r:Role {name: role_name})` in
   `create()` finds nothing and silently draws no edge.
"""

import re

import pytest

from core.exceptions import BusinessLogicError, DuplicateKeyError
from core.security import verify_password
from domains.iam.repository_iam import EmployeeRepository
from domains.iam.schemas_iam import (
    EmployeeCreate,
    EmployeeUpdate,
    LoginRequest,
    PasswordChangeRequest,
)
from domains.iam.service_iam import AuthService, EmployeeService


async def create_existing_employee(session, id: str, name: str = "Existing Employee"):
    """Creates an employee directly, bypassing `create()` — with a sequential string id and
    without initials or password."""
    await session.run(
        "CREATE (e:Employee {id: $id, name: $name, email: $email})",
        id=id, name=name, email=f"{id}@acme.example",
    )


async def create_roles(session, *names: str):
    """Creates `Role` nodes, the way the seed does.

    Without these nodes `MATCH (r:Role {name: role_name})` in `create()` finds nothing — the
    edge then silently does not come about, without an error.
    """
    for name in names:
        await session.run("MERGE (:Role {name: $name})", name=name)


_E_UUID = re.compile(r"^E-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@pytest.mark.asyncio
async def test_create_assigns_an_e_prefixed_uuid(neo4j_session):
    await create_roles(neo4j_session, "Sales")

    new = await EmployeeRepository.create(
        EmployeeCreate(
            name="New Person", initials="NP", email="np@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    assert _E_UUID.match(new.id)


@pytest.mark.asyncio
async def test_create_leaves_sequential_string_ids_untouched(neo4j_session):
    # The state of the demo data: sequential string ids "1" to "3". create() must neither
    # read nor continue them — the new id comes about independently as E-<uuid4>.
    await create_existing_employee(neo4j_session, "1")
    await create_existing_employee(neo4j_session, "2")
    await create_existing_employee(neo4j_session, "3")
    await create_roles(neo4j_session, "Sales")

    new = await EmployeeRepository.create(
        EmployeeCreate(
            name="New Person", initials="NP", email="np@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    assert _E_UUID.match(new.id)
    assert new.id not in {"1", "2", "3"}


@pytest.mark.asyncio
async def test_two_employees_created_in_a_row_get_different_ids(neo4j_session):
    await create_roles(neo4j_session, "Sales")

    first = await EmployeeRepository.create(
        EmployeeCreate(
            name="First Person", initials="FP", email="first@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )
    second = await EmployeeRepository.create(
        EmployeeCreate(
            name="Second Person", initials="SP", email="second@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    assert first.id != second.id


@pytest.mark.asyncio
async def test_create_stores_the_password_hashed_and_verifiable(neo4j_session):
    await create_roles(neo4j_session, "Sales", "User")

    await EmployeeRepository.create(
        EmployeeCreate(
            name="New Person", initials="NP", email="np@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    stored = await EmployeeRepository.get_by_email("np@acme.example", neo4j_session)

    assert stored is not None
    assert stored["password"] != "demo1234"
    assert verify_password("demo1234", stored["password"])
    # The base role is added automatically; the requested role stays alongside it.
    assert sorted(stored["roles"]) == sorted(["Sales", "User"])


@pytest.mark.asyncio
async def test_create_adds_the_base_role_even_when_not_requested(neo4j_session):
    # The base role is assigned regardless of what the caller sends — even an empty role
    # list gets it.
    await create_roles(neo4j_session, "User")

    new = await EmployeeRepository.create(
        EmployeeCreate(
            name="New Person", initials="NP2", email="np2@acme.example",
            password="demo1234", roles=[],
        ),
        neo4j_session,
    )

    assert new.roles == ["User"]


@pytest.mark.asyncio
async def test_duplicate_email_is_a_duplicate_key_error(neo4j_session):
    await create_roles(neo4j_session, "Sales", "Engineering")

    data = EmployeeCreate(
        name="Person One", initials="P1", email="duplicate@acme.example",
        password="demo1234", roles=["Sales"],
    )
    await EmployeeRepository.create(data, neo4j_session)

    with pytest.raises(DuplicateKeyError):
        await EmployeeRepository.create(
            EmployeeCreate(
                name="Person Two", initials="P2", email="duplicate@acme.example",
                password="different", roles=["Engineering"],
            ),
            neo4j_session,
        )


@pytest.mark.asyncio
async def test_duplicate_initials_are_a_duplicate_key_error_regardless_of_case(neo4j_session):
    # Login by initials matches case-insensitively, so "ab" and "AB" would be the same
    # login name — create() has to reject the second one.
    await create_roles(neo4j_session, "Sales")
    await EmployeeRepository.create(
        EmployeeCreate(
            name="Person One", initials="AB", email="one@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    with pytest.raises(DuplicateKeyError):
        await EmployeeRepository.create(
            EmployeeCreate(
                name="Person Two", initials="ab", email="two@acme.example",
                password="demo1234", roles=["Sales"],
            ),
            neo4j_session,
        )


@pytest.mark.asyncio
async def test_get_all_finds_every_created_employee(neo4j_session):
    await create_roles(neo4j_session, "Sales", "Engineering")

    await EmployeeRepository.create(
        EmployeeCreate(
            name="Anna Sample", initials="AS", email="anna@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )
    await EmployeeRepository.create(
        EmployeeCreate(
            name="Ben Sample", initials="BS", email="ben@acme.example",
            password="demo1234", roles=["Engineering"],
        ),
        neo4j_session,
    )

    everyone = await EmployeeRepository.get_all(neo4j_session)

    assert sorted(e.name for e in everyone) == ["Anna Sample", "Ben Sample"]


@pytest.mark.asyncio
async def test_update_changes_only_the_fields_sent(neo4j_session):
    await create_roles(neo4j_session, "Sales", "User")
    created = await EmployeeRepository.create(
        EmployeeCreate(
            name="Old Name", initials="ON", email="person@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    updated = await EmployeeRepository.update(
        created.id, EmployeeUpdate(name="New Name"), neo4j_session
    )

    assert updated is not None
    assert updated.name == "New Name"
    assert updated.email == "person@acme.example"
    # The roles were not part of the update — the edges have to stay untouched.
    assert sorted(updated.roles) == sorted(["Sales", "User"])


@pytest.mark.asyncio
async def test_update_with_roles_replaces_the_old_edges(neo4j_session):
    await create_roles(neo4j_session, "Sales", "Engineering", "BackOffice", "User")
    created = await EmployeeRepository.create(
        EmployeeCreate(
            name="Person", initials="PS", email="person-roles@acme.example",
            password="demo1234", roles=["Sales", "Engineering"],
        ),
        neo4j_session,
    )

    updated = await EmployeeRepository.update(
        created.id, EmployeeUpdate(roles=["BackOffice"]), neo4j_session
    )

    assert updated is not None
    # Sales and Engineering have to be gone rather than stand next to BackOffice —
    # otherwise old and new roles add up instead of being replaced. The base role stays: a
    # replacing role list must not remove it.
    assert sorted(updated.roles) == sorted(["BackOffice", "User"])


@pytest.mark.asyncio
async def test_update_with_roles_without_the_base_role_keeps_it_anyway(neo4j_session):
    # A caller who simply forgets the base role when replacing must not take it away from
    # the employee.
    await create_roles(neo4j_session, "Sales", "Purchasing", "User")
    created = await EmployeeRepository.create(
        EmployeeCreate(
            name="Person", initials="PS2", email="person-base-role@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    updated = await EmployeeRepository.update(
        created.id, EmployeeUpdate(roles=["Purchasing"]), neo4j_session
    )

    assert updated is not None
    assert sorted(updated.roles) == sorted(["Purchasing", "User"])


@pytest.mark.asyncio
async def test_update_to_the_initials_of_somebody_else_is_a_duplicate_key_error(neo4j_session):
    # Lower case on purpose: the sign-in matches initials case-insensitively, so "ab" would
    # be the same login name as "AB" — the check has to catch it, not only the exact spelling.
    await create_roles(neo4j_session, "Sales")
    await EmployeeRepository.create(
        EmployeeCreate(
            name="Person One", initials="AB", email="one@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )
    second = await EmployeeRepository.create(
        EmployeeCreate(
            name="Person Two", initials="CD", email="two@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    with pytest.raises(DuplicateKeyError):
        await EmployeeRepository.update(second.id, EmployeeUpdate(initials="ab"), neo4j_session)


@pytest.mark.asyncio
async def test_update_that_sends_ones_own_initials_back_is_no_conflict(neo4j_session):
    # A form sends the whole record back, unchanged initials included. The check finds the
    # employee themselves under those initials — that must not count as a collision.
    await create_roles(neo4j_session, "Sales")
    created = await EmployeeRepository.create(
        EmployeeCreate(
            name="Old Name", initials="AB", email="own-initials@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    updated = await EmployeeRepository.update(
        created.id, EmployeeUpdate(name="New Name", initials="AB"), neo4j_session
    )

    assert updated is not None
    assert updated.name == "New Name"
    assert updated.initials == "AB"


@pytest.mark.asyncio
async def test_delete_removes_the_employee_and_reports_success(neo4j_session):
    await create_roles(neo4j_session, "Sales")
    created = await EmployeeRepository.create(
        EmployeeCreate(
            name="To Delete", initials="TD", email="delete@acme.example",
            password="demo1234", roles=["Sales"],
        ),
        neo4j_session,
    )

    deleted = await EmployeeRepository.delete(created.id, neo4j_session)

    assert deleted is True
    assert await EmployeeRepository.get_by_id(created.id, neo4j_session) is None


@pytest.mark.asyncio
async def test_delete_of_an_unknown_employee_returns_false(neo4j_session):
    assert await EmployeeRepository.delete("999", neo4j_session) is False


@pytest.mark.asyncio
async def test_roles_from_an_edge_drawn_directly_rather_than_through_create(neo4j_session):
    """The state the seed leaves behind: the edge exists, `create()` was never involved.

    Tests that build employees exclusively through `create()` only check the code against
    itself — a typo in a role name or a role missing from the graph would go unnoticed.
    """
    await create_roles(neo4j_session, "Sales", "Admin")
    await neo4j_session.run(
        "CREATE (e:Employee {id: '1', name: 'Max Mustermann', "
        "email: 'max.mustermann@acme.example', password: $pw}) "
        "WITH e MATCH (r:Role {name: 'Sales'}) MERGE (e)-[:HAS_ROLE]->(r)",
        pw="$2b$12$hashed",
    )

    by_email = await EmployeeRepository.get_by_email("max.mustermann@acme.example", neo4j_session)
    by_id = await EmployeeRepository.get_by_id("1", neo4j_session)

    assert by_email is not None
    assert by_id is not None
    assert by_email["roles"] == ["Sales"]
    assert by_id.roles == ["Sales"]


@pytest.mark.asyncio
async def test_employee_without_a_role_appears_with_an_empty_list(neo4j_session):
    # OPTIONAL MATCH rather than MATCH: otherwise this employee drops out of get_all().
    await create_existing_employee(neo4j_session, "1", name="Without Role")

    everyone = await EmployeeRepository.get_all(neo4j_session)

    assert len(everyone) == 1
    assert everyone[0].roles == []


# --- Login and passwords -------------------------------------------------------
# The tests below cover what user management promises the administrator: a locked account
# no longer gets in, a reset password invalidates the old one, and changing one's own
# password requires the current one. All three run over hashes and the account status in
# the graph — a mock would check none of it.


async def create_account(session, email: str, initials: str, password: str = "demo1234"):
    """Creates an account that can sign in and returns the created employee."""
    await create_roles(session, "Sales", "User")
    return await EmployeeRepository.create(
        EmployeeCreate(
            name="Test Person", initials=initials, email=email,
            password=password, roles=["Sales"],
        ),
        session,
    )


@pytest.mark.asyncio
async def test_login_with_the_email_succeeds(neo4j_session):
    await create_account(neo4j_session, "login@acme.example", "LG")

    token = await AuthService.login(
        LoginRequest(identifier="login@acme.example", password="demo1234"), neo4j_session
    )

    assert token.access_token


@pytest.mark.asyncio
async def test_login_with_the_initials_succeeds_as_well(neo4j_session):
    # The second path in AuthService.login: without an "@" in the identifier the lookup runs
    # over get_by_initials rather than get_by_email.
    await create_account(neo4j_session, "initials@acme.example", "IN")

    token = await AuthService.login(
        LoginRequest(identifier="IN", password="demo1234"), neo4j_session
    )

    assert token.access_token


@pytest.mark.asyncio
async def test_a_locked_account_no_longer_gets_in(neo4j_session):
    created = await create_account(neo4j_session, "locked@acme.example", "LK")
    await EmployeeRepository.update(created.id, EmployeeUpdate(active=False), neo4j_session)

    with pytest.raises(BusinessLogicError):
        await AuthService.login(
            LoginRequest(identifier="locked@acme.example", password="demo1234"),
            neo4j_session,
        )


@pytest.mark.asyncio
async def test_an_unlocked_account_signs_in_again(neo4j_session):
    # Locking must not damage the password: after unlocking, the same one applies again
    # without having to set it anew.
    created = await create_account(neo4j_session, "again@acme.example", "AG")
    await EmployeeRepository.update(created.id, EmployeeUpdate(active=False), neo4j_session)
    await EmployeeRepository.update(created.id, EmployeeUpdate(active=True), neo4j_session)

    token = await AuthService.login(
        LoginRequest(identifier="again@acme.example", password="demo1234"), neo4j_session
    )

    assert token.access_token


@pytest.mark.asyncio
async def test_a_reset_by_the_admin_invalidates_the_old_password(neo4j_session):
    created = await create_account(neo4j_session, "reset@acme.example", "RS")

    await EmployeeRepository.update(
        created.id, EmployeeUpdate(password="newSecret9"), neo4j_session
    )

    with pytest.raises(BusinessLogicError):
        await AuthService.login(
            LoginRequest(identifier="reset@acme.example", password="demo1234"), neo4j_session
        )
    token = await AuthService.login(
        LoginRequest(identifier="reset@acme.example", password="newSecret9"), neo4j_session
    )
    assert token.access_token


@pytest.mark.asyncio
async def test_a_reset_stores_the_new_password_hashed_as_well(neo4j_session):
    # The plaintext must not land in the graph on the update path either — create() hashes,
    # update() has to do the same.
    created = await create_account(neo4j_session, "hash@acme.example", "HS")

    await EmployeeRepository.update(
        created.id, EmployeeUpdate(password="newSecret9"), neo4j_session
    )

    raw = await EmployeeRepository.get_raw_by_id(created.id, neo4j_session)
    assert raw is not None
    assert raw["password"] != "newSecret9"
    assert verify_password("newSecret9", raw["password"])


@pytest.mark.asyncio
async def test_changing_ones_own_password_replaces_the_hash(neo4j_session):
    created = await create_account(neo4j_session, "self@acme.example", "SF")

    await EmployeeService.change_password(
        created.id,
        PasswordChangeRequest(currentPassword="demo1234", newPassword="selfChosen7"),
        neo4j_session,
    )

    token = await AuthService.login(
        LoginRequest(identifier="self@acme.example", password="selfChosen7"), neo4j_session
    )
    assert token.access_token


@pytest.mark.asyncio
async def test_changing_ones_own_password_requires_the_current_one(neo4j_session):
    created = await create_account(neo4j_session, "wrong@acme.example", "WR")

    with pytest.raises(BusinessLogicError):
        await EmployeeService.change_password(
            created.id,
            PasswordChangeRequest(currentPassword="notRight", newPassword="whatever12"),
            neo4j_session,
        )

    # The rejected attempt must not have touched the existing password.
    token = await AuthService.login(
        LoginRequest(identifier="wrong@acme.example", password="demo1234"), neo4j_session
    )
    assert token.access_token
