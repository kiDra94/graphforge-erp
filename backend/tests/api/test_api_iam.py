"""API tests of the IAM routes — the sign-in and the gate every other domain sits behind.

This file carries the tests of the authentication itself, so the other domain files only
have to check their own role gates: that a missing token ends in a 401 and a missing role in
a 403 is proven here once, in the place where the rule lives.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from core.config import get_settings
from core.exceptions import BusinessLogicError, DuplicateKeyError, NotFoundError
from core.security import DEMO_LOCKED_DETAIL, hash_password
from domains.catalog.schemas_catalog import Product
from domains.iam.schemas_iam import EmployeeOut, TokenResponse

AUTH_SERVICE = "domains.iam.router_iam.AuthService"
EMPLOYEE_SERVICE = "domains.iam.router_iam.EmployeeService"
EMPLOYEE_REPOSITORY = "domains.iam.service_iam.EmployeeRepository"
PRODUCT_SERVICE = "domains.catalog.router_catalog.ProductService"


def _employee(**overrides) -> EmployeeOut:
    data: dict = {
        "id": "1",
        "name": "Max Mustermann",
        "email": "max.mustermann@acme.example",
        "initials": "MM",
        "active": True,
        "roles": ["Sales", "User"],
    }
    data.update(overrides)
    return EmployeeOut(**data)


# ==========================================
# Sign-in
# ==========================================

@pytest.mark.asyncio
async def test_the_login_needs_no_token(async_client, monkeypatch):
    """It is the one endpoint that cannot require one — it is where the token comes from."""
    monkeypatch.setattr(
        f"{AUTH_SERVICE}.login",
        AsyncMock(return_value=TokenResponse(access_token="signed.jwt.value")),
    )

    response = await async_client.post(
        "/api/auth/login", json={"identifier": "max.mustermann@acme.example", "password": "demo1234"}
    )

    assert response.status_code == 200
    assert response.json() == {"access_token": "signed.jwt.value", "token_type": "bearer"}


@pytest.mark.asyncio
async def test_wrong_credentials_answer_400(async_client, monkeypatch):
    """A 400 and not a 404: whether the identifier exists is none of the caller's business
    before they are signed in."""
    monkeypatch.setattr(
        f"{AUTH_SERVICE}.login",
        AsyncMock(side_effect=BusinessLogicError("Identifier or password is wrong.")),
    )

    response = await async_client.post(
        "/api/auth/login", json={"identifier": "max.mustermann@acme.example", "password": "wrong"}
    )

    assert response.status_code == 400
    assert "message" in response.json()


@pytest.mark.asyncio
async def test_the_login_rejects_an_unknown_field(async_client):
    """`extra="forbid"`: a client sending `user` instead of `identifier` is meant to learn
    about it, not to be told the password was wrong."""
    response = await async_client.post(
        "/api/auth/login", json={"user": "max.mustermann@acme.example", "password": "demo1234"}
    )

    assert response.status_code == 422
    assert "user" in response.json()["message"]


@pytest.mark.asyncio
async def test_the_swagger_authorize_dialog_can_sign_in(async_client, monkeypatch):
    """The dialog posts form-encoded `username`/`password` to the scheme's `tokenUrl`.

    The URL is read from the published OpenAPI document instead of being written down
    here, so the test breaks if the scheme ever points at an endpoint that takes JSON.
    """
    login = AsyncMock(return_value=TokenResponse(access_token="signed.jwt.value"))
    monkeypatch.setattr(f"{AUTH_SERVICE}.login", login)
    spec = (await async_client.get("/api/openapi.json")).json()
    scheme = spec["components"]["securitySchemes"]["OAuth2PasswordBearer"]
    token_url = scheme["flows"]["password"]["tokenUrl"]

    response = await async_client.post(
        token_url,
        data={"grant_type": "password", "username": "MM", "password": "demo1234"},
    )

    assert response.status_code == 200
    assert response.json()["access_token"] == "signed.jwt.value"
    login.assert_awaited_once()
    sent = login.call_args.args[0]
    assert (sent.identifier, sent.password) == ("MM", "demo1234")


@pytest.mark.asyncio
async def test_the_form_sign_in_rejects_wrong_credentials_like_the_json_one(
    async_client, monkeypatch
):
    monkeypatch.setattr(
        f"{AUTH_SERVICE}.login",
        AsyncMock(side_effect=BusinessLogicError("Identifier or password is wrong.")),
    )

    response = await async_client.post(
        "/api/auth/token",
        data={"username": "max.mustermann@acme.example", "password": "wrong"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_the_token_from_the_authorize_dialog_opens_the_product_list(
    async_client, monkeypatch
):
    """What a visitor of the Swagger UI does: *Authorize*, then `GET /api/products`.

    Unlike the tests above, the sign-in is not mocked. Only the repository is, so the real
    `AuthService` checks the password against a bcrypt hash and signs a real JWT, and the
    product route has to accept that very token. Without it the same route answers 401.
    """
    monkeypatch.setattr(
        f"{EMPLOYEE_REPOSITORY}.get_by_email",
        AsyncMock(return_value={
            "id": "1",
            "email": "max.mustermann@acme.example",
            "name": "Max Mustermann",
            "active": True,
            "password": hash_password("demo1234"),
            "roles": ["Sales"],
        }),
    )
    monkeypatch.setattr(
        f"{PRODUCT_SERVICE}.get_products",
        AsyncMock(return_value=[Product(number="ACME-1000", label="Starter Kit A", unit="pcs")]),
    )
    assert (await async_client.get("/api/products")).status_code == 401

    sign_in = await async_client.post(
        "/api/auth/token",
        data={
            "grant_type": "password",
            "username": "max.mustermann@acme.example",
            "password": "demo1234",
        },
    )
    assert sign_in.status_code == 200
    token = sign_in.json()["access_token"]

    response = await async_client.get(
        "/api/products", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()[0]["number"] == "ACME-1000"


@pytest.mark.asyncio
async def test_the_form_sign_in_stays_out_of_the_api_documentation(async_client):
    """It exists for the dialog; the documented sign-in is the JSON one."""
    spec = (await async_client.get("/api/openapi.json")).json()

    assert "/api/auth/token" not in spec["paths"]
    assert "/api/auth/login" in spec["paths"]


# ==========================================
# The gate
# ==========================================

@pytest.mark.asyncio
async def test_a_protected_route_without_a_token_answers_401(async_client):
    response = await async_client.get("/api/auth/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_garbled_token_answers_401(async_client):
    """Signature and expiry are checked; a self-made payload does not get through."""
    response = await async_client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not.a.real.token"}
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_token_signed_with_another_key_answers_401(async_client, monkeypatch):
    """The one test that proves the signature actually matters: the payload is well formed,
    only the key is wrong."""
    import jwt

    from core.config import get_settings

    settings = get_settings()
    foreign = jwt.encode(
        {"sub": "1", "roles": ["Admin"]},
        "a-different-secret-of-a-length-the-library-accepts",
        algorithm=settings.JWT_ALGORITHM,
    )

    response = await async_client.get("/api/users", headers={"Authorization": f"Bearer {foreign}"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_an_expired_token_answers_401(async_client):
    """Correctly signed, only past its `exp`. For the interface a fresh sign-in is something
    else than a tampered token, but both end in a 401."""
    import jwt

    from core.config import get_settings

    settings = get_settings()
    expired = jwt.encode(
        {
            "sub": "1",
            "roles": ["Admin"],
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    response = await async_client.get("/api/users", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_missing_role_answers_403(async_client, auth):
    """403 and not 404: the caller is authenticated, they simply may not do this. A 404 would
    additionally hide whether the route exists at all."""
    response = await async_client.get("/api/users", headers=auth("Sales"))

    assert response.status_code == 403
    assert "message" in response.json()


@pytest.mark.asyncio
async def test_admin_passes_every_role_check(async_client, auth, monkeypatch):
    """`has_role` lets Admin through everywhere — without it, the role would have to be
    added to every single list."""
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.get_all", AsyncMock(return_value=[_employee()]))

    response = await async_client.get("/api/users", headers=auth("Admin"))

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_me_reads_the_user_out_of_the_token(async_client, auth, monkeypatch):
    """The id comes from the `sub` claim, not from a path parameter — nobody may ask for
    somebody else's profile through this route."""
    service = AsyncMock(return_value=_employee(id="7"))
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.get_by_id", service)

    await async_client.get("/api/auth/me", headers=auth("Sales", sub="7"))

    assert service.await_args is not None
    assert service.await_args.args[0] == "7"


