"""API tests of the product routes.

The service layer is mocked away, so what is checked is the layer above it: that the paths
are registered, that the role gate sits where the specification says, that the 200/201
switch of the bill-of-materials endpoint works, and that query parameters arrive at the
service by name.
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from core.exceptions import BusinessLogicError, DuplicateKeyError, NotFoundError
from domains.catalog.schemas_catalog import BomLine, BomLineDeleted, Product, ProductSupplier

PRODUCT_SERVICE = "domains.catalog.router_catalog.ProductService"
BOM_SERVICE = "domains.catalog.router_catalog.BomService"
CATEGORY_SERVICE = "domains.catalog.router_catalog.CategoryService"


def _product(**overrides) -> Product:
    data: dict = {"number": "ACME-1000", "label": "Starter Kit A", "unit": "pcs"}
    data.update(overrides)
    return Product(**data)


def _new_product() -> dict:
    return {
        "number": "ACME-9000", "label": "New Thing", "unit": "pcs",
        "listPrice": "10.00", "minStock": 1, "targetStock": 5,
    }


# ==========================================
# Reading
# ==========================================

@pytest.mark.asyncio
async def test_the_product_list_is_open_to_every_signed_in_user(async_client, auth, monkeypatch):
    """The catalogue appears as a selection list in several views — reading it belongs to
    nobody in particular."""
    monkeypatch.setattr(f"{PRODUCT_SERVICE}.get_products", AsyncMock(return_value=[_product()]))

    response = await async_client.get("/api/products", headers=auth("Warehouse"))

    assert response.status_code == 200
    assert response.json()[0]["number"] == "ACME-1000"


@pytest.mark.asyncio
async def test_the_filters_reach_the_service_by_name(async_client, auth, monkeypatch):
    """A swapped keyword would silently deliver the wrong list — the one thing a route can
    get wrong without any test noticing."""
    service = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{PRODUCT_SERVICE}.get_products", service)

    await async_client.get(
        "/api/products?search=kit&type=Assembly&active=false", headers=auth("Sales")
    )

    assert service.await_args is not None
    kwargs = service.await_args.kwargs
    assert kwargs["search"] == "kit"
    assert kwargs["type"] == "Assembly"
    assert kwargs["active"] is False


@pytest.mark.asyncio
async def test_an_unknown_product_type_answers_422(async_client, auth):
    """The Literal in the signature rejects it before the service is reached — a silent
    empty list would look like "nothing matched"."""
    response = await async_client.get("/api/products?type=gadget", headers=auth("Sales"))

    assert response.status_code == 422


def _priced_product(**overrides) -> Product:
    """A product carrying both kinds of purchase price: its own cost price and the price of a
    supplier condition — for the preferred supplier the two are usually the same number."""
    return _product(
        costPrice=Decimal("745.00"),
        suppliers=[ProductSupplier(
            supplierId="S-001", leadTimeDays=5,
            purchasePrice=Decimal("745.00"), isPreferredSupplier=True,
        )],
        **overrides,
    )


@pytest.mark.asyncio
async def test_purchase_prices_are_stripped_from_the_list_for_a_role_without_them(
    async_client, auth, monkeypatch
):
    """The masking sits in the router, because it depends on the caller and not on the
    data. Both prices go together: the supplier's purchase price of the preferred supplier
    is the cost price, so stripping only one of them would hide nothing."""
    monkeypatch.setattr(
        f"{PRODUCT_SERVICE}.get_products", AsyncMock(return_value=[_priced_product()])
    )

    hidden = (await async_client.get("/api/products", headers=auth("Sales"))).json()[0]
    visible = (await async_client.get("/api/products", headers=auth("Purchasing"))).json()[0]

    assert hidden["costPrice"] is None
    assert hidden["suppliers"][0]["purchasePrice"] is None
    assert visible["costPrice"] == "745.00"
    assert visible["suppliers"][0]["purchasePrice"] == "745.00"


@pytest.mark.asyncio
async def test_purchase_prices_are_stripped_from_the_detail_for_a_role_without_them(
    async_client, auth, monkeypatch
):
    monkeypatch.setattr(
        f"{PRODUCT_SERVICE}.get_product", AsyncMock(return_value=_priced_product())
    )

    hidden = (await async_client.get("/api/products/ACME-1000", headers=auth("Warehouse"))).json()
    visible = (await async_client.get("/api/products/ACME-1000", headers=auth("Engineering"))).json()

    assert hidden["costPrice"] is None
    assert hidden["suppliers"][0]["purchasePrice"] is None
    assert visible["costPrice"] == "745.00"
    assert visible["suppliers"][0]["purchasePrice"] == "745.00"


@pytest.mark.asyncio
async def test_the_supplier_list_itself_stays_visible_without_prices(async_client, auth, monkeypatch):
    """Sales needs lead time and preferred supplier to name a delivery date — neither
    reveals what the goods cost."""
    monkeypatch.setattr(
        f"{PRODUCT_SERVICE}.get_product", AsyncMock(return_value=_priced_product())
    )

    supplier = (await async_client.get("/api/products/ACME-1000", headers=auth("Sales"))).json()[
        "suppliers"
    ][0]

    assert (supplier["supplierId"], supplier["leadTimeDays"], supplier["isPreferredSupplier"]) == (
        "S-001", 5, True,
    )


@pytest.mark.asyncio
async def test_an_unknown_product_answers_404(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{PRODUCT_SERVICE}.get_product", AsyncMock(side_effect=NotFoundError("no such product"))
    )

    response = await async_client.get("/api/products/NOPE", headers=auth("Sales"))

    assert response.status_code == 404


# ==========================================
# Writing
# ==========================================

@pytest.mark.asyncio
async def test_creating_a_product_needs_engineering(async_client, auth):
    response = await async_client.post("/api/products", json=_new_product(), headers=auth("Sales"))

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_creating_a_product_answers_201(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{PRODUCT_SERVICE}.create_product",
        AsyncMock(return_value=_product(number="ACME-9000", label="New Thing")),
    )

    response = await async_client.post(
        "/api/products", json=_new_product(), headers=auth("Engineering")
    )

    assert response.status_code == 201
    assert response.json()["number"] == "ACME-9000"


@pytest.mark.asyncio
async def test_a_duplicate_number_answers_409(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{PRODUCT_SERVICE}.create_product",
        AsyncMock(side_effect=DuplicateKeyError("number already taken")),
    )

    response = await async_client.post(
        "/api/products", json=_new_product(), headers=auth("Engineering")
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_an_empty_patch_answers_400(async_client, auth, monkeypatch):
    """A PATCH without a single field is a client error, and the message naming it comes out
    of the repository that builds the SET clause."""
    monkeypatch.setattr(
        f"{PRODUCT_SERVICE}.update_product",
        AsyncMock(side_effect=BusinessLogicError("no fields to update")),
    )

    response = await async_client.patch(
        "/api/products/ACME-1000", json={}, headers=auth("Engineering")
    )

    assert response.status_code == 400


# ==========================================
# Bill of materials
# ==========================================

@pytest.mark.asyncio
async def test_adding_a_component_answers_201(async_client, auth, monkeypatch):
    """A new line is a creation, and the status code says so."""
    monkeypatch.setattr(
        f"{BOM_SERVICE}.add_component",
        AsyncMock(return_value={
            "quantity": Decimal("2"), "component": _product(number="ACME-2001"),
            "wasUpdated": False,
        }),
    )

    response = await async_client.post(
        "/api/products/ACME-1000/bom",
        json={"componentNumber": "ACME-2001", "quantity": 2},
        headers=auth("Engineering"),
    )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_changing_a_quantity_answers_200(async_client, auth, monkeypatch):
    """The same call on an existing line only changes the quantity — 200, not 201. The
    distinction comes out of the repository, which knows whether the MERGE hit."""
    monkeypatch.setattr(
        f"{BOM_SERVICE}.add_component",
        AsyncMock(return_value={
            "quantity": Decimal("5"), "component": _product(number="ACME-2001"),
            "wasUpdated": True,
        }),
    )

    response = await async_client.post(
        "/api/products/ACME-1000/bom",
        json={"componentNumber": "ACME-2001", "quantity": 5},
        headers=auth("Engineering"),
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_bom_depth_accepts_minus_one_but_nothing_below(async_client, auth, monkeypatch):
    """`-1` is the documented value for the whole tree. `-2` is no depth at all, and
    `ge=-1` rejects it before the query is built."""
    monkeypatch.setattr(f"{BOM_SERVICE}.get_bom", AsyncMock(return_value=[]))

    whole_tree = await async_client.get(
        "/api/products/ACME-1000/bom?depth=-1", headers=auth("Engineering")
    )
    nonsense = await async_client.get(
        "/api/products/ACME-1000/bom?depth=-2", headers=auth("Engineering")
    )

    assert whole_tree.status_code == 200
    assert nonsense.status_code == 422


@pytest.mark.asyncio
async def test_reading_the_bom_returns_the_tree(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{BOM_SERVICE}.get_bom",
        AsyncMock(return_value=[
            BomLine(quantity=Decimal("2"), component=_product(number="ACME-2001"))
        ]),
    )

    response = await async_client.get("/api/products/ACME-1000/bom", headers=auth("Engineering"))

    assert response.status_code == 200
    assert response.json()[0]["component"]["number"] == "ACME-2001"


def _nested_bom() -> list[dict]:
    """A two-level tree in the shape the repository returns it: plain dicts, a full product
    per component, the sub-assembly's children under `subComponents`."""
    return [{
        "quantity": 1.0,
        "component": _priced_product(number="ACME-2004"),
        "subComponents": [{
            "quantity": 2.0,
            "component": _priced_product(number="ACME-2002"),
            "subComponents": [],
        }],
    }]


