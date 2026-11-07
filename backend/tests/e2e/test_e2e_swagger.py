"""End-to-end test of the path a visitor of the live demo takes through the Swagger UI.

Sign in with *Authorize*, then call `GET /api/products` with *Try it out* → *Execute* —
in Chromium, against the real backend and the full demo graph. The locators target the
markup of the Swagger UI itself, so a test failing after a Swagger UI update may mean the
markup moved rather than that the backend broke; the screenshot pytest-playwright keeps
on failure (`--screenshot only-on-failure`) tells the two apart.
"""

from playwright.sync_api import Locator, Page, expect

DEMO_USER = "max.mustermann@acme.example"
DEMO_PASSWORD = "demo1234"


def _authorize(page: Page, username: str, password: str) -> None:
    """Signs in through the *Authorize* dialog and closes it again.

    Waits for the *Authorized* heading: the dialog only shows it once the token request
    has succeeded, so a rejected sign-in fails here rather than at the next request.
    """
    page.locator(".auth-wrapper button.authorize").click()
    dialog = page.locator(".modal-ux")
    dialog.locator("#oauth_username").fill(username)
    dialog.locator("#oauth_password").fill(password)
    dialog.locator("button.authorize").click()
    expect(dialog.get_by_role("heading", name="Authorized")).to_be_visible()
    dialog.locator("button.btn-done").click()
    expect(dialog).to_be_hidden()


def _execute_get(page: Page, path: str) -> Locator:
    """Opens the GET operation for `path`, runs it and returns its live response row."""
    operation = page.locator(
        f'.opblock-get:has(.opblock-summary-path[data-path="{path}"])'
    )
    operation.locator(".opblock-summary").click()
    operation.get_by_role("button", name="Try it out").click()
    operation.get_by_role("button", name="Execute").click()
    response = operation.locator(".live-responses-table .response")
    expect(response).to_be_visible()
    return response


def test_a_visitor_signs_in_with_authorize_and_lists_the_products(page: Page):
    page.goto("/api/docs")

    _authorize(page, DEMO_USER, DEMO_PASSWORD)
    response = _execute_get(page, "/api/products")

    expect(response.locator(".response-col_status")).to_contain_text("200")
    # ACME-1000 is the first product of seed.cypher, "Starter Kit A".
    expect(response.locator(".response-col_description")).to_contain_text("ACME-1000")


def test_without_authorize_the_product_list_stays_closed(page: Page):
    """The counterpart: the same click path without signing in ends in a 401, so the 200
    above is owed to the token and not to an open route."""
    page.goto("/api/docs")

    response = _execute_get(page, "/api/products")

    expect(response.locator(".response-col_status")).to_contain_text("401")