@pytest.mark.asyncio
async def test_logout_answers_204(async_client, auth):
    response = await async_client.post("/api/auth/logout", headers=auth("Sales"))

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_logout_still_checks_the_token(async_client):
    """There is no session to end, but an expired or missing token has to answer 401 all the
    same — otherwise the endpoint would be the one place that accepts anybody."""
    response = await async_client.post("/api/auth/logout")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_changing_ones_own_password_uses_the_id_from_the_token(async_client, auth, monkeypatch):
    """Like `/me`: the id comes from `sub`, so nobody can change somebody else's password
    through this route."""
    service = AsyncMock(return_value=None)
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.change_password", service)

    response = await async_client.patch(
        "/api/auth/me/password",
        json={"currentPassword": "demo1234", "newPassword": "a-new-password"},
        headers=auth("Sales", sub="7"),
    )

    assert response.status_code == 204
    assert service.await_args is not None
    assert service.await_args.args[0] == "7"


@pytest.mark.asyncio
async def test_a_wrong_current_password_answers_400(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{EMPLOYEE_SERVICE}.change_password",
        AsyncMock(side_effect=BusinessLogicError("The current password is wrong.")),
    )

    response = await async_client.patch(
        "/api/auth/me/password",
        json={"currentPassword": "wrong", "newPassword": "a-new-password"},
        headers=auth("Sales"),
    )

    assert response.status_code == 400