@pytest.mark.asyncio
async def test_purchase_prices_are_stripped_on_every_level_of_the_bom(async_client, auth, monkeypatch):
    """Reading a bill of materials is open to every signed-in user, and every component is a
    full product. Masking only the first level would leave the prices of every sub-assembly
    readable."""
    monkeypatch.setattr(f"{BOM_SERVICE}.get_bom", AsyncMock(return_value=_nested_bom()))

    line = (await async_client.get("/api/products/ACME-1000/bom?depth=-1", headers=auth("Sales"))).json()[0]
    child = line["subComponents"][0]

    for component in (line["component"], child["component"]):
        assert component["costPrice"] is None
        assert component["suppliers"][0]["purchasePrice"] is None
    # Structure and quantities are untouched by the masking.
    assert (line["component"]["number"], child["component"]["number"], child["quantity"]) == (
        "ACME-2004", "ACME-2002", "2.0",
    )


@pytest.mark.asyncio
async def test_a_purchasing_role_sees_the_bom_prices(async_client, auth, monkeypatch):
    monkeypatch.setattr(f"{BOM_SERVICE}.get_bom", AsyncMock(return_value=_nested_bom()))

    line = (await async_client.get("/api/products/ACME-1000/bom?depth=-1", headers=auth("Purchasing"))).json()[0]

    assert line["component"]["costPrice"] == "745.00"
    assert line["subComponents"][0]["component"]["suppliers"][0]["purchasePrice"] == "745.00"


@pytest.mark.asyncio
async def test_deleting_a_component_reports_what_is_left(async_client, auth, monkeypatch):
    monkeypatch.setattr(
        f"{BOM_SERVICE}.delete_component",
        AsyncMock(return_value=BomLineDeleted(
            number="ACME-1000", componentNumber="ACME-2001", remainingLines=3
        )),
    )

    response = await async_client.delete(
        "/api/products/ACME-1000/bom/ACME-2001", headers=auth("Engineering")
    )

    assert response.status_code == 200
    assert response.json()["remainingLines"] == 3
