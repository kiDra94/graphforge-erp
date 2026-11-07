"""End-to-end tests of the paths a visitor of the live demo takes through the Swagger UI.

Sign in with *Authorize*, call an endpoint with *Try it out* → *Execute*, sign out again —
in Chromium, against the real backend and the full demo graph. One test per HTTP method the
API uses (GET, POST, PATCH, PUT, DELETE) and one for signing out. Covering every single
endpoint is the job of the API and integration tests; what is proven here is that the
Swagger UI and the backend agree on each kind of request: path parameters, JSON bodies,
empty 204 answers and the token in the header.

All tests share one seeded database. None of them depends on what another one wrote: a
test that changes or deletes something first creates its own product, through the API
rather than the UI, under a number no other test uses.

The locators target the markup of the Swagger UI itself, so a test failing after a Swagger
UI update may mean the markup moved rather than that the backend broke; the screenshot
pytest-playwright keeps on failure (`--screenshot only-on-failure`) tells the two apart.
"""

import json

from playwright.sync_api import Locator, Page, expect

DEMO_PASSWORD = "demo1234"
SALES = "max.mustermann@acme.example"
ENGINEERING = "erika.musterfrau@acme.example"


# ==========================================
# Helpers
# ==========================================

def _authorize(page: Page, username: str) -> None:
    """Signs in through the *Authorize* dialog and closes it again.

    Waits for the *Authorized* heading: the dialog only shows it once the token request
    has succeeded, so a rejected sign-in fails here rather than at the next request.
    """
    page.locator(".auth-wrapper button.authorize").click()
    dialog = page.locator(".modal-ux")
    dialog.locator("#oauth_username").fill(username)
    dialog.locator("#oauth_password").fill(DEMO_PASSWORD)
    dialog.locator("button.authorize").click()
    expect(dialog.get_by_role("heading", name="Authorized")).to_be_visible()
    dialog.locator("button.btn-done").click()
    expect(dialog).to_be_hidden()


def _logout(page: Page) -> None:
    """Signs out through the *Authorize* dialog: *Logout*, then closes it.

    After *Logout* the dialog offers the sign-in form again, which is what is waited for.
    """
    page.locator(".auth-wrapper button.authorize").click()
    dialog = page.locator(".modal-ux")
    dialog.locator("button", has_text="Logout").click()
    expect(dialog.locator("#oauth_username")).to_be_visible()
    dialog.locator("button.btn-done").click()
    expect(dialog).to_be_hidden()


def _execute(
    page: Page,
    method: str,
    path: str,
    params: dict[str, str] | None = None,
    body: dict | None = None,
) -> Locator:
    """Runs one operation with *Try it out* → *Execute* and returns its live response row.

    Args:
        page: The page with the Swagger UI open.
        method: The HTTP method in lower case, as Swagger UI names its blocks.
        path: The path exactly as documented, e.g. `/api/products/{number}`.
        params: Values for the path and query parameters, by parameter name.
        body: The JSON request body, replacing the example Swagger UI fills in.
    """
    operation = page.locator(
        f'.opblock-{method}:has(.opblock-summary-path[data-path="{path}"])'
    )
    operation.locator(".opblock-summary").click()
    operation.get_by_role("button", name="Try it out").click()
    for name, value in (params or {}).items():
        operation.locator(f'tr[data-param-name="{name}"] input').fill(value)
    if body is not None:
        operation.locator("textarea.body-param__text").fill(json.dumps(body))
    operation.get_by_role("button", name="Execute").click()
    response = operation.locator(".live-responses-table .response")
    expect(response).to_be_visible()
    return response


def _expect_status(response: Locator, status: int) -> None:
    expect(response.locator(".response-col_status")).to_contain_text(str(status))


def _expect_body(response: Locator, text: str) -> None:
    expect(response.locator(".response-col_description")).to_contain_text(text)


def _api_token(page: Page, username: str) -> str:
    """Signs in through the JSON login, outside the UI, for setting up test data."""
    response = page.request.post(
        "/api/auth/login", data={"identifier": username, "password": DEMO_PASSWORD}
    )
    assert response.ok, response.text()
    return response.json()["access_token"]