# ==========================================
# User administration
# ==========================================

@pytest.mark.asyncio
async def test_the_user_list_never_carries_a_password(async_client, auth, monkeypatch):
    """Neither the plaintext nor the hash ever leaves the server — the read model has no
    field for it, and this test is what keeps it that way."""
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.get_all", AsyncMock(return_value=[_employee()]))

    response = await async_client.get("/api/users", headers=auth("Admin"))

    body = response.json()[0]
    assert "password" not in body
    assert "passwordHash" not in body


@pytest.mark.asyncio
async def test_creating_a_user_answers_201(async_client, auth, monkeypatch):
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.create", AsyncMock(return_value=_employee(id="9")))

    response = await async_client.post(
        "/api/users",
        json={
            "name": "New Person", "email": "new.person@acme.example", "initials": "NP",
            "password": "demo1234", "roles": ["Sales"],
        },
        headers=auth("Admin"),
    )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_a_duplicate_email_answers_409(async_client, auth, monkeypatch):
    """The uniqueness comes from the constraint, not from a check beforehand — and it
    arrives as a 409, not as a 400."""
    monkeypatch.setattr(
        f"{EMPLOYEE_SERVICE}.create",
        AsyncMock(side_effect=DuplicateKeyError("e-mail already taken")),
    )

    response = await async_client.post(
        "/api/users",
        json={
            "name": "New Person", "email": "max.mustermann@acme.example", "initials": "NP",
            "password": "demo1234", "roles": ["Sales"],
        },
        headers=auth("Admin"),
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_an_unknown_user_answers_404(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{EMPLOYEE_SERVICE}.get_by_id", AsyncMock(side_effect=NotFoundError("no such user"))
    )

    response = await async_client.get("/api/users/999", headers=auth("Admin"))

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_updating_a_user_answers_with_the_changed_record(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=_employee(name="New Name"))
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.update", service)

    response = await async_client.patch(
        "/api/users/1", json={"name": "New Name"}, headers=auth("Admin")
    )

    assert response.status_code == 200
    assert response.json()["name"] == "New Name"
    assert service.await_args is not None
    assert service.await_args.args[0] == "1"


@pytest.mark.asyncio
async def test_initials_taken_on_update_answer_409(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{EMPLOYEE_SERVICE}.update",
        AsyncMock(side_effect=DuplicateKeyError("Initials 'MM' are already taken.")),
    )

    response = await async_client.patch(
        "/api/users/2", json={"initials": "MM"}, headers=auth("Admin")
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_deleting_a_user_answers_204(async_client, auth, monkeypatch):
    service = AsyncMock(return_value=None)
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.delete", service)

    response = await async_client.delete("/api/users/9", headers=auth("Admin"))

    assert response.status_code == 204
    assert service.await_args is not None
    assert service.await_args.args[0] == "9"


@pytest.mark.asyncio
async def test_only_admin_may_delete_a_user(async_client, auth, monkeypatch):
    """The one irreversible route in user management — checked on its own rather than
    trusted to the gate test of the list."""
    service = AsyncMock(return_value=None)
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.delete", service)

    response = await async_client.delete("/api/users/9", headers=auth("BackOffice"))

    assert response.status_code == 403
    service.assert_not_awaited()


# ==========================================
# Public demo (DEMO_MODE)
# ==========================================

@pytest.fixture
def demo_mode(monkeypatch):
    """Turns `DEMO_MODE` on for the demo lock, without touching the cached settings."""
    settings = get_settings().model_copy(update={"DEMO_MODE": True})
    monkeypatch.setattr("core.security.get_settings", lambda: settings)


# The account changes that would lock the next visitor of the demo out, each with the
# service method that must never be reached.
_DEMO_LOCKED = [
    pytest.param(
        "patch", "/api/auth/me/password",
        {"currentPassword": "demo1234", "newPassword": "a-new-password"},
        "change_password", "Sales",
        id="own-password",
    ),
    pytest.param("patch", "/api/users/1", {"name": "Taken Over"}, "update", "Admin", id="edit-user"),
    pytest.param("delete", "/api/users/1", None, "delete", "Admin", id="delete-user"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body", "service_method", "role"), _DEMO_LOCKED)
async def test_the_demo_refuses_account_changes(
    async_client, auth, monkeypatch, demo_mode, method, path, body, service_method, role
):
    service = AsyncMock()
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.{service_method}", service)

    kwargs = {"json": body} if body is not None else {}
    response = await async_client.request(method, path, headers=auth(role), **kwargs)

    assert response.status_code == 403
    assert response.json()["message"] == DEMO_LOCKED_DETAIL
    service.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_demo_lock_still_answers_401_without_a_token(async_client, demo_mode):
    """The lock only speaks to signed-in users; the gate before it stays the same."""
    response = await async_client.delete("/api/users/1")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_the_demo_leaves_every_other_write_open(async_client, auth, monkeypatch, demo_mode):
    """Only the three account changes are locked — creating a user, for one, still works,
    since the nightly reset removes it again."""
    monkeypatch.setattr(f"{EMPLOYEE_SERVICE}.create", AsyncMock(return_value=_employee(id="9")))

    response = await async_client.post(
        "/api/users",
        json={
            "name": "New Person", "email": "new.person@acme.example", "initials": "NP",
            "password": "demo1234", "roles": ["Sales"],
        },
        headers=auth("Admin"),
    )

    assert response.status_code == 201