def _new_product(number: str) -> dict:
    return {
        "number": number,
        "label": "Created by the e2e tests",
        "unit": "pcs",
        "minStock": 1,
        "targetStock": 5,
        "listPrice": "9.90",
    }


def _create_product_via_api(page: Page, number: str) -> None:
    """Creates a product for a test to change or delete — through the API, not the UI,
    so the test only exercises the one request it is about."""
    token = _api_token(page, ENGINEERING)
    response = page.request.post(
        "/api/products",
        data=_new_product(number),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status == 201, response.text()


# ==========================================
# Signing in
# ==========================================

def test_a_visitor_signs_in_with_authorize_and_lists_the_products(page: Page):
    page.goto("/api/docs")

    _authorize(page, SALES)
    response = _execute(page, "get", "/api/products")

    _expect_status(response, 200)
    # ACME-1000 is the first product of seed.cypher, "Starter Kit A".
    _expect_body(response, "ACME-1000")


def test_without_authorize_the_product_list_stays_closed(page: Page):
    """The counterpart: the same click path without signing in ends in a 401, so every
    success in this file is owed to the token and not to an open route."""
    page.goto("/api/docs")

    response = _execute(page, "get", "/api/products")

    _expect_status(response, 401)


# ==========================================
# One test per HTTP method
# ==========================================

def test_get_with_a_path_parameter_reads_one_product(page: Page):
    page.goto("/api/docs")
    _authorize(page, SALES)

    response = _execute(page, "get", "/api/products/{number}", params={"number": "ACME-1000"})

    _expect_status(response, 200)
    _expect_body(response, "Starter Kit A")


def test_post_creates_a_product(page: Page):
    page.goto("/api/docs")
    _authorize(page, ENGINEERING)

    response = _execute(page, "post", "/api/products", body=_new_product("ACME-E2E-POST"))

    _expect_status(response, 201)
    _expect_body(response, "ACME-E2E-POST")


def test_patch_changes_a_field_of_a_product(page: Page):
    _create_product_via_api(page, "ACME-E2E-PATCH")
    page.goto("/api/docs")
    _authorize(page, ENGINEERING)

    response = _execute(
        page,
        "patch",
        "/api/products/{number}",
        params={"number": "ACME-E2E-PATCH"},
        body={"label": "Renamed in the Swagger UI"},
    )

    _expect_status(response, 200)
    _expect_body(response, "Renamed in the Swagger UI")


def test_put_reports_a_service_as_done(page: Page):
    """`SN-2026-0001` is a seeded asset built from ACME-1000, and ACME-2005 one of the
    leaves of its bill of materials — so the component instance exists in the demo graph."""
    page.goto("/api/docs")
    _authorize(page, SALES)

    response = _execute(
        page,
        "put",
        "/api/assets/service/{componentInstanceId}/completion",
        params={"componentInstanceId": "SN-2026-0001_ACME-2005"},
        body={"technicianInitials": "MM", "note": "Swapped during the e2e test"},
    )

    _expect_status(response, 200)
    _expect_body(response, "completedAt")


def test_delete_removes_a_product(page: Page):
    _create_product_via_api(page, "ACME-E2E-DELETE")
    page.goto("/api/docs")
    _authorize(page, ENGINEERING)

    response = _execute(
        page, "delete", "/api/products/{number}", params={"number": "ACME-E2E-DELETE"}
    )

    _expect_status(response, 204)
    token = _api_token(page, ENGINEERING)
    gone = page.request.get(
        "/api/products/ACME-E2E-DELETE", headers={"Authorization": f"Bearer {token}"}
    )
    assert gone.status == 404


# ==========================================
# Signing out
# ==========================================

def test_logout_closes_the_api_again(page: Page):
    """Both halves of signing out: the logout endpoint accepts the token, and *Logout* in
    the dialog drops it, so the next request goes out without one and ends in a 401.

    The endpoint alone could not achieve that — the JWT is stateless, so discarding it is
    the client's job, and here the client is the Swagger UI.
    """
    page.goto("/api/docs")
    _authorize(page, SALES)

    signed_out = _execute(page, "post", "/api/auth/logout")
    _expect_status(signed_out, 204)

    _logout(page)
    response = _execute(page, "get", "/api/products")

    _expect_status(response, 401)
