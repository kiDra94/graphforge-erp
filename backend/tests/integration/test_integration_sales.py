"""Integration tests of the sales domain against a real Neo4j.

These tests answer one question: **does the Cypher do what the code claims?**
Everything that can be checked without a database — status codes, error translation, the
rounding as a pure function — lives in the API and unit tests and is deliberately not
repeated here.

The rules of the document chain live here, because none of them can be shown with mocks:

* **The stock effect per document type** — an order confirmation reserves, a delivery note
  issues, an invoice behind a delivery note does **not** issue again. Whether the
  predecessor chain is really read only the graph can show.
* **Document and booking come about together or not at all.** If the stock is not enough,
  neither the document nor a movement may exist afterwards — checked through Cypher, not
  through a status code.
* **Partial deliveries and cancellations**, whose open quantities are read from `FULFILS`
  edges rather than written forward.
* **The ordered/received comparison** with all four delivery statuses, against a purchase
  order built for the purpose.
* **The number assignment**, which reads the maximum in the same transaction.

Every test builds its data itself through Cypher. Deliberately not through the repositories
of other domains: these tests check the sales queries, not the product creation path of the
catalog.

The tests work against the **repository**, because that is where the Cypher is. Only where
the calculated amounts matter do they go through the **service** — the repository leaves
those empty on purpose.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from core.exceptions import BusinessLogicError, DuplicateKeyError, NotFoundError
from domains.assets.repository_assets import AssetRepository
from domains.assets.schemas_assets import (
    AssetCreate,
    AssetDraftConfirmation,
    AssetReleaseRequest,
    ComponentLineCreate,
)
from domains.sales.repository_sales import (
    ContractRepository,
    CustomerRepository,
    DocumentRepository,
    PriceCalculationRepository,
    ReportRepository,
)
from domains.sales.schemas_sales import (
    CancelledLine,
    ConditionCreate,
    ContractCreate,
    CustomerCreate,
    CustomerUpdate,
    DocumentCreate,
    DocumentUpdate,
    GoodsReceiptCreate,
    PriceCalculationRequest,
)
from domains.sales.service_sales import DocumentService

# ==========================================
# HELPERS
# ==========================================

async def create_master_data(session) -> None:
    """Creates the base constellation: two locations, three employees, customer, supplier.

    The location with `type: 'Warehouse'` is the fallback for lines without a location of
    their own — it is looked up by its type, not by its id.
    """
    await session.run(
        """
        MERGE (w:Location {id: '1'})  ON CREATE SET w.name = 'Central Warehouse', w.type = 'Warehouse'
        MERGE (v:Location {id: '2'})  ON CREATE SET v.name = 'Service Van 1', v.type = 'Vehicle'
        MERGE (e1:Employee {id: '1'}) ON CREATE SET e1.name = 'Max Mustermann'
        MERGE (e2:Employee {id: '2'}) ON CREATE SET e2.name = 'Erika Musterfrau'
        MERGE (e3:Employee {id: '3'}) ON CREATE SET e3.name = 'John Doe'
        MERGE (c:Customer {id: 'C-1001'}) ON CREATE SET c.name = 'Example Industries GmbH'
        MERGE (s:Supplier {id: 'S-001'})  ON CREATE SET s.name = 'Alpha Components'
        """
    )


async def create_product(
    session,
    number: str,
    label: str = "Test product",
    unit: str = "pcs",
    listPriceCent: int | None = None,
    stockEffect: str | None = None,
    costPriceCent: int | None = None,
) -> None:
    """Creates a product; the optional fields stay unset when not given."""
    await session.run(
        """
        MERGE (p:Product {number: $number})
        SET p.label = $label, p.unit = $unit
        FOREACH (_ IN CASE WHEN $listPriceCent IS NULL THEN [] ELSE [1] END |
            SET p.listPriceCent = $listPriceCent)
        FOREACH (_ IN CASE WHEN $stockEffect IS NULL THEN [] ELSE [1] END |
            SET p.stockEffect = $stockEffect)
        FOREACH (_ IN CASE WHEN $costPriceCent IS NULL THEN [] ELSE [1] END |
            SET p.costPriceCent = $costPriceCent)
        """,
        number=number,
        label=label,
        unit=unit,
        listPriceCent=listPriceCent,
        stockEffect=stockEffect,
        costPriceCent=costPriceCent,
    )


async def create_stock(
    session, productNumber: str, locationId: str, quantity: float, reserved: float = 0.0
) -> None:
    """Creates a stock record — one node per product and location, as in the seed."""
    await session.run(
        """
        MATCH (p:Product  {number: $productNumber})
        MATCH (l:Location {id: $locationId})
        MERGE (s:StockLevel {id: $productNumber + '_' + $locationId})
        SET s.quantity = $quantity, s.reserved = $reserved
        MERGE (p)-[:HAS_STOCK]->(s)
        MERGE (s)-[:AT_LOCATION]->(l)
        """,
        productNumber=productNumber,
        locationId=locationId,
        quantity=quantity,
        reserved=reserved,
    )


async def create_order(session, projectNumber: str, customerId: str = "C-1001") -> None:
    """Creates an order as the bracket over the document chain."""
    await session.run(
        """
        MATCH (c:Customer {id: $customerId})
        MERGE (o:Order {projectNumber: $projectNumber})
        MERGE (c)-[:HAS_ORDER]->(o)
        """,
        projectNumber=projectNumber,
        customerId=customerId,
    )


async def create_raw_document(
    session,
    number: str,
    type: str,
    *,
    status: str = "open",
    customerId: str | None = None,
    supplierId: str | None = None,
    basedOn: str | None = None,
    documentDate: str = "2026-02-10",
) -> None:
    """Creates a document directly in the graph, bypassing the repository.

    The type label is set along with it, the way the repository does — without it the
    stock would fall apart into two kinds.
    """
    await session.run(
        f"""
        MERGE (d:Document:{type} {{number: $number}})
        SET d.type = $type, d.status = $status, d.date = date($documentDate), d.language = 'DE'
        WITH d
        OPTIONAL MATCH (c:Customer {{id: $customerId}})
        FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END |
            MERGE (d)-[:BELONGS_TO_CUSTOMER]->(c))
        WITH d
        OPTIONAL MATCH (s:Supplier {{id: $supplierId}})
        FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
            MERGE (d)-[:BELONGS_TO_SUPPLIER]->(s))
        WITH d
        OPTIONAL MATCH (v:Document {{number: $basedOn}})
        FOREACH (_ IN CASE WHEN v IS NULL THEN [] ELSE [1] END |
            MERGE (d)-[:BASED_ON]->(v))
        """,
        number=number,
        type=type,
        status=status,
        documentDate=documentDate,
        customerId=customerId,
        supplierId=supplierId,
        basedOn=basedOn,
    )


async def create_raw_line(
    session,
    documentNumber: str,
    productNumber: str,
    quantity: float,
    unitPriceCent: int,
    discountPercent: float = 0.0,
    *,
    lineNumber: int,
    sortOrder: int | None = None,
) -> None:
    """Hangs a line off an existing document.

    `lineNumber` is the key component (`documentNumber_lineNumber`) and has to be unique per
    document. `sortOrder` usually stays unset and is then derived from the line number the
    way the repository does it; set explicitly, it lets a test reproduce a line inserted
    between two existing ones.
    """
    await session.run(
        """
        MATCH (d:Document {number: $documentNumber})
        MATCH (p:Product  {number: $productNumber})
        MERGE (l:DocumentLine {id: $documentNumber + '_' + toString($lineNumber)})
        SET l.lineNumber      = $lineNumber,
            l.sortOrder       = $sortOrder,
            l.quantity        = $quantity,
            l.unitPriceCent   = $unitPriceCent,
            l.discountPercent = $discountPercent,
            l.priceOverridden = false
        MERGE (d)-[:HAS_LINE]->(l)
        MERGE (l)-[:OF_PRODUCT]->(p)
        """,
        documentNumber=documentNumber,
        productNumber=productNumber,
        quantity=quantity,
        unitPriceCent=unitPriceCent,
        discountPercent=discountPercent,
        lineNumber=lineNumber,
        sortOrder=sortOrder if sortOrder is not None else lineNumber * 10,
    )


async def read_stock(session, productNumber: str, locationId: str = "1") -> tuple[float, float]:
    """Reads quantity and reserved quantity of a stock record."""
    result = await session.run(
        "MATCH (s:StockLevel {id: $id}) RETURN s.quantity AS quantity, s.reserved AS reserved",
        id=f"{productNumber}_{locationId}",
    )
    record = await result.single()
    return (record["quantity"], record["reserved"]) if record else (0.0, 0.0)


async def movements_of(session, documentNumber: str) -> list[dict]:
    """Reads every movement hanging off a document through BASED_ON_DOCUMENT."""
    result = await session.run(
        """
        MATCH (m:StockMovement)-[:BASED_ON_DOCUMENT]->(:Document {number: $documentNumber})
        OPTIONAL MATCH (p:Product)-[:HAS_STOCK]->(:StockLevel)<-[:POSTED_TO]-(m)
        RETURN m.type              AS type,
               m.quantity          AS quantity,
               m.purchasePriceCent AS purchasePriceCent,
               p.number            AS productNumber
        ORDER BY productNumber, type
        """,
        documentNumber=documentNumber,
    )
    return [dict(record) async for record in result]


async def count(session, cypher: str, **params) -> int:
    """Runs a counting query and returns the number."""
    result = await session.run(cypher, **params)
    record = await result.single()
    return record[0] if record else 0


async def labels_of(session, number: str) -> set[str]:
    """Reads the labels of a document node."""
    result = await session.run(
        "MATCH (d:Document {number: $number}) RETURN labels(d) AS labels", number=number
    )
    record = await result.single()
    return set(record["labels"]) if record else set()


def new_document(**overrides) -> DocumentCreate:
    """Builds a valid creation request; single fields can be overridden.

    Fills in the fields a document type makes mandatory: `assetPurpose` on a quote,
    `deliveryDate` on an order confirmation, and the supplier instead of the customer on a
    purchasing document.
    """
    data: dict = {
        "type": "OrderConfirmation",
        "customerId": "C-1001",
        "lines": [{"productNumber": "ACME-2007", "quantity": 3, "unitPrice": "4200.00"}],
    }
    data.update(overrides)
    if data["type"] == "Quote" and "assetPurpose" not in data:
        data["assetPurpose"] = "newAsset"
    if data["type"] == "OrderConfirmation" and "deliveryDate" not in data:
        data["deliveryDate"] = "2026-09-01"
    if data["type"] in ("PurchaseOrder", "GoodsReceipt") and "supplierId" not in data:
        data["customerId"] = None
        data["supplierId"] = "S-001"
    return DocumentCreate.model_validate(data)


async def create_another_customer(session, id: str, name: str) -> None:
    """Creates an additional customer outside the base constellation."""
    await session.run("MERGE (c:Customer {id: $id}) SET c.name = $name", id=id, name=name)


def today() -> date:
    return date.today()


async def create_contract(
    session,
    id: str,
    name: str,
    isGlobal: bool,
    validFrom: date | None = None,
    validTo: date | None = None,
    discountPercent: float | None = None,
) -> None:
    """Creates a framework contract, optionally with a flat discount rate.

    Without explicit dates the contract is valid today — relative to the day the test runs,
    so the tests do not start failing once a fixed year has passed.
    """
    await session.run(
        """
        MERGE (ct:Contract {id: $id})
        SET ct.name = $name, ct.isGlobal = $isGlobal,
            ct.validFrom = $validFrom, ct.validTo = $validTo,
            ct.discountPercent = $discountPercent
        """,
        id=id, name=name, isGlobal=isGlobal,
        validFrom=validFrom or today() - timedelta(days=365),
        validTo=validTo or today() + timedelta(days=365),
        discountPercent=discountPercent,
    )


async def create_condition(
    session, contractId: str, productNumber: str, fixedPriceCent: int | None = None
) -> None:
    """Hangs a condition (fixed price) off a contract."""
    await session.run(
        """
        MATCH (ct:Contract {id: $contractId})
        MATCH (p:Product {number: $productNumber})
        MERGE (ct)-[cf:CONDITION_FOR]->(p)
        SET cf.fixedPriceCent = $fixedPriceCent
        """,
        contractId=contractId, productNumber=productNumber, fixedPriceCent=fixedPriceCent,
    )


async def link_customer_contract(session, customerId: str, contractId: str) -> None:
    """Links a customer directly with a contract, bypassing the repository."""
    await session.run(
        "MATCH (c:Customer {id: $customerId}) MATCH (ct:Contract {id: $contractId}) "
        "MERGE (c)-[:HAS_CONTRACT]->(ct)",
        customerId=customerId, contractId=contractId,
    )


async def create_discount(
    session,
    id: str,
    value: float,
    validFrom: str,
    validTo: str,
    productNumber: str | None = None,
    productGroupId: int | None = None,
) -> None:
    """Creates a `Discount` node, either on a product or on a product group."""
    await session.run(
        """
        MERGE (d:Discount {id: $id})
        SET d.value = $value, d.validFrom = date($validFrom), d.validTo = date($validTo)
        WITH d
        OPTIONAL MATCH (p:Product {number: $productNumber})
        FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | MERGE (d)-[:APPLIES_TO_PRODUCT]->(p))
        WITH d
        OPTIONAL MATCH (g:ProductGroup {id: $productGroupId})
        FOREACH (_ IN CASE WHEN g IS NULL THEN [] ELSE [1] END | MERGE (d)-[:APPLIES_TO_GROUP]->(g))
        """,
        id=id, value=value, validFrom=validFrom, validTo=validTo,
        productNumber=productNumber, productGroupId=productGroupId,
    )


async def create_priced_product(
    session,
    number: str,
    listPriceCent: int,
    discountable: bool | None = True,
    label: str = "Test product",
) -> None:
    """Creates a product with a sales price, the way the price calculation needs it.

    `discountable=None` does not set the property at all (Cypher removes it on a
    `SET … = null`) — the real case of a missing value, not a stored `false`.
    """
    await session.run(
        """
        MERGE (p:Product {number: $number})
        SET p.label = $label, p.listPriceCent = $listPriceCent, p.discountable = $discountable
        """,
        number=number, label=label, listPriceCent=listPriceCent, discountable=discountable,
    )


async def assign_product_group(
    session, productNumber: str, subcategoryId: int, productGroupId: int
) -> None:
    """Hangs a product off a product group over a subcategory.

    Two hops, as in the real model: `(:Product)-[:BELONGS_TO]->(:Subcategory)
    -[:PART_OF]->(:ProductGroup)`. There is no direct edge.
    """
    await session.run(
        """
        MATCH (p:Product {number: $productNumber})
        MERGE (s:Subcategory {id: $subcategoryId})
        MERGE (g:ProductGroup {id: $productGroupId})
        MERGE (p)-[:BELONGS_TO]->(s)
        MERGE (s)-[:PART_OF]->(g)
        """,
        productNumber=productNumber, subcategoryId=subcategoryId, productGroupId=productGroupId,
    )


# ==========================================
# CREATING CUSTOMERS
# ==========================================

@pytest.mark.asyncio
async def test_a_customer_gets_a_prefixed_uuid_number(neo4j_session):
    # No counter: it would have to read the maximum before every write, and two concurrent
    # creations would compute the same number.
    await create_master_data(neo4j_session)

    created = await CustomerRepository.create_customer(
        CustomerCreate(name="New Railways AG"), neo4j_session
    )

    assert created.id.startswith("C-")
    assert created.id != "C-1001"


@pytest.mark.asyncio
async def test_two_customers_get_different_numbers(neo4j_session):
    await create_master_data(neo4j_session)

    first = await CustomerRepository.create_customer(CustomerCreate(name="First"), neo4j_session)
    second = await CustomerRepository.create_customer(CustomerCreate(name="Second"), neo4j_session)

    assert first.id != second.id


@pytest.mark.asyncio
async def test_creating_sets_createdat_but_no_updatedat(neo4j_session):
    # An updatedAt on creation would claim a change that never happened. It stays empty
    # until the first PATCH arrives.
    await create_master_data(neo4j_session)

    created = await CustomerRepository.create_customer(
        CustomerCreate(name="New Railways AG"), neo4j_session
    )

    assert created.createdAt is not None
    assert created.updatedAt is None


@pytest.mark.asyncio
async def test_creating_with_a_name_only_leaves_the_other_fields_empty(neo4j_session):
    # `SET c += $props` does not create a property with the value null in the first place —
    # the customer gets no empty placeholders on its node.
    await create_master_data(neo4j_session)

    created = await CustomerRepository.create_customer(
        CustomerCreate(name="New Railways AG"), neo4j_session
    )

    assert (created.street, created.city, created.country, created.vatId) == (None,) * 4
    assert created.accountManager is None


@pytest.mark.asyncio
async def test_an_account_manager_id_draws_the_edge(neo4j_session):
    # The account manager is a relationship, not a property: a change is a change of
    # assignment, not a changed value.
    await create_master_data(neo4j_session)

    created = await CustomerRepository.create_customer(
        CustomerCreate(name="New Railways AG", accountManagerId="1"), neo4j_session
    )

    assert created.accountManager is not None
    assert created.accountManager.name == "Max Mustermann"
    assert await count(
        neo4j_session,
        "MATCH (:Employee)-[r:ACCOUNT_MANAGER_OF]->(:Customer {id: $id}) RETURN count(r)",
        id=created.id,
    ) == 1


@pytest.mark.asyncio
async def test_an_unknown_account_manager_prevents_the_creation(neo4j_session):
    # Entirely: a customer without the requested account manager would be a partial result
    # nobody asked for.
    await create_master_data(neo4j_session)

    with pytest.raises(NotFoundError):
        await CustomerRepository.create_customer(
            CustomerCreate(name="New Railways AG", accountManagerId="99"), neo4j_session
        )

    assert await count(
        neo4j_session, "MATCH (c:Customer {name: 'New Railways AG'}) RETURN count(c)"
    ) == 0


# ==========================================
# READING, SEARCHING AND CHANGING CUSTOMERS
# ==========================================

@pytest.mark.asyncio
async def test_a_customer_with_only_number_and_name_is_readable(neo4j_session):
    # An import may deliver no more than that. One more mandatory field would turn this
    # entirely normal state into a 500.
    await create_master_data(neo4j_session)

    read = await CustomerRepository.get_customer("C-1001", neo4j_session)

    assert read is not None
    assert read.name == "Example Industries GmbH"
    assert read.street is None


@pytest.mark.asyncio
async def test_an_unknown_customer_number_returns_none(neo4j_session):
    await create_master_data(neo4j_session)

    assert await CustomerRepository.get_customer("C-9999", neo4j_session) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("example", "C-1001"),
        ("c-1001", "C-1001"),
        ("vienna", "C-2000"),
        ("ATU00000042", "C-2000"),
    ],
)
async def test_the_search_matches_all_four_fields(neo4j_session, term, expected):
    # Name, number, city and VAT id — sales searches by every one of them.
    await create_master_data(neo4j_session)
    await neo4j_session.run(
        "CREATE (:Customer {id: 'C-2000', name: 'New Railways AG', city: '1010 Vienna', vatId: 'ATU00000042'})"
    )

    hits = await CustomerRepository.get_customers(neo4j_session, search=term)

    assert [customer.id for customer in hits] == [expected]


@pytest.mark.asyncio
async def test_a_blank_search_returns_everyone(neo4j_session):
    # A cleared search field arrives as whitespace. Read as a filter it would be a "contains
    # nothing" — the list would look empty although nothing was searched for.
    await create_master_data(neo4j_session)

    assert len(await CustomerRepository.get_customers(neo4j_session, search="   ")) == 1


@pytest.mark.asyncio
async def test_a_patch_writes_only_the_fields_sent(neo4j_session):
    await create_master_data(neo4j_session)
    created = await CustomerRepository.create_customer(
        CustomerCreate(name="New Railways AG", city="1010 Vienna", vatId="ATU00000042"),
        neo4j_session,
    )

    changed = await CustomerRepository.update_customer(
        created.id, CustomerUpdate(city="4020 Linz"), neo4j_session
    )

    assert changed is not None
    assert changed.city == "4020 Linz"
    assert changed.vatId == "ATU00000042"
    assert changed.name == "New Railways AG"
    assert changed.updatedAt is not None


@pytest.mark.asyncio
async def test_a_patch_without_any_field_is_rejected(neo4j_session):
    await create_master_data(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await CustomerRepository.update_customer("C-1001", CustomerUpdate(), neo4j_session)


@pytest.mark.asyncio
async def test_changing_the_account_manager_leaves_exactly_one_edge(neo4j_session):
    # A MERGE without the DELETE before it would hang a second account manager beside the
    # first, and the customer would appear twice in the list.
    await create_master_data(neo4j_session)
    created = await CustomerRepository.create_customer(
        CustomerCreate(name="New Railways AG", accountManagerId="1"), neo4j_session
    )

    changed = await CustomerRepository.update_customer(
        created.id, CustomerUpdate(accountManagerId="2"), neo4j_session
    )

    assert changed is not None
    assert changed.accountManager is not None
    assert changed.accountManager.id == "2"
    assert await count(
        neo4j_session,
        "MATCH (:Employee)-[r:ACCOUNT_MANAGER_OF]->(:Customer {id: $id}) RETURN count(r)",
        id=created.id,
    ) == 1


@pytest.mark.asyncio
async def test_an_explicit_null_dissolves_the_account_manager(neo4j_session):
    # The only way to run a customer without an account manager again.
    await create_master_data(neo4j_session)
    created = await CustomerRepository.create_customer(
        CustomerCreate(name="New Railways AG", accountManagerId="1"), neo4j_session
    )

    dissolved = await CustomerRepository.update_customer(
        created.id, CustomerUpdate(accountManagerId=None), neo4j_session
    )

    assert dissolved is not None
    assert dissolved.accountManager is None


@pytest.mark.asyncio
async def test_a_patch_on_an_unknown_customer_returns_none(neo4j_session):
    await create_master_data(neo4j_session)

    assert await CustomerRepository.update_customer(
        "C-9999", CustomerUpdate(city="1010 Vienna"), neo4j_session
    ) is None


# ==========================================
# CustomerRepository.assign_contract
# ==========================================

@pytest.mark.asyncio
async def test_assigning_a_contract_draws_the_edge(neo4j_session):
    await create_master_data(neo4j_session)
    await create_contract(neo4j_session, "1", "Test contract", isGlobal=False)

    customer = await CustomerRepository.assign_contract("C-1001", "1", neo4j_session)

    assert customer is not None
    assert await count(
        neo4j_session,
        "MATCH (:Customer {id: 'C-1001'})-[r:HAS_CONTRACT]->(:Contract {id: '1'}) RETURN count(r)",
    ) == 1


@pytest.mark.asyncio
async def test_assigning_a_contract_is_idempotent(neo4j_session):
    # Twice with the same combination yields one edge, not two.
    await create_master_data(neo4j_session)
    await create_contract(neo4j_session, "1", "Test contract", isGlobal=False)

    await CustomerRepository.assign_contract("C-1001", "1", neo4j_session)
    await CustomerRepository.assign_contract("C-1001", "1", neo4j_session)

    assert await count(
        neo4j_session,
        "MATCH (:Customer {id: 'C-1001'})-[r:HAS_CONTRACT]->(:Contract {id: '1'}) RETURN count(r)",
    ) == 1


@pytest.mark.asyncio
async def test_assigning_a_global_contract_is_rejected(neo4j_session):
    # A global contract already applies to every customer without an assignment — an edge
    # would contradict that.
    await create_master_data(neo4j_session)
    await create_contract(neo4j_session, "2", "Global campaign", isGlobal=True)

    with pytest.raises(BusinessLogicError):
        await CustomerRepository.assign_contract("C-1001", "2", neo4j_session)


@pytest.mark.asyncio
async def test_assigning_to_an_unknown_customer_returns_none(neo4j_session):
    await create_contract(neo4j_session, "1", "Test contract", isGlobal=False)

    assert await CustomerRepository.assign_contract("C-9999", "1", neo4j_session) is None


@pytest.mark.asyncio
async def test_assigning_an_unknown_contract_is_not_found(neo4j_session):
    await create_master_data(neo4j_session)

    with pytest.raises(NotFoundError):
        await CustomerRepository.assign_contract("C-1001", "99", neo4j_session)


# ==========================================
# CustomerRepository.get_customer — the contract axis (effectiveDiscountPercent)
# ==========================================
# The customer discount hangs exclusively off the contract. If a customer has several
# contracts valid today at the same time — an own one through HAS_CONTRACT and/or a global
# one —, the higher rate wins.

@pytest.mark.asyncio
async def test_get_customer_the_global_contract_rate_wins(neo4j_session):
    await create_another_customer(neo4j_session, "C-9001", "Customer global wins")
    await create_contract(neo4j_session, "own-9001", "Own contract", isGlobal=False, discountPercent=3.0)
    await link_customer_contract(neo4j_session, "C-9001", "own-9001")
    await create_contract(neo4j_session, "global-9001", "Global campaign", isGlobal=True, discountPercent=4.0)

    customer = await CustomerRepository.get_customer("C-9001", neo4j_session)

    assert customer is not None
    assert customer.effectiveDiscountPercent == 4.0


@pytest.mark.asyncio
async def test_get_customer_the_own_contract_rate_wins(neo4j_session):
    await create_another_customer(neo4j_session, "C-9002", "Customer own wins")
    await create_contract(neo4j_session, "own-9002", "Own contract", isGlobal=False, discountPercent=5.0)
    await link_customer_contract(neo4j_session, "C-9002", "own-9002")
    await create_contract(neo4j_session, "global-9002", "Global campaign", isGlobal=True, discountPercent=1.0)

    customer = await CustomerRepository.get_customer("C-9002", neo4j_session)

    assert customer is not None
    assert customer.effectiveDiscountPercent == 5.0


@pytest.mark.asyncio
async def test_get_customer_a_maintained_zero_stays_distinguishable_from_none(neo4j_session):
    # 0.0 is a maintained rate (no discount agreed), not "not maintained".
    await create_another_customer(neo4j_session, "C-9003", "Customer zero")
    await create_contract(neo4j_session, "c-9003", "Contract without discount", isGlobal=False, discountPercent=0.0)
    await link_customer_contract(neo4j_session, "C-9003", "c-9003")

    customer = await CustomerRepository.get_customer("C-9003", neo4j_session)

    assert customer is not None
    assert customer.effectiveDiscountPercent == 0.0


@pytest.mark.asyncio
async def test_get_customer_an_expired_contract_does_not_count(neo4j_session):
    # "Valid today" is the condition, not "exists".
    await create_another_customer(neo4j_session, "C-9004", "Customer expired")
    await create_contract(
        neo4j_session, "c-9004", "Expired contract", isGlobal=False, discountPercent=9.0,
        validFrom=date(2024, 1, 1), validTo=date(2024, 12, 31),
    )
    await link_customer_contract(neo4j_session, "C-9004", "c-9004")

    customer = await CustomerRepository.get_customer("C-9004", neo4j_session)

    assert customer is not None
    assert customer.effectiveDiscountPercent is None


@pytest.mark.asyncio
async def test_get_customer_without_a_contract_is_none(neo4j_session):
    await create_master_data(neo4j_session)

    customer = await CustomerRepository.get_customer("C-1001", neo4j_session)

    assert customer is not None
    assert customer.effectiveDiscountPercent is None


@pytest.mark.asyncio
async def test_the_customer_list_does_not_carry_the_rate(neo4j_session):
    # GET /api/customers stays unchanged — the value is only needed on the single customer.
    await create_another_customer(neo4j_session, "C-9005", "Customer list")
    await create_contract(neo4j_session, "c-9005", "Contract", isGlobal=False, discountPercent=4.0)
    await link_customer_contract(neo4j_session, "C-9005", "c-9005")

    customers = await CustomerRepository.get_customers(neo4j_session)

    hit = next(c for c in customers if c.id == "C-9005")
    assert hit.effectiveDiscountPercent is None


# ==========================================
# DOCUMENT NUMBERS AND ORDERS
# ==========================================

@pytest.mark.asyncio
async def test_a_document_with_an_order_carries_its_project_number(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_order(neo4j_session, "2026-0734")

    created = await DocumentRepository.create_document(
        new_document(type="Quote", orderProjectNumber="2026-0734"), "1", neo4j_session
    )

    assert created.number == "QU-2026-0734"


@pytest.mark.asyncio
async def test_a_sales_document_without_an_order_gets_a_new_one_with_all_edges(neo4j_session):
    # There is no separate place that hands out orders — the sales document creates its
    # order in the same transaction as itself.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    year = datetime.now(UTC).year

    created = await DocumentRepository.create_document(
        new_document(type="Quote", deliveryDate="2026-09-01"), "1", neo4j_session
    )

    assert created.orderProjectNumber == f"{year}-0001"
    assert created.number == f"QU-{year}-0001"
    result = await neo4j_session.run(
        """
        MATCH (c:Customer {id: 'C-1001'})-[:HAS_ORDER]->(o:Order {projectNumber: $projectNumber})
        MATCH (d:Document {number: $number})-[:BELONGS_TO_ORDER]->(o)
        RETURN o.year AS year, o.deliveryDate AS deliveryDate
        """,
        projectNumber=created.orderProjectNumber, number=created.number,
    )
    record = await result.single()
    assert record is not None
    assert record["year"] == year
    assert str(record["deliveryDate"]) == "2026-09-01"


@pytest.mark.asyncio
async def test_an_order_without_a_delivery_date_stays_without_one(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    created = await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)

    result = await neo4j_session.run(
        "MATCH (o:Order {projectNumber: $projectNumber}) RETURN o.deliveryDate AS deliveryDate",
        projectNumber=created.orderProjectNumber,
    )
    record = await result.single()
    assert record is not None
    assert record["deliveryDate"] is None


@pytest.mark.asyncio
async def test_the_order_sequence_counts_across_customers(neo4j_session):
    # The sequence counts within the year over every customer — a second customer does not
    # start again at 1.
    await create_master_data(neo4j_session)
    await create_another_customer(neo4j_session, "C-1002", "Sample Logistics AG")
    await create_product(neo4j_session, "ACME-2007")

    first = await DocumentRepository.create_document(
        new_document(type="Quote", customerId="C-1001"), "1", neo4j_session
    )
    second = await DocumentRepository.create_document(
        new_document(type="Quote", customerId="C-1002"), "1", neo4j_session
    )

    assert first.orderProjectNumber is not None and second.orderProjectNumber is not None
    first_sequence = int(first.orderProjectNumber.rsplit("-", 1)[1])
    second_sequence = int(second.orderProjectNumber.rsplit("-", 1)[1])
    assert second_sequence == first_sequence + 1


@pytest.mark.asyncio
async def test_an_existing_order_moves_the_next_sequence_number(neo4j_session):
    # The counter reads the maximum over ALL orders of the year, imported ones included.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    year = datetime.now(UTC).year
    await create_order(neo4j_session, f"{year}-0734")

    created = await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)

    assert created.orderProjectNumber == f"{year}-0735"


@pytest.mark.asyncio
async def test_the_order_sequence_skips_numbers_taken_by_documents_of_the_year(neo4j_session):
    # A purchase order numbered from the yearly counter occupies the same slot a project
    # number would claim. Counting orders alone would hand out 2026-0001 and collide with
    # a document number already taken as soon as a follow-up used it.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    year = datetime.now(UTC).year
    await create_raw_document(neo4j_session, f"PO-{year}-0005", "PurchaseOrder", supplierId="S-001")

    created = await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)

    assert created.orderProjectNumber == f"{year}-0006"


@pytest.mark.asyncio
async def test_a_purchasing_document_gets_no_automatic_order(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    created = await DocumentRepository.create_document(
        new_document(type="PurchaseOrder"), "1", neo4j_session
    )

    assert created.orderProjectNumber is None
    assert await count(neo4j_session, "MATCH (o:Order) RETURN count(o)") == 0


@pytest.mark.asyncio
async def test_a_given_project_number_creates_no_second_order(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_order(neo4j_session, "2026-0734")

    await DocumentRepository.create_document(
        new_document(type="Quote", orderProjectNumber="2026-0734"), "1", neo4j_session
    )

    assert await count(neo4j_session, "MATCH (o:Order) RETURN count(o)") == 1


@pytest.mark.asyncio
async def test_an_unknown_project_number_is_not_found(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    with pytest.raises(NotFoundError):
        await DocumentRepository.create_document(
            new_document(type="Quote", orderProjectNumber="1999-9999"), "1", neo4j_session
        )


@pytest.mark.asyncio
async def test_the_yearly_count_of_purchasing_documents_runs_per_type(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    year = datetime.now(UTC).year

    await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)
    purchase_order = await DocumentRepository.create_document(
        new_document(type="PurchaseOrder"), "1", neo4j_session
    )

    assert purchase_order.number == f"PO-{year}-0001"


@pytest.mark.asyncio
async def test_a_second_document_of_the_same_type_on_an_order_collides(neo4j_session):
    # A consequence of the format <PREFIX>-<projectNumber>: per order and document type
    # (except delivery note and invoice) exactly one document is possible. The uniqueness
    # constraint catches the second one.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_order(neo4j_session, "2026-0734")
    await DocumentRepository.create_document(
        new_document(type="Quote", orderProjectNumber="2026-0734"), "1", neo4j_session
    )

    with pytest.raises(DuplicateKeyError):
        await DocumentRepository.create_document(
            new_document(type="Quote", orderProjectNumber="2026-0734"), "1", neo4j_session
        )


@pytest.mark.asyncio
async def test_a_second_delivery_note_on_the_same_order_gets_a_suffix(neo4j_session):
    # Delivery notes and invoices may come about several times per order. The first stays
    # without a suffix — a number already printed must never change.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)
    await create_order(neo4j_session, "2026-0734")

    first = await DocumentRepository.create_document(
        new_document(type="DeliveryNote", orderProjectNumber="2026-0734"), "1", neo4j_session
    )
    second = await DocumentRepository.create_document(
        new_document(type="DeliveryNote", orderProjectNumber="2026-0734"), "1", neo4j_session
    )
    third = await DocumentRepository.create_document(
        new_document(type="DeliveryNote", orderProjectNumber="2026-0734"), "1", neo4j_session
    )

    assert (first.number, second.number, third.number) == (
        "DN-2026-0734", "DN-2026-0734-2", "DN-2026-0734-3",
    )


# ==========================================
# CREATING A DOCUMENT
# ==========================================

@pytest.mark.asyncio
async def test_the_new_document_carries_its_type_label(neo4j_session):
    # Without it the stock falls apart into two kinds, and every query over the label
    # silently overlooks the new documents.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    created = await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)

    assert await labels_of(neo4j_session, created.number) == {"Document", "Quote"}


@pytest.mark.asyncio
async def test_the_new_document_starts_open_with_todays_date(neo4j_session):
    # The document date is the day of creation and does not come from the request: it is
    # printed on the document.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    created = await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)

    assert created.status == "open"
    assert created.date == datetime.now(UTC).date()
    assert created.createdAt is not None


@pytest.mark.asyncio
async def test_a_new_order_confirmation_starts_completed(neo4j_session):
    # An order confirmation IS the confirmation of the order — no second click is needed
    # before the warehouse sees it as an open request to deliver.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=100.0)

    created = await DocumentRepository.create_document(new_document(), "1", neo4j_session)

    assert created.status == "completed"


@pytest.mark.asyncio
async def test_a_freshly_created_order_confirmation_is_locked_in_substance(neo4j_session):
    # The direct consequence of status='completed' from creation: an order confirmation
    # entered wrongly needs the same correction path as every other concluded document —
    # cancel and create anew, no content correction by PATCH.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=100.0)
    created = await DocumentRepository.create_document(new_document(), "1", neo4j_session)

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            created.number, DocumentUpdate(deliveryDate=date(2026, 10, 1)), "1", neo4j_session
        )


@pytest.mark.asyncio
async def test_the_document_hangs_off_customer_employee_order_and_predecessor(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=100.0)
    await create_order(neo4j_session, "2026-0734")
    await create_raw_document(neo4j_session, "QU-2026-0734", "Quote", customerId="C-1001")

    created = await DocumentRepository.create_document(
        new_document(orderProjectNumber="2026-0734", basedOn=["QU-2026-0734"]),
        "1", neo4j_session,
    )

    assert created.customer is not None
    assert created.customer.id == "C-1001"
    assert created.createdBy is not None
    assert created.createdBy.name == "Max Mustermann"
    assert created.basedOn == ["QU-2026-0734"]
    assert created.orderProjectNumber == "2026-0734"
    assert created.supplier is None


@pytest.mark.asyncio
async def test_the_customer_address_is_copied_onto_the_document(neo4j_session):
    # A later move of the customer must not change retroactively what is printed on
    # documents already issued.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await neo4j_session.run(
        "MATCH (c:Customer {id: 'C-1001'}) SET c.street = 'Musterstrasse 1', c.city = '1010 Vienna'"
    )
    created = await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)

    await CustomerRepository.update_customer(
        "C-1001", CustomerUpdate(name="Renamed GmbH", city="4020 Linz"), neo4j_session
    )
    document = await DocumentRepository.get_document(created.number, neo4j_session)

    assert document is not None
    assert document.customer is not None
    assert document.customer.name == "Example Industries GmbH"
    assert document.customer.city == "1010 Vienna"


@pytest.mark.asyncio
async def test_a_differing_address_on_the_request_wins_over_the_customer_master(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    created = await DocumentRepository.create_document(
        new_document(type="Quote", customerAddress={"city": "8010 Graz"}), "1", neo4j_session
    )

    assert created.customer is not None
    assert created.customer.city == "8010 Graz"
    assert created.customer.name == "Example Industries GmbH"


@pytest.mark.asyncio
async def test_a_new_document_closes_its_open_predecessor(neo4j_session):
    # Previous documents are locked once a follow-up document exists — through basedOn on
    # creation, not only on a cancellation.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=100.0)
    await create_order(neo4j_session, "2026-0734")
    await create_raw_document(neo4j_session, "QU-2026-0734", "Quote", customerId="C-1001")

    await DocumentRepository.create_document(
        new_document(orderProjectNumber="2026-0734", basedOn=["QU-2026-0734"]),
        "1", neo4j_session,
    )

    predecessor = await DocumentRepository.get_document("QU-2026-0734", neo4j_session)
    assert predecessor is not None
    assert predecessor.status == "completed"


@pytest.mark.asyncio
async def test_the_closed_predecessor_is_locked_in_substance(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=100.0)
    await create_order(neo4j_session, "2026-0734")
    await create_raw_document(neo4j_session, "QU-2026-0734", "Quote", customerId="C-1001")
    await DocumentRepository.create_document(
        new_document(orderProjectNumber="2026-0734", basedOn=["QU-2026-0734"]),
        "1", neo4j_session,
    )

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            "QU-2026-0734", DocumentUpdate(deliveryDate=date(2026, 9, 1)), "1", neo4j_session
        )


@pytest.mark.asyncio
async def test_an_unknown_predecessor_prevents_the_document(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    with pytest.raises(NotFoundError):
        await DocumentRepository.create_document(
            new_document(type="Quote", basedOn=["QU-1999-9999"]), "1", neo4j_session
        )

    assert await count(neo4j_session, "MATCH (d:Document) RETURN count(d)") == 0


@pytest.mark.asyncio
async def test_a_purchasing_document_hangs_off_the_supplier(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    created = await DocumentRepository.create_document(
        new_document(type="PurchaseOrder"), "1", neo4j_session
    )

    assert created.customer is None
    assert created.supplier is not None
    assert created.supplier.name == "Alpha Components"


@pytest.mark.asyncio
async def test_the_lines_are_stored_as_cents(neo4j_session):
    # Money sits in the graph as integer cents. A float would still be inconspicuous at
    # 4,200.00 EUR and no longer when summing.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    created = await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)

    result = await neo4j_session.run(
        """
        MATCH (:Document {number: $number})-[:HAS_LINE]->(l:DocumentLine)
        RETURN l.id AS id, l.unitPriceCent AS cent
        """,
        number=created.number,
    )
    row = await result.single()
    assert row is not None
    assert row["cent"] == 420000
    assert row["id"] == f"{created.number}_1"


@pytest.mark.asyncio
async def test_the_same_product_may_appear_twice_on_a_sales_document(neo4j_session):
    # A line is keyed by document number and line number, not by product number — once
    # regular, once as a goodwill line at a different price.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    created = await DocumentRepository.create_document(
        new_document(
            type="Quote",
            lines=[
                {"productNumber": "ACME-2007", "quantity": 2, "unitPrice": "10.00"},
                {"productNumber": "ACME-2007", "quantity": 1, "unitPrice": "0.00"},
            ],
        ),
        "1", neo4j_session,
    )

    assert [(line.lineNumber, line.quantity) for line in created.lines] == [(1, 2), (2, 1)]


@pytest.mark.asyncio
async def test_an_unknown_product_prevents_the_whole_document(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    with pytest.raises(NotFoundError):
        await DocumentRepository.create_document(
            new_document(
                type="Quote",
                lines=[
                    {"productNumber": "ACME-2007", "quantity": 1, "unitPrice": "1.00"},
                    {"productNumber": "does-not-exist", "quantity": 1, "unitPrice": "1.00"},
                ],
            ),
            "1", neo4j_session,
        )

    assert await count(neo4j_session, "MATCH (d:Document) RETURN count(d)") == 0
    assert await count(neo4j_session, "MATCH (o:Order) RETURN count(o)") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("type", ["Quote", "PurchaseOrder"])
async def test_a_fractional_quantity_for_pcs_is_rejected_on_every_document_type(neo4j_session, type):
    # A piece stays a piece, whether bought or sold — even on documents without a stock
    # effect.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    with pytest.raises(BusinessLogicError, match="whole number"):
        await DocumentRepository.create_document(
            new_document(
                type=type,
                lines=[{"productNumber": "ACME-2007", "quantity": 2.5, "unitPrice": "1.00"}],
            ),
            "1", neo4j_session,
        )

    assert await count(neo4j_session, "MATCH (d:Document) RETURN count(d)") == 0


# ==========================================
# PARTIAL DELIVERY: the remainder lives on the server
# ==========================================
# Nothing is written forward, everything is computed: every partial delivery creates its
# invoice, whose lines hang off the delivery note line through FULFILS. `deliveredQuantity`
# is the sum of those edges — the same mechanism a purchase order counts its goods receipts
# with.

async def delivery_note_over(session, quantity: float, product: str = "ACME-2007"):
    """Builds the base constellation and returns a delivery note over `quantity`."""
    await create_master_data(session)
    await create_product(session, product)
    await create_stock(session, product, "1", quantity=100.0)
    await create_order(session, "2026-0734")
    return await DocumentRepository.create_document(
        new_document(
            type="DeliveryNote",
            orderProjectNumber="2026-0734",
            lines=[{"productNumber": product, "quantity": quantity, "unitPrice": "4200.00"}],
        ),
        "1", session,
    )


async def deliver(session, delivery_note_number: str, quantity: str, status: str = "partiallyDelivered"):
    """Reports a partial quantity as delivered and returns the updated document."""
    return await DocumentRepository.update_document(
        delivery_note_number,
        DocumentUpdate.model_validate(
            {"status": status, "deliveredQuantities": [{"lineNumber": 1, "quantity": quantity}]}
        ),
        "1", session,
    )


@pytest.mark.asyncio
async def test_a_partial_delivery_leaves_the_remainder_on_the_delivery_note(neo4j_session):
    delivery_note = await delivery_note_over(neo4j_session, 6)

    after = await deliver(neo4j_session, delivery_note.number, "5")

    assert after is not None
    assert after.status == "partiallyDelivered"
    # The ordered quantity stays what it was — delivered and open come on top.
    assert after.lines[0].quantity == 6
    assert after.lines[0].deliveredQuantity == 5
    assert after.lines[0].openQuantity == 1

    invoice = await DocumentRepository.get_document(after.createdFollowUpDocument, neo4j_session)  # type: ignore[arg-type]
    assert invoice is not None
    assert invoice.type == "Invoice"
    assert invoice.lines[0].quantity == 5


@pytest.mark.asyncio
async def test_the_remainder_survives_a_reload(neo4j_session):
    # A second, fresh read has to see the remainder — it lives in the graph, not in the
    # browser that reported the delivery.
    delivery_note = await delivery_note_over(neo4j_session, 6)
    await deliver(neo4j_session, delivery_note.number, "5")

    fresh = await DocumentRepository.get_document(delivery_note.number, neo4j_session)

    assert fresh is not None
    assert fresh.lines[0].openQuantity == 1


@pytest.mark.asyncio
async def test_delivering_the_remainder_closes_the_delivery_note_by_itself(neo4j_session):
    # The caller still asks for 'partiallyDelivered' — the server recognises that nothing is
    # open any more and concludes. Otherwise the document would hang in the outbound list
    # for ever.
    delivery_note = await delivery_note_over(neo4j_session, 6)
    await deliver(neo4j_session, delivery_note.number, "4")

    after = await deliver(neo4j_session, delivery_note.number, "2")

    assert after is not None
    assert after.status == "completed"
    assert after.lines[0].deliveredQuantity == 6
    assert after.lines[0].openQuantity == 0

    second = await DocumentRepository.get_document(after.createdFollowUpDocument, neo4j_session)  # type: ignore[arg-type]
    assert second is not None
    assert second.lines[0].quantity == 2


@pytest.mark.asyncio
async def test_concluding_invoices_only_the_open_quantity(neo4j_session):
    # The second road to the same goal: not through deliveredQuantities but through the
    # ordinary conclusion. Taking `quantity` instead of the open quantity would invoice all
    # six a second time.
    delivery_note = await delivery_note_over(neo4j_session, 6)
    await deliver(neo4j_session, delivery_note.number, "5")

    concluded = await DocumentRepository.update_document(
        delivery_note.number, DocumentUpdate(status="completed"), "1", neo4j_session
    )

    assert concluded is not None
    remainder_invoice = await DocumentRepository.get_document(
        concluded.createdFollowUpDocument, neo4j_session  # type: ignore[arg-type]
    )
    assert remainder_invoice is not None
    assert remainder_invoice.lines[0].quantity == 1


@pytest.mark.asyncio
async def test_delivering_more_than_is_open_is_rejected(neo4j_session):
    delivery_note = await delivery_note_over(neo4j_session, 6)
    await deliver(neo4j_session, delivery_note.number, "5")

    with pytest.raises(BusinessLogicError, match="exceeds the open quantity"):
        await deliver(neo4j_session, delivery_note.number, "2")

    # The rejected delivery must not have left anything behind.
    unchanged = await DocumentRepository.get_document(delivery_note.number, neo4j_session)
    assert unchanged is not None
    assert unchanged.lines[0].openQuantity == 1
    assert unchanged.status == "partiallyDelivered"
    assert await count(neo4j_session, "MATCH (d:Invoice) RETURN count(d)") == 1


@pytest.mark.asyncio
async def test_a_cancelled_partial_invoice_frees_the_quantity_again(neo4j_session):
    # The consequence of `deliveredQuantity` being computed rather than stored: if the
    # invoice falls away, its quantity counts as open again. A counter written forward
    # would need a manual correction here.
    delivery_note = await delivery_note_over(neo4j_session, 6)
    delivered = await deliver(neo4j_session, delivery_note.number, "5")
    assert delivered is not None

    await DocumentRepository.update_document(
        delivered.createdFollowUpDocument,  # type: ignore[arg-type]
        DocumentUpdate(status="cancelled"), "1", neo4j_session,
    )

    after = await DocumentRepository.get_document(delivery_note.number, neo4j_session)
    assert after is not None
    assert after.lines[0].deliveredQuantity == 0
    assert after.lines[0].openQuantity == 6


@pytest.mark.asyncio
async def test_delivered_quantities_on_a_concluded_delivery_note_are_an_error(neo4j_session):
    delivery_note = await delivery_note_over(neo4j_session, 6)
    await deliver(neo4j_session, delivery_note.number, "6")

    with pytest.raises(BusinessLogicError, match="concluded"):
        await deliver(neo4j_session, delivery_note.number, "1")


@pytest.mark.asyncio
async def test_delivered_quantities_exist_only_on_a_delivery_note(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    quote = await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)

    with pytest.raises(BusinessLogicError, match="delivery note"):
        await deliver(neo4j_session, quote.number, "1")


@pytest.mark.asyncio
async def test_cancelling_the_remainder_closes_the_line(neo4j_session):
    # Two go out, the rest is written off. Cancellation and delivery run in one transaction
    # — the cancellation first, because it rejects a document with an active follow-up and
    # the invoice of this step would be exactly one.
    delivery_note = await delivery_note_over(neo4j_session, 6)

    after = await DocumentRepository.update_document(
        delivery_note.number,
        DocumentUpdate.model_validate({
            "status": "partiallyCancelled",
            "cancelledLines":      [{"lineNumber": 1, "quantity": 4}],
            "deliveredQuantities": [{"lineNumber": 1, "quantity": 2}],
        }),
        "1", neo4j_session,
    )

    assert after is not None
    # 'partiallyCancelled' stays, although nothing is open any more: the rest was written
    # off, not delivered. A 'completed' would conceal that.
    assert after.status == "partiallyCancelled"
    assert after.lines[0].quantity == 2
    assert after.lines[0].openQuantity == 0

    invoice = await DocumentRepository.get_document(after.createdFollowUpDocument, neo4j_session)  # type: ignore[arg-type]
    assert invoice is not None
    assert invoice.lines[0].quantity == 2


@pytest.mark.asyncio
async def test_delivering_the_full_quantity_concludes_right_away(neo4j_session):
    # Whoever picks 'partiallyDelivered' but then delivers everything gets no
    # 'partiallyDelivered' document without a remainder.
    delivery_note = await delivery_note_over(neo4j_session, 6)
    await deliver(neo4j_session, delivery_note.number, "6", status="partiallyDelivered")

    state = await DocumentRepository.get_document(delivery_note.number, neo4j_session)
    assert state is not None
    assert state.status == "completed"
    assert state.lines[0].openQuantity == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["partiallyDelivered", "backorder"])
async def test_the_partial_invoice_does_not_close_the_partially_delivered_note(neo4j_session, state):
    # The counterpart to `test_a_new_document_closes_its_open_predecessor`: the lock only
    # applies to a predecessor that is still 'open'. A partially delivered delivery note
    # keeps its state across the invoice over the partial quantity — otherwise the
    # remainder would disappear from the outbound list.
    delivery_note = await delivery_note_over(neo4j_session, 6)
    await DocumentRepository.update_document(
        delivery_note.number, DocumentUpdate(status=state), "1", neo4j_session
    )

    await DocumentRepository.create_document(
        new_document(
            type="Invoice",
            orderProjectNumber="2026-0734",
            basedOn=[delivery_note.number],
            lines=[{"productNumber": "ACME-2007", "quantity": 5, "unitPrice": "4200.00"}],
        ),
        "1", neo4j_session,
    )

    after = await DocumentRepository.get_document(delivery_note.number, neo4j_session)
    assert after is not None
    assert after.status == state


@pytest.mark.asyncio
async def test_the_partial_invoice_does_not_flatten_a_partial_cancellation(neo4j_session):
    # 'partiallyCancelled' and 'completed' both lock in substance, but only
    # 'partiallyCancelled' says that part was never delivered — the follow-up invoice must
    # not overwrite that statement.
    delivery_note = await delivery_note_over(neo4j_session, 6)
    await DocumentRepository.update_document(
        delivery_note.number,
        DocumentUpdate.model_validate(
            {"status": "partiallyCancelled", "cancelledLines": [{"lineNumber": 1, "quantity": 1}]}
        ),
        "1", neo4j_session,
    )

    await DocumentRepository.create_document(
        new_document(
            type="Invoice",
            orderProjectNumber="2026-0734",
            basedOn=[delivery_note.number],
            lines=[{"productNumber": "ACME-2007", "quantity": 5, "unitPrice": "4200.00"}],
        ),
        "1", neo4j_session,
    )

    after = await DocumentRepository.get_document(delivery_note.number, neo4j_session)
    assert after is not None
    assert after.status == "partiallyCancelled"


# ==========================================
# STOCK EFFECT
# ==========================================

@pytest.mark.asyncio
async def test_the_order_confirmation_reserves(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)

    created = await DocumentRepository.create_document(new_document(), "1", neo4j_session)

    assert await read_stock(neo4j_session, "ACME-2007") == (100.0, 3.0)
    assert [m["type"] for m in await movements_of(neo4j_session, created.number)] == ["Reservation"]


@pytest.mark.asyncio
async def test_the_delivery_note_issues_and_releases_the_reservation(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0, reserved=3.0)

    created = await DocumentRepository.create_document(
        new_document(type="DeliveryNote"), "1", neo4j_session
    )

    assert await read_stock(neo4j_session, "ACME-2007") == (97.0, 0.0)
    assert [m["type"] for m in await movements_of(neo4j_session, created.number)] == ["Issue"]


@pytest.mark.asyncio
async def test_an_invoice_after_a_delivery_note_does_not_issue_again(neo4j_session):
    # The delivery note has issued already; a second issue would take the same goods out
    # twice. The whole BASED_ON chain is checked for that.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)
    await create_raw_document(neo4j_session, "DN-2026-0734", "DeliveryNote", customerId="C-1001")

    created = await DocumentRepository.create_document(
        new_document(type="Invoice", basedOn=["DN-2026-0734"]), "1", neo4j_session
    )

    assert await read_stock(neo4j_session, "ACME-2007") == (100.0, 0.0)
    assert await movements_of(neo4j_session, created.number) == []


@pytest.mark.asyncio
async def test_a_delivery_note_further_up_the_chain_counts_as_well(neo4j_session):
    # The delivery note stands two steps above the invoice and has issued all the same.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)
    await create_raw_document(neo4j_session, "DN-2026-0734", "DeliveryNote", customerId="C-1001")
    await create_raw_document(
        neo4j_session, "OC-2026-0734", "OrderConfirmation",
        customerId="C-1001", basedOn="DN-2026-0734",
    )

    created = await DocumentRepository.create_document(
        new_document(type="Invoice", basedOn=["OC-2026-0734"]), "1", neo4j_session
    )

    assert await movements_of(neo4j_session, created.number) == []


@pytest.mark.asyncio
async def test_a_direct_invoice_without_a_delivery_note_issues(neo4j_session):
    # Here the invoice is the only document that can issue.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)

    await DocumentRepository.create_document(new_document(type="Invoice"), "1", neo4j_session)

    assert await read_stock(neo4j_session, "ACME-2007") == (97.0, 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("type", ["Quote", "PurchaseOrder"])
async def test_quote_and_purchase_order_have_no_effect(neo4j_session, type):
    # Both say something about an intention, not about a movement of goods.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)

    await DocumentRepository.create_document(new_document(type=type), "1", neo4j_session)

    assert await read_stock(neo4j_session, "ACME-2007") == (100.0, 0.0)
    assert await count(neo4j_session, "MATCH (m:StockMovement) RETURN count(m)") == 0


@pytest.mark.asyncio
async def test_a_line_with_stock_effect_none_books_nothing(neo4j_session):
    # Labour and flat fees carry `stockEffect: 'none'` — a delivery note must not try to
    # issue them from a shelf they never lay on.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "SERVICE-1", "Commissioning", stockEffect="none")

    created = await DocumentRepository.create_document(
        new_document(
            type="DeliveryNote",
            lines=[{"productNumber": "SERVICE-1", "quantity": 1, "unitPrice": "480.00"}],
        ),
        "1", neo4j_session,
    )

    assert await movements_of(neo4j_session, created.number) == []


@pytest.mark.asyncio
async def test_the_booking_hangs_off_the_document_and_the_customer(neo4j_session):
    # Without BASED_ON_DOCUMENT the movement could no longer be assigned to its operation.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)

    created = await DocumentRepository.create_document(new_document(), "1", neo4j_session)

    assert len(await movements_of(neo4j_session, created.number)) == 1
    assert await count(
        neo4j_session,
        "MATCH (:StockMovement)-[r:CONCERNS_CUSTOMER]->(:Customer {id: 'C-1001'}) RETURN count(r)",
    ) == 1


@pytest.mark.asyncio
async def test_without_a_location_the_main_warehouse_applies(neo4j_session):
    # Looked up by the type, not by the id.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)
    await create_stock(neo4j_session, "ACME-2007", "2", 100.0)

    await DocumentRepository.create_document(new_document(), "1", neo4j_session)

    assert await read_stock(neo4j_session, "ACME-2007", "1") == (100.0, 3.0)
    assert await read_stock(neo4j_session, "ACME-2007", "2") == (100.0, 0.0)


@pytest.mark.asyncio
async def test_a_line_books_against_its_own_location(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)
    await create_stock(neo4j_session, "ACME-2007", "2", 100.0)

    await DocumentRepository.create_document(
        new_document(
            lines=[{"productNumber": "ACME-2007", "quantity": 3, "unitPrice": "1.00", "locationId": "2"}]
        ),
        "1", neo4j_session,
    )

    assert await read_stock(neo4j_session, "ACME-2007", "1") == (100.0, 0.0)
    assert await read_stock(neo4j_session, "ACME-2007", "2") == (100.0, 3.0)


@pytest.mark.asyncio
async def test_an_unknown_location_prevents_the_document(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)

    with pytest.raises(NotFoundError):
        await DocumentRepository.create_document(
            new_document(
                lines=[{"productNumber": "ACME-2007", "quantity": 3, "unitPrice": "1.00", "locationId": "99"}]
            ),
            "1", neo4j_session,
        )

    assert await count(neo4j_session, "MATCH (d:Document) RETURN count(d)") == 0


# ==========================================
# TOO LITTLE STOCK
# ==========================================

@pytest.mark.asyncio
async def test_too_little_stock_leaves_neither_document_nor_movement(neo4j_session):
    # Checked through Cypher, not through the status code: a partially booked document
    # would be a state no endpoint straightens out again.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", 10.0)

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.create_document(
            new_document(
                type="DeliveryNote",
                lines=[{"productNumber": "ACME-2007", "quantity": 999, "unitPrice": "1.00"}],
            ),
            "1", neo4j_session,
        )

    assert await count(neo4j_session, "MATCH (d:Document) RETURN count(d)") == 0
    assert await count(neo4j_session, "MATCH (l:DocumentLine) RETURN count(l)") == 0
    assert await count(neo4j_session, "MATCH (m:StockMovement) RETURN count(m)") == 0
    assert await count(neo4j_session, "MATCH (o:Order) RETURN count(o)") == 0
    assert await read_stock(neo4j_session, "ACME-2007") == (10.0, 0.0)


@pytest.mark.asyncio
async def test_the_second_line_takes_the_first_booking_back_with_it(neo4j_session):
    # The actual proof of the transaction: the first line can be booked, the second
    # cannot. Without the shared transaction the first issue would stay.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_product(neo4j_session, "ACME-2002")
    await create_stock(neo4j_session, "ACME-2007", "1", 100.0)
    await create_stock(neo4j_session, "ACME-2002", "1", 5.0)

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.create_document(
            new_document(
                type="DeliveryNote",
                lines=[
                    {"productNumber": "ACME-2007", "quantity": 3, "unitPrice": "1.00"},
                    {"productNumber": "ACME-2002", "quantity": 999, "unitPrice": "1.00"},
                ],
            ),
            "1", neo4j_session,
        )

    assert await read_stock(neo4j_session, "ACME-2007") == (100.0, 0.0)
    assert await count(neo4j_session, "MATCH (m:StockMovement) RETURN count(m)") == 0


@pytest.mark.asyncio
async def test_an_order_confirmation_beyond_the_available_stock_reserves_what_is_there(neo4j_session):
    # 30 ordered, only 20 available — the order confirmation comes about anyway (otherwise
    # the product would never show up in the reorder suggestion), but reserves only the 20.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)

    created = await DocumentRepository.create_document(
        new_document(lines=[{"productNumber": "ACME-2007", "quantity": 30, "unitPrice": "10.00"}]),
        "1", neo4j_session,
    )

    assert await read_stock(neo4j_session, "ACME-2007") == (20.0, 20.0)
    movements = await movements_of(neo4j_session, created.number)
    assert [(m["type"], m["quantity"]) for m in movements] == [("Reservation", 20.0)]


@pytest.mark.asyncio
async def test_a_second_order_confirmation_on_fully_reserved_goods_is_rejected(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    await DocumentRepository.create_document(
        new_document(lines=[{"productNumber": "ACME-2007", "quantity": 20, "unitPrice": "10.00"}]),
        "1", neo4j_session,
    )

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.create_document(
            new_document(lines=[{"productNumber": "ACME-2007", "quantity": 5, "unitPrice": "10.00"}]),
            "1", neo4j_session,
        )

    # Rejected means rejected: no second document, no additional reservation.
    assert await read_stock(neo4j_session, "ACME-2007") == (20.0, 20.0)
    assert await count(neo4j_session, "MATCH (d:OrderConfirmation) RETURN count(d)") == 1


# ==========================================
# READING A DOCUMENT AND ITS TOTALS
# ==========================================

@pytest.mark.asyncio
async def test_the_totals_of_an_order_confirmation(neo4j_session):
    # 3 x 4,200.00 EUR less 5 % plus 376.00 plus 397.00 makes 12,743.00 EUR. Read through
    # the service, because the repository leaves the calculated fields empty on purpose.
    await create_master_data(neo4j_session)
    for number in ("ACME-1000", "SERVICE-1", "SERVICE-2"):
        await create_product(neo4j_session, number)
    await create_raw_document(neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001")
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-1000", 3, 420000, 5.0, lineNumber=1)
    await create_raw_line(neo4j_session, "OC-2026-0734", "SERVICE-1", 1, 37600, lineNumber=2)
    await create_raw_line(neo4j_session, "OC-2026-0734", "SERVICE-2", 1, 39700, lineNumber=3)

    document = await DocumentService.get_document("OC-2026-0734", neo4j_session)

    assert document.subtotal == Decimal("12743.00")
    assert document.totalNet == Decimal("12743.00")
    # No tax rate maintained counts as 0 — an imported document keeps its totals.
    assert document.totalGross == Decimal("12743.00")
    assert sum(line.amount or Decimal(0) for line in document.lines) == Decimal("12743.00")


@pytest.mark.asyncio
async def test_the_list_reports_the_same_net_amount_as_the_detail(neo4j_session):
    # The list calculates in Cypher, the detail view in the service. If the two differ, the
    # overview shows a different amount than the opened document.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-1000")
    await create_raw_document(neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001")
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-1000", 3, 420000, 5.0, lineNumber=1)
    await neo4j_session.run(
        "MATCH (d:Document {number: 'OC-2026-0734'}) SET d.orderDiscountPercent = 3.0"
    )

    from_list = (await DocumentRepository.get_documents(neo4j_session))[0]
    from_detail = await DocumentService.get_document("OC-2026-0734", neo4j_session)

    assert from_list.totalNet == Decimal("11610.90")
    assert from_detail.totalNet == from_list.totalNet


@pytest.mark.asyncio
async def test_list_and_detail_agree_on_a_fixed_price_line_as_well(neo4j_session):
    # The same rule in both places: a fixed-price line stays outside the order discount.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-1000")
    await create_product(neo4j_session, "ACME-2007")
    await create_raw_document(neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001")
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-1000", 1, 8000, lineNumber=1)
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-2007", 1, 10000, lineNumber=2)
    await neo4j_session.run(
        """
        MATCH (d:Document {number: 'OC-2026-0734'}) SET d.orderDiscountPercent = 5.0
        WITH d MATCH (l:DocumentLine {id: 'OC-2026-0734_1'}) SET l.hasFixedPrice = true
        """
    )

    from_list = (await DocumentRepository.get_documents(neo4j_session))[0]
    from_detail = await DocumentService.get_document("OC-2026-0734", neo4j_session)

    assert from_list.totalNet == Decimal("175.00")
    assert from_detail.totalNet == Decimal("175.00")


@pytest.mark.asyncio
async def test_a_document_without_lines_has_totals_of_zero(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "IN-2026-0734", "Invoice", customerId="C-1001")

    document = await DocumentService.get_document("IN-2026-0734", neo4j_session)

    assert document.lines == []
    assert document.totalNet == Decimal("0.00")
    assert (await DocumentRepository.get_documents(neo4j_session))[0].totalNet == Decimal("0.00")


@pytest.mark.asyncio
async def test_the_lines_come_ordered_by_sort_order(neo4j_session):
    # Relationships in the graph have no order. Created deliberately in an order that
    # would sort differently by product number.
    await create_master_data(neo4j_session)
    for number in ("ACME-1000", "SERVICE-1", "ACME-2002"):
        await create_product(neo4j_session, number)
    await create_raw_document(neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001")
    await create_raw_line(neo4j_session, "OC-2026-0734", "SERVICE-1", 1, 100, lineNumber=1)
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-2002", 1, 100, lineNumber=2)
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-1000", 1, 100, lineNumber=3)

    document = await DocumentRepository.get_document("OC-2026-0734", neo4j_session)

    assert document is not None
    assert [line.productNumber for line in document.lines] == ["SERVICE-1", "ACME-2002", "ACME-1000"]


@pytest.mark.asyncio
async def test_an_inserted_sort_order_works_without_new_line_numbers(neo4j_session):
    # `sortOrder` lies ten apart, exactly so that something can be inserted between two lines
    # without reassigning existing line numbers (the key component).
    await create_master_data(neo4j_session)
    for number in ("ACME-1000", "SERVICE-1", "ACME-2002"):
        await create_product(neo4j_session, number)
    await create_raw_document(neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001")
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-1000", 1, 100, lineNumber=1, sortOrder=10)
    await create_raw_line(neo4j_session, "OC-2026-0734", "SERVICE-1", 1, 100, lineNumber=2, sortOrder=20)
    # Inserted between line 1 and 2, with a line number of its own.
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-2002", 1, 100, lineNumber=3, sortOrder=15)

    document = await DocumentRepository.get_document("OC-2026-0734", neo4j_session)

    assert document is not None
    assert [line.productNumber for line in document.lines] == ["ACME-1000", "ACME-2002", "SERVICE-1"]


@pytest.mark.asyncio
async def test_the_line_carries_the_master_data_of_its_product(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", label="Vibration Sensor", unit="pcs")
    await create_raw_document(neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001")
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-2007", 3, 8500, lineNumber=1)

    document = await DocumentRepository.get_document("OC-2026-0734", neo4j_session)

    assert document is not None
    line = document.lines[0]
    assert line.label == "Vibration Sensor"
    assert line.unit == "pcs"
    assert line.unitPrice == Decimal("85.00")


@pytest.mark.asyncio
async def test_a_price_of_zero_stays_recorded(neo4j_session):
    # 0 means "recorded as free of charge", not "no price" — the goodwill line.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_raw_document(neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001")
    await create_raw_line(neo4j_session, "OC-2026-0734", "ACME-2007", 3, 0, lineNumber=1)

    document = await DocumentRepository.get_document("OC-2026-0734", neo4j_session)

    assert document is not None
    assert document.lines[0].unitPrice == Decimal("0.00")


@pytest.mark.asyncio
async def test_an_unknown_document_number_returns_none(neo4j_session):
    await create_master_data(neo4j_session)

    assert await DocumentRepository.get_document("IN-1999-9999", neo4j_session) is None


# ==========================================
# DOCUMENT LIST AND FILTERS
# ==========================================

@pytest.mark.asyncio
async def test_the_supplier_filter_returns_the_purchasing_documents(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "PO-2026-0001", "PurchaseOrder", supplierId="S-001")
    await create_raw_document(neo4j_session, "GR-2026-0001", "GoodsReceipt", supplierId="S-001")
    await create_raw_document(neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001")

    hits = await DocumentRepository.get_documents(neo4j_session, supplier_id="S-001")

    assert {d.number for d in hits} == {"PO-2026-0001", "GR-2026-0001"}
    assert all(d.customer is None for d in hits)


@pytest.mark.asyncio
async def test_the_type_filter_returns_exactly_one_document_type(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "QU-2026-0734", "Quote", customerId="C-1001")
    await create_raw_document(neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001")

    hits = await DocumentRepository.get_documents(neo4j_session, type="Quote")

    assert [d.number for d in hits] == ["QU-2026-0734"]


@pytest.mark.asyncio
async def test_the_filters_add_up(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "QU-2026-0734", "Quote", customerId="C-1001")
    await create_raw_document(
        neo4j_session, "QU-2026-0735", "Quote", customerId="C-1001", status="completed"
    )

    hits = await DocumentRepository.get_documents(neo4j_session, type="Quote", status="completed")

    assert [d.number for d in hits] == ["QU-2026-0735"]


@pytest.mark.asyncio
async def test_the_period_filter_includes_its_bounds(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "QU-2024", "Quote", customerId="C-1001", documentDate="2024-12-31")
    await create_raw_document(neo4j_session, "QU-2025-A", "Quote", customerId="C-1001", documentDate="2025-01-01")
    await create_raw_document(neo4j_session, "QU-2025-B", "Quote", customerId="C-1001", documentDate="2025-12-31")

    hits = await DocumentRepository.get_documents(
        neo4j_session, from_date=date(2025, 1, 1), to_date=date(2025, 12, 31)
    )

    assert {d.number for d in hits} == {"QU-2025-A", "QU-2025-B"}


@pytest.mark.asyncio
async def test_the_search_matches_document_number_and_partner_names(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "QU-2026-0734", "Quote", customerId="C-1001")
    await create_raw_document(neo4j_session, "PO-2026-0001", "PurchaseOrder", supplierId="S-001")

    by_number = await DocumentRepository.get_documents(neo4j_session, search="0734")
    by_customer = await DocumentRepository.get_documents(neo4j_session, search="example")
    by_supplier = await DocumentRepository.get_documents(neo4j_session, search="Alpha")

    assert [d.number for d in by_number] == ["QU-2026-0734"]
    assert [d.number for d in by_customer] == ["QU-2026-0734"]
    assert [d.number for d in by_supplier] == ["PO-2026-0001"]


@pytest.mark.asyncio
async def test_an_invoice_over_two_delivery_notes_appears_once_in_the_list(neo4j_session):
    # basedOn can point at several predecessors. Without the collect() before the WHERE the
    # invoice would appear once per predecessor.
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "DN-A", "DeliveryNote", customerId="C-1001")
    await create_raw_document(neo4j_session, "DN-B", "DeliveryNote", customerId="C-1001")
    await create_raw_document(neo4j_session, "IN-A", "Invoice", customerId="C-1001", basedOn="DN-A")
    await neo4j_session.run(
        "MATCH (i:Document {number: 'IN-A'}), (d:Document {number: 'DN-B'}) MERGE (i)-[:BASED_ON]->(d)"
    )

    hits = await DocumentRepository.get_documents(neo4j_session, type="Invoice")

    assert [d.number for d in hits] == ["IN-A"]
    assert sorted(hits[0].basedOn) == ["DN-A", "DN-B"]


@pytest.mark.asyncio
async def test_a_filter_combination_without_hits_is_empty(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "QU-2026-0734", "Quote", customerId="C-1001")

    assert await DocumentRepository.get_documents(
        neo4j_session, type="Invoice", customer_id="C-9999"
    ) == []


# ==========================================
# CHANGING A DOCUMENT AND THE LOCK
# ==========================================

@pytest.mark.asyncio
async def test_a_patch_maintains_the_open_fields(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "QU-2026-0734", "Quote", customerId="C-1001")

    changed = await DocumentRepository.update_document(
        "QU-2026-0734",
        DocumentUpdate.model_validate(
            {"deliveryDate": "2026-09-01", "orderDiscountPercent": 3.0, "taxPercent": 20.0}
        ),
        "1", neo4j_session,
    )

    assert changed is not None
    assert changed.deliveryDate == date(2026, 9, 1)
    assert changed.orderDiscountPercent == 3.0
    assert changed.taxPercent == 20.0
    assert changed.updatedAt is not None


@pytest.mark.asyncio
async def test_a_completed_document_is_locked_in_substance(neo4j_session):
    # Otherwise the discount of an invoice already printed and booked could be changed
    # after the fact.
    await create_master_data(neo4j_session)
    await create_raw_document(
        neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001", status="completed"
    )

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            "OC-2026-0734", DocumentUpdate(deliveryDate=date(2026, 9, 1)), "1", neo4j_session
        )

    document = await DocumentRepository.get_document("OC-2026-0734", neo4j_session)
    assert document is not None
    assert document.deliveryDate is None


@pytest.mark.asyncio
async def test_a_completed_document_can_be_reopened(neo4j_session):
    # A status change stays allowed on a concluded document.
    await create_master_data(neo4j_session)
    await create_raw_document(
        neo4j_session, "OC-2026-0734", "OrderConfirmation", customerId="C-1001", status="completed"
    )

    changed = await DocumentRepository.update_document(
        "OC-2026-0734", DocumentUpdate(status="open"), "1", neo4j_session
    )

    assert changed is not None
    assert changed.status == "open"


@pytest.mark.asyncio
async def test_a_patch_without_any_field_is_rejected_for_documents(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "QU-2026-0734", "Quote", customerId="C-1001")

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            "QU-2026-0734", DocumentUpdate(), "1", neo4j_session
        )


@pytest.mark.asyncio
async def test_a_patch_on_an_unknown_document_returns_none(neo4j_session):
    await create_master_data(neo4j_session)

    assert await DocumentRepository.update_document(
        "IN-1999-9999", DocumentUpdate(status="open"), "1", neo4j_session
    ) is None


# ==========================================
# A COMPLETED DELIVERY NOTE CREATES ITS INVOICE
# ==========================================
# If the delivery note arrives with status "completed", the invoice comes about
# server-side in the same transaction.

@pytest.mark.asyncio
async def test_a_completed_delivery_note_creates_the_invoice_automatically(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    await create_order(neo4j_session, "2026-0734")

    confirmation = await DocumentRepository.create_document(
        new_document(
            orderProjectNumber="2026-0734",
            lines=[{"productNumber": "ACME-2007", "quantity": 5, "unitPrice": "10.00"}],
        ),
        "1", neo4j_session,
    )
    delivery_note = await DocumentRepository.create_document(
        new_document(
            type="DeliveryNote",
            orderProjectNumber="2026-0734",
            basedOn=[confirmation.number],
            lines=[{"productNumber": "ACME-2007", "quantity": 5, "unitPrice": "10.00"}],
        ),
        "1", neo4j_session,
    )

    changed = await DocumentRepository.update_document(
        delivery_note.number, DocumentUpdate(status="completed"), "2", neo4j_session
    )

    assert changed is not None
    assert changed.createdFollowUpDocument == "IN-2026-0734"

    invoice = await DocumentRepository.get_document(changed.createdFollowUpDocument, neo4j_session)
    assert invoice is not None
    assert invoice.type == "Invoice"
    assert invoice.basedOn == [delivery_note.number]
    assert invoice.orderProjectNumber == "2026-0734"
    assert invoice.createdBy is not None
    assert invoice.createdBy.id == "2"
    assert [(line.productNumber, line.quantity, line.unitPrice) for line in invoice.lines] == [
        ("ACME-2007", 5, Decimal("10.00")),
    ]


@pytest.mark.asyncio
async def test_the_automatic_invoice_does_not_issue_a_second_time(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)

    delivery_note = await DocumentRepository.create_document(
        new_document(
            type="DeliveryNote",
            lines=[{"productNumber": "ACME-2007", "quantity": 5, "unitPrice": "10.00"}],
        ),
        "1", neo4j_session,
    )
    assert await read_stock(neo4j_session, "ACME-2007") == (15.0, 0.0)

    await DocumentRepository.update_document(
        delivery_note.number, DocumentUpdate(status="completed"), "1", neo4j_session
    )

    assert await read_stock(neo4j_session, "ACME-2007") == (15.0, 0.0)


@pytest.mark.asyncio
async def test_a_second_patch_to_completed_creates_no_second_invoice(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    delivery_note = await DocumentRepository.create_document(
        new_document(
            type="DeliveryNote",
            lines=[{"productNumber": "ACME-2007", "quantity": 5, "unitPrice": "10.00"}],
        ),
        "1", neo4j_session,
    )

    first = await DocumentRepository.update_document(
        delivery_note.number, DocumentUpdate(status="completed"), "1", neo4j_session
    )
    second = await DocumentRepository.update_document(
        delivery_note.number, DocumentUpdate(status="completed"), "1", neo4j_session
    )

    assert first is not None and first.createdFollowUpDocument is not None
    assert second is not None and second.createdFollowUpDocument is None
    assert await count(
        neo4j_session,
        "MATCH (i:Invoice)-[:BASED_ON]->(:Document {number: $number}) RETURN count(i)",
        number=delivery_note.number,
    ) == 1


@pytest.mark.asyncio
async def test_the_concluded_delivery_note_is_locked_in_substance_itself(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    delivery_note = await DocumentRepository.create_document(
        new_document(
            type="DeliveryNote",
            lines=[{"productNumber": "ACME-2007", "quantity": 5, "unitPrice": "10.00"}],
        ),
        "1", neo4j_session,
    )
    await DocumentRepository.update_document(
        delivery_note.number, DocumentUpdate(status="completed"), "1", neo4j_session
    )

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            delivery_note.number, DocumentUpdate(deliveryDate=date(2026, 9, 1)), "1", neo4j_session
        )


@pytest.mark.asyncio
async def test_the_automatic_invoice_takes_over_a_manual_price_and_its_reason(neo4j_session):
    # A line whose price was overridden by hand brings its reason along — otherwise the new
    # invoice line would fail its own validation, which demands exactly that.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", listPriceCent=1200)
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    delivery_note = await DocumentRepository.create_document(
        new_document(
            type="DeliveryNote",
            lines=[{
                "productNumber": "ACME-2007", "quantity": 1, "unitPrice": "10.00",
                "priceOverridden": True, "reason": "Damaged packaging.",
            }],
        ),
        "1", neo4j_session,
    )

    changed = await DocumentRepository.update_document(
        delivery_note.number, DocumentUpdate(status="completed"), "1", neo4j_session
    )

    assert changed is not None
    overrides = await DocumentRepository.get_price_overrides(
        changed.createdFollowUpDocument, neo4j_session  # type: ignore[arg-type]
    )
    assert overrides is not None
    assert [(o.newPrice, o.reason) for o in overrides] == [(Decimal("10.00"), "Damaged packaging.")]


# ==========================================
# REASON FOR A MANUAL PRICE CHANGE
# ==========================================
# Only lines with priceOverridden=True leave a price change behind; oldPrice is the list
# price of the product at the time of the change, not the value that stood on the line
# before.

def overridden_line(product: str, unit_price: str, reason: str, quantity: int = 1) -> dict:
    return {
        "productNumber": product, "quantity": quantity, "unitPrice": unit_price,
        "priceOverridden": True, "reason": reason,
    }


@pytest.mark.asyncio
async def test_a_price_overridden_by_hand_leaves_a_reason(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", listPriceCent=1200)

    created = await DocumentRepository.create_document(
        new_document(
            type="Quote",
            lines=[overridden_line("ACME-2007", "10.00", "Customer received a damaged piece.")],
        ),
        "1", neo4j_session,
    )

    rows = await DocumentRepository.get_price_overrides(created.number, neo4j_session)

    assert rows is not None
    assert len(rows) == 1
    row = rows[0]
    assert row.productNumber == "ACME-2007"
    assert row.oldPrice == Decimal("12.00")
    assert row.newPrice == Decimal("10.00")
    assert row.reason == "Customer received a damaged piece."
    assert row.employee is not None
    assert row.employee.id == "1"


@pytest.mark.asyncio
async def test_only_the_overridden_line_leaves_a_reason(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", listPriceCent=1200)
    await create_product(neo4j_session, "ACME-2002")

    created = await DocumentRepository.create_document(
        new_document(
            type="Quote",
            lines=[
                overridden_line("ACME-2007", "10.00", "Special discount."),
                {"productNumber": "ACME-2002", "quantity": 1, "unitPrice": "5.00"},
            ],
        ),
        "1", neo4j_session,
    )

    rows = await DocumentRepository.get_price_overrides(created.number, neo4j_session)

    assert rows is not None
    assert [r.productNumber for r in rows] == ["ACME-2007"]


@pytest.mark.asyncio
async def test_the_reason_points_at_the_right_line_when_a_product_appears_twice(neo4j_session):
    # By product number alone it would no longer be possible to say which of the two lines
    # was changed by hand — and the caller could not hand the reason back to the right line
    # when building a follow-up document.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", listPriceCent=1200)

    created = await DocumentRepository.create_document(
        new_document(
            type="Quote",
            lines=[
                {"productNumber": "ACME-2007", "quantity": 1, "unitPrice": "12.00"},
                overridden_line("ACME-2007", "9.00", "Volume price from three pieces.", quantity=3),
            ],
        ),
        "1", neo4j_session,
    )

    rows = await DocumentRepository.get_price_overrides(created.number, neo4j_session)

    assert rows is not None
    assert [(r.lineNumber, r.newPrice) for r in rows] == [(2, Decimal("9.00"))]


@pytest.mark.asyncio
async def test_reasons_come_in_line_order(neo4j_session):
    # Ordered by line number, not by product number: the list stands beside the lines on the
    # document and is meant to have the same order.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", listPriceCent=1200)
    await create_product(neo4j_session, "ACME-2002", listPriceCent=3000)

    created = await DocumentRepository.create_document(
        new_document(
            type="Quote",
            lines=[
                overridden_line("ACME-2007", "10.00", "Entered first."),
                overridden_line("ACME-2002", "25.00", "Entered second."),
            ],
        ),
        "1", neo4j_session,
    )

    rows = await DocumentRepository.get_price_overrides(created.number, neo4j_session)

    assert rows is not None
    assert [(r.lineNumber, r.productNumber) for r in rows] == [(1, "ACME-2007"), (2, "ACME-2002")]


@pytest.mark.asyncio
async def test_a_document_without_an_overridden_line_has_an_empty_list(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")

    created = await DocumentRepository.create_document(new_document(type="Quote"), "1", neo4j_session)

    assert await DocumentRepository.get_price_overrides(created.number, neo4j_session) == []


@pytest.mark.asyncio
async def test_reasons_of_an_unknown_document_return_none(neo4j_session):
    await create_master_data(neo4j_session)

    assert await DocumentRepository.get_price_overrides("IN-1999-9999", neo4j_session) is None


# ==========================================
# CANCELLATION — reversal, chain lock, partial cancellation
# ==========================================
# A reversal booking rather than a snapshot restore, a marker on the line with the line
# excluded from the totals, and a lock on the chain instead of an automatic cascade.

async def document_with(session, type: str, *lines: tuple[str, float, str], **fields):
    """Creates a document of `type` with the given (product, quantity, price) lines."""
    return await DocumentRepository.create_document(
        new_document(
            type=type,
            lines=[
                {"productNumber": product, "quantity": quantity, "unitPrice": price}
                for product, quantity, price in lines
            ],
            **fields,
        ),
        "1", session,
    )


@pytest.mark.asyncio
async def test_cancelling_a_delivery_note_books_the_stock_back(neo4j_session):
    # The issue is reversed by raising only the quantity — reserved stays untouched: the
    # order the reservation was made for is void with the cancellation, the goods come back
    # freely available, not reserved again.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)

    confirmation = await document_with(neo4j_session, "OrderConfirmation", ("ACME-2007", 5, "10.00"))
    delivery_note = await document_with(
        neo4j_session, "DeliveryNote", ("ACME-2007", 5, "10.00"), basedOn=[confirmation.number]
    )
    assert await read_stock(neo4j_session, "ACME-2007") == (15.0, 0.0)

    cancelled = await DocumentRepository.update_document(
        delivery_note.number, DocumentUpdate(status="cancelled"), "1", neo4j_session
    )

    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.lines[0].cancelled is True
    assert await read_stock(neo4j_session, "ACME-2007") == (20.0, 0.0)

    reversal = next(
        m for m in await movements_of(neo4j_session, delivery_note.number) if m["type"] == "Correction"
    )
    assert reversal["quantity"] == 5.0


@pytest.mark.asyncio
async def test_a_partial_cancellation_of_an_order_confirmation_releases_only_that_line(neo4j_session):
    # A reservation is reversed against reserved, never against the quantity — a
    # reservation never changed the quantity.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_product(neo4j_session, "ACME-2002")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    await create_stock(neo4j_session, "ACME-2002", "1", quantity=20.0)

    confirmation = await document_with(
        neo4j_session, "OrderConfirmation", ("ACME-2007", 3, "10.00"), ("ACME-2002", 2, "10.00")
    )
    assert await read_stock(neo4j_session, "ACME-2007") == (20.0, 3.0)
    assert await read_stock(neo4j_session, "ACME-2002") == (20.0, 2.0)

    partially_cancelled = await DocumentRepository.update_document(
        confirmation.number,
        DocumentUpdate(status="partiallyCancelled", cancelledLines=[CancelledLine(lineNumber=1)]),
        "1", neo4j_session,
    )

    assert partially_cancelled is not None
    assert partially_cancelled.status == "partiallyCancelled"
    assert {line.productNumber: line.cancelled for line in partially_cancelled.lines} == {
        "ACME-2007": True, "ACME-2002": False,
    }
    assert await read_stock(neo4j_session, "ACME-2007") == (20.0, 0.0)
    assert await read_stock(neo4j_session, "ACME-2002") == (20.0, 2.0)


@pytest.mark.asyncio
async def test_a_partial_cancellation_reverses_only_the_cancelled_line_of_a_duplicated_product(neo4j_session):
    # The reversal runs over StockMovement.lineNumber, not the product number: filtering by
    # product alone would also hit the sister line that was not cancelled.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    confirmation = await document_with(
        neo4j_session, "OrderConfirmation", ("ACME-2007", 3, "10.00"), ("ACME-2007", 2, "0.00")
    )
    assert await read_stock(neo4j_session, "ACME-2007") == (20.0, 5.0)

    await DocumentRepository.update_document(
        confirmation.number,
        DocumentUpdate(status="partiallyCancelled", cancelledLines=[CancelledLine(lineNumber=2)]),
        "1", neo4j_session,
    )

    assert await read_stock(neo4j_session, "ACME-2007") == (20.0, 3.0)


@pytest.mark.asyncio
async def test_cancelling_with_an_active_follow_up_document_is_locked(neo4j_session):
    # No automatic cascade: cancellation runs backwards through the chain, not as a chain
    # reaction forwards.
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "OC-1", "OrderConfirmation", customerId="C-1001")
    await create_raw_document(neo4j_session, "DN-1", "DeliveryNote", customerId="C-1001", basedOn="OC-1")

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            "OC-1", DocumentUpdate(status="cancelled"), "1", neo4j_session
        )

    # The predecessor stays unchanged.
    document = await DocumentRepository.get_document("OC-1", neo4j_session)
    assert document is not None
    assert document.status == "open"


@pytest.mark.asyncio
async def test_cancellation_runs_backwards_through_the_chain(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "OC-1", "OrderConfirmation", customerId="C-1001")
    await create_raw_document(neo4j_session, "DN-1", "DeliveryNote", customerId="C-1001", basedOn="OC-1")

    # First the successor...
    await DocumentRepository.update_document("DN-1", DocumentUpdate(status="cancelled"), "1", neo4j_session)
    # ...then the predecessor can be cancelled.
    predecessor = await DocumentRepository.update_document(
        "OC-1", DocumentUpdate(status="cancelled"), "1", neo4j_session
    )

    assert predecessor is not None
    assert predecessor.status == "cancelled"


@pytest.mark.asyncio
async def test_a_successor_two_steps_down_blocks_as_well(neo4j_session):
    # The whole chain downwards counts, not only the immediate follow-up.
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "QU-1", "Quote", customerId="C-1001")
    await create_raw_document(
        neo4j_session, "OC-1", "OrderConfirmation", customerId="C-1001", basedOn="QU-1", status="cancelled"
    )
    await create_raw_document(neo4j_session, "DN-1", "DeliveryNote", customerId="C-1001", basedOn="OC-1")

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            "QU-1", DocumentUpdate(status="cancelled"), "1", neo4j_session
        )


@pytest.mark.asyncio
async def test_a_document_already_cancelled_cannot_be_cancelled_again(neo4j_session):
    # Otherwise the same movement would be reversed a second time.
    await create_master_data(neo4j_session)
    await create_raw_document(
        neo4j_session, "OC-1", "OrderConfirmation", customerId="C-1001", status="cancelled"
    )

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            "OC-1", DocumentUpdate(status="cancelled"), "1", neo4j_session
        )


@pytest.mark.asyncio
async def test_a_cancelled_document_stays_readable(neo4j_session):
    await create_master_data(neo4j_session)
    await create_raw_document(neo4j_session, "IN-1", "Invoice", customerId="C-1001", status="cancelled")

    document = await DocumentRepository.get_document("IN-1", neo4j_session)

    assert document is not None
    assert document.status == "cancelled"


@pytest.mark.asyncio
async def test_a_partial_quantity_cancellation_gives_back_only_that_quantity(neo4j_session):
    # The outbound case: a delivery note over 5, two go out, the remaining three are
    # written off. Reversing the full line would give all five back, although two are
    # already at the customer.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    delivery_note = await document_with(neo4j_session, "DeliveryNote", ("ACME-2007", 5, "10.00"))
    assert await read_stock(neo4j_session, "ACME-2007") == (15.0, 0.0)

    partially_cancelled = await DocumentRepository.update_document(
        delivery_note.number,
        DocumentUpdate(
            status="partiallyCancelled",
            cancelledLines=[CancelledLine(lineNumber=1, quantity=Decimal("3"))],
        ),
        "1", neo4j_session,
    )

    assert partially_cancelled is not None
    # Only the three cancelled come back, not all five.
    assert await read_stock(neo4j_session, "ACME-2007") == (18.0, 0.0)
    # The line stays and carries the quantity actually delivered — the customer still owes
    # the two that went out.
    line = partially_cancelled.lines[0]
    assert line.quantity == 2
    assert line.cancelled is False


@pytest.mark.asyncio
async def test_a_partial_cancellation_of_the_full_quantity_marks_the_line_cancelled(neo4j_session):
    # A partial quantity equal to the booked quantity is a full line cancellation —
    # otherwise a line with quantity 0 would remain instead of being marked.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    delivery_note = await document_with(neo4j_session, "DeliveryNote", ("ACME-2007", 4, "10.00"))

    partially_cancelled = await DocumentRepository.update_document(
        delivery_note.number,
        DocumentUpdate(
            status="partiallyCancelled",
            cancelledLines=[CancelledLine(lineNumber=1, quantity=Decimal("4"))],
        ),
        "1", neo4j_session,
    )

    assert partially_cancelled is not None
    assert partially_cancelled.lines[0].cancelled is True
    assert await read_stock(neo4j_session, "ACME-2007") == (20.0, 0.0)


@pytest.mark.asyncio
async def test_a_partial_cancellation_beyond_the_booked_quantity_is_an_error(neo4j_session):
    # Stock would come out of nothing: the document booked three, a cancellation cannot give
    # four back.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    delivery_note = await document_with(neo4j_session, "DeliveryNote", ("ACME-2007", 3, "10.00"))

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            delivery_note.number,
            DocumentUpdate(
                status="partiallyCancelled",
                cancelledLines=[CancelledLine(lineNumber=1, quantity=Decimal("4"))],
            ),
            "1", neo4j_session,
        )

    # Neither stock nor status moved.
    assert await read_stock(neo4j_session, "ACME-2007") == (17.0, 0.0)


@pytest.mark.asyncio
async def test_a_partial_quantity_cancellation_shortens_the_total_accordingly(neo4j_session):
    # The counterpart to `test_the_totals_pass_over_the_cancelled_line`: there the whole line
    # drops out of the total, here only its cancelled share.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)

    # Through the service, not the repository: the totals are calculated there.
    delivery_note = await DocumentService.create_document(
        new_document(
            type="DeliveryNote",
            lines=[{"productNumber": "ACME-2007", "quantity": 5, "unitPrice": "10.00"}],
        ),
        "1", neo4j_session,
    )
    assert delivery_note.totalNet == Decimal("50.00")

    await DocumentService.update_document(
        delivery_note.number,
        DocumentUpdate(
            status="partiallyCancelled",
            cancelledLines=[CancelledLine(lineNumber=1, quantity=Decimal("3"))],
        ),
        "1", neo4j_session,
    )

    document = await DocumentService.get_document(delivery_note.number, neo4j_session)
    assert document.totalNet == Decimal("20.00")


@pytest.mark.asyncio
async def test_a_partial_cancellation_with_an_unknown_line_number_is_an_error(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    confirmation = await document_with(neo4j_session, "OrderConfirmation", ("ACME-2007", 3, "10.00"))

    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            confirmation.number,
            DocumentUpdate(status="partiallyCancelled", cancelledLines=[CancelledLine(lineNumber=2)]),
            "1", neo4j_session,
        )


@pytest.mark.asyncio
async def test_the_totals_pass_over_the_cancelled_line(neo4j_session):
    # The line stays visible on the document (with its amount) but no longer counts towards
    # subtotal and net — an invoice once issued is not changed retroactively, a
    # cancellation is an additional marker.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_product(neo4j_session, "ACME-2002")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    await create_stock(neo4j_session, "ACME-2002", "1", quantity=20.0)

    confirmation = await DocumentService.create_document(
        new_document(
            lines=[
                {"productNumber": "ACME-2007", "quantity": 1, "unitPrice": "100.00"},
                {"productNumber": "ACME-2002", "quantity": 1, "unitPrice": "50.00"},
            ],
        ),
        "1", neo4j_session,
    )
    await DocumentService.update_document(
        confirmation.number,
        DocumentUpdate(status="partiallyCancelled", cancelledLines=[CancelledLine(lineNumber=2)]),
        "1", neo4j_session,
    )

    document = await DocumentService.get_document(confirmation.number, neo4j_session)

    assert document.subtotal == Decimal("100.00")
    assert document.totalNet == Decimal("100.00")
    cancelled_line = next(line for line in document.lines if line.productNumber == "ACME-2002")
    assert cancelled_line.cancelled is True
    assert cancelled_line.amount == Decimal("50.00")


# ==========================================
# BILL-OF-MATERIALS LINES: the delivery note issues the components
# ==========================================
# ACME-1000 carries `stockEffect: 'billOfMaterials'` and a one-level bill of materials
# (1 x ACME-2007 per piece). The order confirmation itself creates no asset and reserves
# nothing for such a line — the reservation happens on the release of each asset — and the
# delivery note issues the components of the assets instead of the kit.

async def bom_product(session, quantity_per_piece: float = 1.0) -> None:
    """Creates the asset product ACME-1000 with a one-level bill of materials."""
    await create_product(session, "ACME-1000", "Starter Kit A", stockEffect="billOfMaterials")
    await create_product(session, "ACME-2007", "Vibration Sensor")
    await session.run(
        "MATCH (p:Product {number: 'ACME-1000'}), (c:Product {number: 'ACME-2007'}) "
        "CREATE (p)-[:CONTAINS {quantity: $quantity}]->(c)",
        quantity=quantity_per_piece,
    )


async def assets_of(session, productNumber: str) -> list[str]:
    result = await session.run(
        "MATCH (a:AssetInstance)-[:BASED_ON]->(:Product {number: $productNumber}) "
        "RETURN a.serialNumber AS sn ORDER BY a.serialNumber",
        productNumber=productNumber,
    )
    return [r["sn"] async for r in result]


async def confirmation_with_assets(session, pieces: int, release: bool = True) -> str:
    """Builds an order confirmation over `pieces` kits with one asset per piece.

    The assets come about through the manual path (`AssetRepository.create_asset`), because
    the order confirmation itself creates none.
    """
    await create_master_data(session)
    await create_order(session, "2026-0734")
    await bom_product(session)
    await create_stock(session, "ACME-2007", "1", quantity=100.0)

    confirmation = await document_with(
        session, "OrderConfirmation", ("ACME-1000", pieces, "5000.00"),
        orderProjectNumber="2026-0734",
    )
    for _ in range(pieces):
        created = await AssetRepository.create_asset(
            AssetCreate(documentNumber=confirmation.number, productNumber="ACME-1000"), session
        )
        if release:
            await AssetRepository.set_release(
                created.serialNumber, AssetReleaseRequest(released=True), "2", session
            )
    return confirmation.number


@pytest.mark.asyncio
async def test_an_order_confirmation_creates_no_asset_by_itself(neo4j_session):
    # The asset comes about when engineering confirms the draft or creates it by hand — not
    # automatically with the order confirmation.
    await create_master_data(neo4j_session)
    await create_order(neo4j_session, "2026-0734")
    await bom_product(neo4j_session)

    await document_with(
        neo4j_session, "OrderConfirmation", ("ACME-1000", 2, "5000.00"),
        orderProjectNumber="2026-0734", assetPurpose="newAsset",
    )

    assert await assets_of(neo4j_session, "ACME-1000") == []


@pytest.mark.asyncio
async def test_a_bill_of_materials_line_on_an_order_confirmation_reserves_nothing(neo4j_session):
    # Reserved is only on the release of the individual asset.
    await create_master_data(neo4j_session)
    await create_order(neo4j_session, "2026-0734")
    await bom_product(neo4j_session)

    confirmation = await document_with(
        neo4j_session, "OrderConfirmation", ("ACME-1000", 1, "5000.00"),
        orderProjectNumber="2026-0734",
    )

    assert await movements_of(neo4j_session, confirmation.number) == []


@pytest.mark.asyncio
async def test_the_delivery_note_issues_the_components_of_every_asset(neo4j_session):
    confirmation_number = await confirmation_with_assets(neo4j_session, pieces=2)

    await document_with(
        neo4j_session, "DeliveryNote", ("ACME-1000", 2, "5000.00"),
        basedOn=[confirmation_number], orderProjectNumber="2026-0734",
    )

    # Two assets with one ACME-2007 each: the issue sums over both, and releases the two
    # reservations of the release with it.
    assert await read_stock(neo4j_session, "ACME-2007") == (98.0, 0.0)
    # The kit itself is not issued — only its components.
    assert await read_stock(neo4j_session, "ACME-1000") == (0.0, 0.0)


@pytest.mark.asyncio
async def test_a_delivery_note_without_a_release_is_rejected(neo4j_session):
    confirmation_number = await confirmation_with_assets(neo4j_session, pieces=1, release=False)

    with pytest.raises(BusinessLogicError, match="not released"):
        await document_with(
            neo4j_session, "DeliveryNote", ("ACME-1000", 1, "5000.00"),
            basedOn=[confirmation_number], orderProjectNumber="2026-0734",
        )

    # No release, no delivery note: nothing booked, no delivery note created.
    assert await read_stock(neo4j_session, "ACME-2007") == (100.0, 0.0)
    assert await count(neo4j_session, "MATCH (d:DeliveryNote) RETURN count(d)") == 0


@pytest.mark.asyncio
async def test_the_delivery_note_quantity_has_to_match_the_number_of_assets(neo4j_session):
    confirmation_number = await confirmation_with_assets(neo4j_session, pieces=2)

    with pytest.raises(BusinessLogicError):
        await document_with(
            neo4j_session, "DeliveryNote", ("ACME-1000", 1, "5000.00"),
            basedOn=[confirmation_number], orderProjectNumber="2026-0734",
        )

    # Unchanged since the release (two assets with one piece each reserved) — the failed
    # delivery note booked nothing on top.
    assert await read_stock(neo4j_session, "ACME-2007") == (100.0, 2.0)


@pytest.mark.asyncio
async def test_a_bill_of_materials_line_without_a_predecessor_is_rejected(neo4j_session):
    # Without basedOn there is no order confirmation to find the assets through.
    await create_master_data(neo4j_session)
    await bom_product(neo4j_session)

    with pytest.raises(BusinessLogicError, match="basedOn"):
        await document_with(neo4j_session, "DeliveryNote", ("ACME-1000", 1, "5000.00"))


# ==========================================
# ASSET DRAFTS ON TOP OF A REAL ORDER CONFIRMATION
# ==========================================
# What stood on the order confirmation was reserved there. If engineering adds something
# beyond it on confirmation, the difference has to be reserved in the main warehouse —
# otherwise goods leave the house that were never bound. The comparison happens in
# `excess_over_document`; these tests show whether the booking arrives in the graph.

async def confirmation_for_new_asset(session, lines: list[tuple[str, float]] | None = None) -> str:
    """Builds an order confirmation marked as a new asset; every line product has stock.

    Ordinary products (`stockEffect='direct'`) reserve with the order confirmation itself,
    independently of `assetPurpose` — without stock the order confirmation would already
    fail, long before the draft gets confirmed.
    """
    lines = lines or [("ACME-2003", 1), ("ACME-2002", 1)]
    await create_master_data(session)
    await create_order(session, "2026-0734")
    for product, _ in lines:
        await create_product(session, product)
        await create_stock(session, product, "1", quantity=100.0)
    confirmation = await document_with(
        session, "OrderConfirmation",
        *[(product, quantity, "10.00") for product, quantity in lines],
        orderProjectNumber="2026-0734", assetPurpose="newAsset",
    )
    return confirmation.number


def confirm(document_number: str, *components: tuple[str, str]) -> AssetDraftConfirmation:
    return AssetDraftConfirmation(
        documentNumber=document_number,
        components=[
            ComponentLineCreate(productNumber=number, quantity=Decimal(quantity))
            for number, quantity in components
        ],
    )


@pytest.mark.asyncio
async def test_a_draft_appears_for_an_order_confirmation_marked_as_a_new_asset(neo4j_session):
    # Sales alone decides assetPurpose — no product stands for a whole asset, so none is a
    # precondition of a draft.
    confirmation_number = await confirmation_for_new_asset(
        neo4j_session, [("ACME-2003", 1), ("ACME-2002", 1), ("ACME-2008", 1)]
    )

    drafts = await AssetRepository.get_asset_drafts(neo4j_session)

    assert [d.documentNumber for d in drafts] == [confirmation_number]
    assert {line.productNumber: line.quantity for line in drafts[0].lines} == {
        "ACME-2003": 1, "ACME-2002": 1, "ACME-2008": 1,
    }


@pytest.mark.asyncio
async def test_no_draft_without_an_asset_purpose(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2003")
    await create_stock(neo4j_session, "ACME-2003", "1", quantity=10.0)
    await document_with(neo4j_session, "OrderConfirmation", ("ACME-2003", 1, "12.00"))

    assert await AssetRepository.get_asset_drafts(neo4j_session) == []


@pytest.mark.asyncio
async def test_confirming_creates_the_asset_without_reserving_again(neo4j_session):
    confirmation_number = await confirmation_for_new_asset(neo4j_session)

    created = await AssetRepository.confirm_draft(
        confirm(confirmation_number, ("ACME-2002", "1")), "2", neo4j_session
    )

    assert [c.status for c in created] == ["released"]
    asset = await AssetRepository.get_asset(created[0].serialNumber, neo4j_session)
    assert asset is not None
    assert [c.productNumber for c in asset.components] == ["ACME-2002"]
    assert asset.productNumber is None
    # No second reservation: ACME-2002 stood on the order confirmation and reserved there
    # already — the confirmation books nothing on top.
    assert await read_stock(neo4j_session, "ACME-2002") == (100.0, 1.0)
    assert await AssetRepository.get_asset_drafts(neo4j_session) == []


@pytest.mark.asyncio
async def test_confirming_reserves_a_component_added_by_engineering(neo4j_session):
    confirmation_number = await confirmation_for_new_asset(neo4j_session)
    # Stands on no line of the document, so has reserved nothing yet.
    await create_product(neo4j_session, "ACME-2008", "Added afterwards")
    await create_stock(neo4j_session, "ACME-2008", "1", quantity=50.0)

    await AssetRepository.confirm_draft(
        confirm(confirmation_number, ("ACME-2002", "1"), ("ACME-2008", "2")), "2", neo4j_session
    )

    assert await read_stock(neo4j_session, "ACME-2008") == (50.0, 2.0)
    # The line that stood on the document stays at its one reservation.
    assert await read_stock(neo4j_session, "ACME-2002") == (100.0, 1.0)


@pytest.mark.asyncio
async def test_confirming_reserves_only_the_difference_of_a_raised_quantity(neo4j_session):
    # 1 on the document, 3 confirmed — 2 have to be reserved on top, not 3.
    confirmation_number = await confirmation_for_new_asset(neo4j_session)

    await AssetRepository.confirm_draft(
        confirm(confirmation_number, ("ACME-2002", "3")), "2", neo4j_session
    )

    assert await read_stock(neo4j_session, "ACME-2002") == (100.0, 3.0)


@pytest.mark.asyncio
async def test_a_lower_quantity_releases_nothing(neo4j_session):
    # Releasing is the cancellation's business, not the draft confirmation's: the order
    # confirmation stays in place unchanged, its reservation must not be touched here.
    confirmation_number = await confirmation_for_new_asset(neo4j_session, [("ACME-2002", 5)])

    await AssetRepository.confirm_draft(
        confirm(confirmation_number, ("ACME-2002", "2")), "2", neo4j_session
    )

    # 5 from the document line, not 2 — and not 7 either.
    assert await read_stock(neo4j_session, "ACME-2002") == (100.0, 5.0)


@pytest.mark.asyncio
async def test_components_named_twice_count_together(neo4j_session):
    # Two rows of the same product have to be compared with the document quantity as a sum,
    # otherwise every row counts as covered on its own and the excess disappears.
    confirmation_number = await confirmation_for_new_asset(neo4j_session, [("ACME-2002", 2)])

    await AssetRepository.confirm_draft(
        confirm(confirmation_number, ("ACME-2002", "2"), ("ACME-2002", "1")), "2", neo4j_session
    )

    # 2 covered by the document, 1 reserved on top.
    assert await read_stock(neo4j_session, "ACME-2002") == (100.0, 3.0)


@pytest.mark.asyncio
async def test_exactly_sufficient_stock_is_not_rejected(neo4j_session):
    # The bound still belongs to the allowed range: the check compares with "<", not "<=".
    confirmation_number = await confirmation_for_new_asset(neo4j_session)
    await create_product(neo4j_session, "ACME-2008", "Exactly enough")
    await create_stock(neo4j_session, "ACME-2008", "1", quantity=5.0)

    created = await AssetRepository.confirm_draft(
        confirm(confirmation_number, ("ACME-2002", "1"), ("ACME-2008", "5")), "2", neo4j_session
    )

    assert len(created) == 1
    assert await read_stock(neo4j_session, "ACME-2008") == (5.0, 5.0)


@pytest.mark.asyncio
async def test_missing_stock_for_the_excess_prevents_the_asset(neo4j_session):
    # All or nothing, as on the bill-of-materials release. A reservation caps at the
    # available stock by itself instead of rejecting — right on an order confirmation, where
    # the rest stays as an open quantity and shows up in the reorder suggestion. Here neither
    # exists, so confirm_draft checks beforehand and rejects.
    confirmation_number = await confirmation_for_new_asset(neo4j_session)
    await create_product(neo4j_session, "ACME-2008", "Scarce")
    await create_stock(neo4j_session, "ACME-2008", "1", quantity=1.0)

    with pytest.raises(BusinessLogicError, match="ACME-2008"):
        await AssetRepository.confirm_draft(
            confirm(confirmation_number, ("ACME-2002", "1"), ("ACME-2008", "5")), "2", neo4j_session
        )

    # Neither booking nor asset — the draft stands there unchanged afterwards.
    assert await read_stock(neo4j_session, "ACME-2008") == (1.0, 0.0)
    assert await AssetRepository.get_asset_drafts(neo4j_session) != []


# ==========================================
# FOLLOW-UP DELIVERY — the whole chain
# ==========================================

@pytest.mark.asyncio
async def test_follow_up_delivery_two_delivery_notes_one_invoice(neo4j_session):
    """The full course: an order confirmation over 30 pieces with 20 available -> a
    delivery note over the available 20 -> a goods receipt brings 10 more -> a second
    delivery note over the remaining 10 -> one invoice over both delivery notes.

    The second delivery note shows at the same time that an issue checks against the
    quantity and not against a reservation made beforehand — the second 10 were never
    reserved.
    """
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_stock(neo4j_session, "ACME-2007", "1", quantity=20.0)
    await create_order(neo4j_session, "2026-0734")

    confirmation = await document_with(
        neo4j_session, "OrderConfirmation", ("ACME-2007", 30, "10.00"),
        orderProjectNumber="2026-0734",
    )
    assert await read_stock(neo4j_session, "ACME-2007") == (20.0, 20.0)

    first = await document_with(
        neo4j_session, "DeliveryNote", ("ACME-2007", 20, "10.00"),
        orderProjectNumber="2026-0734", basedOn=[confirmation.number],
    )
    assert first.number == "DN-2026-0734"
    assert await read_stock(neo4j_session, "ACME-2007") == (0.0, 0.0)

    # A goods receipt brings 10 more — through a purchase order of its own, as in reality.
    purchase_order = await document_with(neo4j_session, "PurchaseOrder", ("ACME-2007", 10, "8.00"))
    await DocumentRepository.post_goods_receipt(
        purchase_order.number,
        GoodsReceiptCreate.model_validate(
            {"deliveryNoteNumber": "SUP-DN-1", "lines": [{"lineNumber": 1, "quantity": 10}]}
        ),
        "3", neo4j_session,
    )
    assert await read_stock(neo4j_session, "ACME-2007") == (10.0, 0.0)

    # The second delivery note needs no reservation of its own — it issues against the goods
    # that just arrived.
    second = await document_with(
        neo4j_session, "DeliveryNote", ("ACME-2007", 10, "10.00"),
        orderProjectNumber="2026-0734", basedOn=[confirmation.number],
    )
    assert second.number == "DN-2026-0734-2"
    assert await read_stock(neo4j_session, "ACME-2007") == (0.0, 0.0)

    invoice = await document_with(
        neo4j_session, "Invoice", ("ACME-2007", 30, "10.00"),
        orderProjectNumber="2026-0734", basedOn=[first.number, second.number],
    )
    assert invoice.number == "IN-2026-0734"
    assert invoice.basedOn == sorted([first.number, second.number])
    # Both predecessors are delivery notes — the invoice therefore does not issue again.
    assert await movements_of(neo4j_session, invoice.number) == []
    assert await read_stock(neo4j_session, "ACME-2007") == (0.0, 0.0)

    states = [
        (await DocumentRepository.get_document(number, neo4j_session)).status  # type: ignore[union-attr]
        for number in (first.number, second.number)
    ]
    assert states == ["completed", "completed"]

    # Another order confirmation over the now used-up product finds nothing available and
    # is rejected.
    with pytest.raises(BusinessLogicError):
        await document_with(neo4j_session, "OrderConfirmation", ("ACME-2007", 1, "10.00"))

    # Cancelling the first delivery note is locked while the shared invoice is active — the
    # chain lock holds through a fan-in as well.
    with pytest.raises(BusinessLogicError):
        await DocumentRepository.update_document(
            first.number, DocumentUpdate(status="cancelled"), "1", neo4j_session
        )


# ==========================================
# GOODS RECEIPT CONTROL
# ==========================================

async def build_purchase_order(session) -> None:
    """Builds the purchase order the ordered/received comparison runs against.

    Four lines, so all four delivery statuses show on one document: one line gets
    delivered in full, one only in part, one over-delivered — and a fourth stays without
    any delivery.
    """
    await create_master_data(session)
    for number in ("ACME-2007", "ACME-2002", "ACME-2003", "ACME-2008"):
        await create_product(session, number, label=f"Product {number}")
    await create_raw_document(session, "PO-2026-0001", "PurchaseOrder", supplierId="S-001")
    await create_raw_line(session, "PO-2026-0001", "ACME-2007", 25, 8500, lineNumber=1)
    await create_raw_line(session, "PO-2026-0001", "ACME-2002", 500, 6, lineNumber=2)
    await create_raw_line(session, "PO-2026-0001", "ACME-2003", 10, 1000, lineNumber=3)
    await create_raw_line(session, "PO-2026-0001", "ACME-2008", 7, 500, lineNumber=4)


async def receive(session, purchase_order: str, delivery_note: str, *lines, employee: str = "3"):
    """Books a goods receipt; `lines` are (lineNumber, quantity[, purchasePrice])."""
    return await DocumentRepository.post_goods_receipt(
        purchase_order,
        GoodsReceiptCreate.model_validate({
            "deliveryNoteNumber": delivery_note,
            "lines": [
                {"lineNumber": line[0], "quantity": line[1],
                 **({"purchasePrice": line[2]} if len(line) > 2 else {})}
                for line in lines
            ],
        }),
        employee, session,
    )


def status_of(result, productNumber: str):
    """Looks the result row of a product up."""
    return next(line for line in result.lines if line.productNumber == productNumber)


@pytest.mark.asyncio
async def test_the_comparison_knows_all_four_delivery_statuses(neo4j_session):
    # One line complete, one partial with an open quantity, one over-delivered with open
    # quantity 0 — and one to which nothing arrived. That one is the most important row of
    # the control.
    await build_purchase_order(neo4j_session)

    result = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 25), (2, 300), (3, 12))

    assert (status_of(result, "ACME-2007").deliveryStatus, status_of(result, "ACME-2007").openQuantity) == ("Complete", 0)
    assert (status_of(result, "ACME-2002").deliveryStatus, status_of(result, "ACME-2002").openQuantity) == ("Partial", 200)
    assert (status_of(result, "ACME-2003").deliveryStatus, status_of(result, "ACME-2003").openQuantity) == ("Over", 0)
    assert (status_of(result, "ACME-2008").deliveryStatus, status_of(result, "ACME-2008").openQuantity) == ("Pending", 7)


@pytest.mark.asyncio
async def test_the_comparison_names_every_line_of_the_purchase_order(neo4j_session):
    await build_purchase_order(neo4j_session)

    result = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 1))

    assert [line.productNumber for line in result.lines] == ["ACME-2007", "ACME-2002", "ACME-2003", "ACME-2008"]
    assert result.purchaseOrderNumber == "PO-2026-0001"


@pytest.mark.asyncio
async def test_the_goods_receipt_creates_its_document(neo4j_session):
    # Type GoodsReceipt, yearly number, BASED_ON on the purchase order and the supplier of
    # the predecessor — otherwise the purchasing filter would not find it.
    await build_purchase_order(neo4j_session)

    result = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 25))

    year = datetime.now(UTC).year
    assert result.goodsReceiptNumber == f"GR-{year}-0001"

    document = await DocumentRepository.get_document(result.goodsReceiptNumber, neo4j_session)
    assert document is not None
    assert document.type == "GoodsReceipt"
    assert document.basedOn == ["PO-2026-0001"]
    assert document.supplier is not None
    assert document.supplier.id == "S-001"
    assert document.createdBy is not None
    assert await labels_of(neo4j_session, result.goodsReceiptNumber) == {"Document", "GoodsReceipt"}


@pytest.mark.asyncio
async def test_the_goods_receipt_document_carries_the_delivered_quantities(neo4j_session):
    await build_purchase_order(neo4j_session)

    result = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 20, "90.00"))

    document = await DocumentRepository.get_document(result.goodsReceiptNumber, neo4j_session)
    assert document is not None
    assert len(document.lines) == 1
    assert document.lines[0].quantity == 20
    assert document.lines[0].unitPrice == Decimal("90.00")
    assert document.lines[0].priceOverridden is True


@pytest.mark.asyncio
async def test_the_bookings_hang_off_the_purchase_order_and_carry_the_price(neo4j_session):
    # Deliberately off the PURCHASE ORDER: only through it does the comparison find the
    # bookings again, those of earlier deliveries included. Without a price on the movement
    # the receipt would stay invisible in the average cost price.
    await build_purchase_order(neo4j_session)

    result = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 25), (2, 300, "0.07"))

    on_the_purchase_order = await movements_of(neo4j_session, "PO-2026-0001")
    assert [m["productNumber"] for m in on_the_purchase_order] == ["ACME-2002", "ACME-2007"]
    assert all(m["type"] == "Receipt" for m in on_the_purchase_order)
    prices = {m["productNumber"]: m["purchasePriceCent"] for m in on_the_purchase_order}
    # Without a price of its own the price of the purchase order line applies (85.00 EUR).
    assert prices == {"ACME-2007": 8500, "ACME-2002": 7}
    assert await movements_of(neo4j_session, result.goodsReceiptNumber) == []


@pytest.mark.asyncio
async def test_the_goods_receipt_raises_the_stock_of_the_main_warehouse(neo4j_session):
    await build_purchase_order(neo4j_session)

    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 25))

    assert await read_stock(neo4j_session, "ACME-2007") == (25.0, 0.0)


@pytest.mark.asyncio
async def test_two_partial_deliveries_with_different_delivery_notes_add_up(neo4j_session):
    # Two GENUINE partial deliveries to the same purchase order stay possible — they carry
    # different delivery note numbers. At an order of 25, twice 15 is an over-delivery.
    await build_purchase_order(neo4j_session)

    first = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 15))
    second = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-2", (1, 15))

    assert status_of(first, "ACME-2007").deliveryStatus == "Partial"
    assert status_of(second, "ACME-2007").receivedQuantity == 30
    assert status_of(second, "ACME-2007").deliveryStatus == "Over"
    # A follow-up delivery to the same purchase order: base number of the first delivery
    # plus a counter, not simply the next free yearly number.
    assert second.goodsReceiptNumber == f"{first.goodsReceiptNumber}-2"
    # The line is complete now, but the purchase order carries three more lines never
    # delivered — it therefore stays open.
    assert second.purchaseOrderStatus == "open"


@pytest.mark.asyncio
async def test_a_partial_delivery_keeps_the_purchase_order_open_until_everything_is_there(neo4j_session):
    await build_purchase_order(neo4j_session)

    first = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 25))
    assert first.purchaseOrderStatus == "open"
    document = await DocumentRepository.get_document("PO-2026-0001", neo4j_session)
    assert document is not None and document.status == "open"

    last = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-2", (2, 500), (3, 10), (4, 7))

    assert last.purchaseOrderStatus == "completed"
    document = await DocumentRepository.get_document("PO-2026-0001", neo4j_session)
    assert document is not None and document.status == "completed"


@pytest.mark.asyncio
async def test_a_third_delivery_to_the_same_purchase_order_gets_suffix_3(neo4j_session):
    await build_purchase_order(neo4j_session)

    first = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 10))
    second = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-2", (1, 10))
    third = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-3", (1, 5))

    assert second.goodsReceiptNumber == f"{first.goodsReceiptNumber}-2"
    assert third.goodsReceiptNumber == f"{first.goodsReceiptNumber}-3"


@pytest.mark.asyncio
async def test_the_same_delivery_note_does_not_book_twice(neo4j_session):
    # The same delivery note to the same purchase order must not produce an over-delivery,
    # however often the call is repeated (double click, second tab, flaky connection) — and
    # a repeat draws no follow-up number either.
    await build_purchase_order(neo4j_session)

    first = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 15))
    second = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 15))
    third = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 15))

    assert first.goodsReceiptNumber == second.goodsReceiptNumber == third.goodsReceiptNumber
    assert status_of(second, "ACME-2007").receivedQuantity == 15
    assert status_of(second, "ACME-2007").deliveryStatus == "Partial"
    assert await count(neo4j_session, "MATCH (d:GoodsReceipt) RETURN count(d)") == 1
    assert await count(neo4j_session, "MATCH (m:StockMovement {type: 'Receipt'}) RETURN count(m)") == 1


@pytest.mark.asyncio
async def test_the_same_delivery_note_to_different_purchase_orders_is_allowed(neo4j_session):
    # The uniqueness constraint is deliberately composite (purchaseOrderNumber,
    # deliveryNoteNumber) — two suppliers may happen to use the same numbering.
    await build_purchase_order(neo4j_session)
    await create_raw_document(neo4j_session, "PO-2026-0002", "PurchaseOrder", supplierId="S-001")
    await create_raw_line(neo4j_session, "PO-2026-0002", "ACME-2002", 10, 100, lineNumber=1)

    first = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-SHARED", (1, 5))
    second = await receive(neo4j_session, "PO-2026-0002", "SUP-DN-SHARED", (1, 5))

    assert first.goodsReceiptNumber != second.goodsReceiptNumber
    assert await count(neo4j_session, "MATCH (d:GoodsReceipt) RETURN count(d)") == 2


@pytest.mark.asyncio
async def test_a_goods_receipt_against_a_cancelled_purchase_order_keeps_its_status(neo4j_session):
    # post_goods_receipt does not check the purchase order status itself (follow-up
    # deliveries stay possible regardless) — but the automatic switch to 'completed' must not
    # overwrite a purchase order cancelled in the meantime.
    await build_purchase_order(neo4j_session)
    cancelled = await DocumentRepository.update_document(
        "PO-2026-0001", DocumentUpdate(status="cancelled"), "1", neo4j_session
    )
    assert cancelled is not None and cancelled.status == "cancelled"

    result = await receive(
        neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 25), (2, 500), (3, 10), (4, 7)
    )

    assert result.purchaseOrderStatus == "cancelled"
    document = await DocumentRepository.get_document("PO-2026-0001", neo4j_session)
    assert document is not None and document.status == "cancelled"


@pytest.mark.asyncio
async def test_earlier_receipts_count_as_well(neo4j_session):
    # The comparison runs over FULFILS edges, not over the stock movements directly. An
    # earlier receipt that exists only as a line with a FULFILS edge — without any API call
    # having run — has to count.
    await build_purchase_order(neo4j_session)
    await neo4j_session.run(
        """
        MATCH (orderLine:DocumentLine {id: 'PO-2026-0001_1'})
        CREATE (earlier:DocumentLine {id: 'EARLIER-GR_1', quantity: 25.0})
        CREATE (earlier)-[:FULFILS]->(orderLine)
        """
    )

    result = await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (2, 1))

    assert status_of(result, "ACME-2007").receivedQuantity == 25
    assert status_of(result, "ACME-2007").deliveryStatus == "Complete"


@pytest.mark.asyncio
async def test_a_goods_receipt_against_an_invoice_is_rejected(neo4j_session):
    # Meaningless in business terms — and a 400, not a 422: the document type sits in the
    # graph, not in the request.
    await build_purchase_order(neo4j_session)
    await create_raw_document(neo4j_session, "IN-2026-0734", "Invoice", customerId="C-1001")

    with pytest.raises(BusinessLogicError):
        await receive(neo4j_session, "IN-2026-0734", "SUP-DN-1", (1, 1))

    assert await count(neo4j_session, "MATCH (m:StockMovement) RETURN count(m)") == 0


@pytest.mark.asyncio
async def test_an_unknown_line_number_is_rejected(neo4j_session):
    await build_purchase_order(neo4j_session)

    with pytest.raises(BusinessLogicError):
        await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (999, 1))

    assert await count(neo4j_session, "MATCH (d:GoodsReceipt) RETURN count(d)") == 0


@pytest.mark.asyncio
async def test_an_unknown_employee_prevents_the_booking(neo4j_session):
    await build_purchase_order(neo4j_session)

    with pytest.raises(NotFoundError):
        await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 1), employee="99")

    assert await count(neo4j_session, "MATCH (m:StockMovement) RETURN count(m)") == 0


@pytest.mark.asyncio
async def test_a_goods_receipt_against_an_unknown_purchase_order_is_not_found(neo4j_session):
    await build_purchase_order(neo4j_session)

    with pytest.raises(NotFoundError):
        await receive(neo4j_session, "PO-9999", "SUP-DN-1", (1, 1))


@pytest.mark.asyncio
async def test_cancelling_a_purchase_order_reverses_its_receipts(neo4j_session):
    # The receipts hang off the purchase order line by line — which is exactly what lets the
    # cancellation find them again.
    await build_purchase_order(neo4j_session)
    await receive(neo4j_session, "PO-2026-0001", "SUP-DN-1", (1, 25))
    assert await read_stock(neo4j_session, "ACME-2007") == (25.0, 0.0)
    # The goods receipt document is the follow-up of the purchase order and would block the
    # cancellation through the chain lock. It books nothing itself — the bookings sit on the
    # purchase order — so marking it cancelled directly is enough to clear the way.
    await neo4j_session.run("MATCH (gr:GoodsReceipt) SET gr.status = 'cancelled'")

    await DocumentRepository.update_document(
        "PO-2026-0001", DocumentUpdate(status="cancelled"), "1", neo4j_session
    )

    assert await read_stock(neo4j_session, "ACME-2007") == (0.0, 0.0)


# ==========================================
# ContractRepository
# ==========================================

@pytest.mark.asyncio
async def test_get_contracts_returns_the_assigned_customers(neo4j_session):
    await create_master_data(neo4j_session)
    await create_contract(neo4j_session, "1", "Test contract", isGlobal=False)
    await link_customer_contract(neo4j_session, "C-1001", "1")

    contracts = await ContractRepository.get_contracts(neo4j_session)

    hit = next(c for c in contracts if c.id == "1")
    assert [c.id for c in hit.customers] == ["C-1001"]
    assert hit.active is True


@pytest.mark.asyncio
async def test_get_contracts_a_global_contract_has_an_empty_customer_list(neo4j_session):
    await create_contract(neo4j_session, "2", "Global campaign", isGlobal=True)

    contracts = await ContractRepository.get_contracts(neo4j_session)

    hit = next(c for c in contracts if c.id == "2")
    assert hit.customers == []


@pytest.mark.asyncio
async def test_an_expired_contract_is_not_active(neo4j_session):
    # `active` is calculated from the validity dates, not stored.
    await create_contract(
        neo4j_session, "3", "Last year", isGlobal=False,
        validFrom=date(2024, 1, 1), validTo=date(2024, 12, 31),
    )

    contract = await ContractRepository.get_contract("3", neo4j_session)

    assert contract is not None
    assert contract.active is False


@pytest.mark.asyncio
async def test_get_contract_returns_its_conditions(neo4j_session):
    await create_product(neo4j_session, "ACME-1000", "Starter Kit A")
    await create_contract(neo4j_session, "1", "Test contract", isGlobal=False)
    await create_condition(neo4j_session, "1", "ACME-1000", fixedPriceCent=9900)

    contract = await ContractRepository.get_contract("1", neo4j_session)

    assert contract is not None
    condition = next(c for c in contract.conditions if c.productNumber == "ACME-1000")
    assert condition.fixedPrice == Decimal("99.00")
    assert condition.label == "Starter Kit A"


@pytest.mark.asyncio
async def test_get_contract_unknown_returns_none(neo4j_session):
    assert await ContractRepository.get_contract("does-not-exist", neo4j_session) is None


@pytest.mark.asyncio
async def test_create_contract_assigns_a_uuid_id(neo4j_session):
    data = ContractCreate.model_validate(
        {"name": "Annual contract", "validFrom": "2026-01-01", "validTo": "2026-12-31", "isGlobal": False}
    )

    created = await ContractRepository.create_contract(data, neo4j_session)

    assert created.id.startswith("contract-")
    assert created.name == "Annual contract"
    assert (created.customers, created.conditions) == ([], [])


@pytest.mark.asyncio
async def test_adding_a_condition_stores_the_fixed_price(neo4j_session):
    await create_product(neo4j_session, "ACME-2007", "Vibration Sensor")
    await create_contract(neo4j_session, "1", "Test contract", isGlobal=False)

    contract = await ContractRepository.add_condition(
        "1", ConditionCreate(productNumber="ACME-2007", fixedPrice=Decimal("99.00")), neo4j_session
    )

    assert contract is not None
    condition = next(c for c in contract.conditions if c.productNumber == "ACME-2007")
    assert condition.fixedPrice == Decimal("99.00")


@pytest.mark.asyncio
async def test_adding_a_condition_twice_overwrites_instead_of_duplicating(neo4j_session):
    await create_product(neo4j_session, "ACME-2007", "Vibration Sensor")
    await create_contract(neo4j_session, "1", "Test contract", isGlobal=False)

    await ContractRepository.add_condition(
        "1", ConditionCreate(productNumber="ACME-2007", fixedPrice=Decimal("99.00")), neo4j_session
    )
    contract = await ContractRepository.add_condition(
        "1", ConditionCreate(productNumber="ACME-2007", fixedPrice=Decimal("89.00")), neo4j_session
    )

    assert contract is not None
    assert [(c.productNumber, c.fixedPrice) for c in contract.conditions] == [("ACME-2007", Decimal("89.00"))]


@pytest.mark.asyncio
async def test_adding_a_condition_to_an_unknown_contract_returns_none(neo4j_session):
    await create_product(neo4j_session, "ACME-2004", "Pump Unit")

    assert await ContractRepository.add_condition(
        "does-not-exist", ConditionCreate(productNumber="ACME-2004", fixedPrice=Decimal("50.00")),
        neo4j_session,
    ) is None


@pytest.mark.asyncio
async def test_adding_a_condition_for_an_unknown_product_is_not_found(neo4j_session):
    await create_contract(neo4j_session, "1", "Test contract", isGlobal=False)

    with pytest.raises(NotFoundError):
        await ContractRepository.add_condition(
            "1", ConditionCreate(productNumber="does-not-exist", fixedPrice=Decimal("50.00")),
            neo4j_session,
        )


# ==========================================
# PriceCalculationRepository — the raw data of the product axis
# ==========================================
# Picking among the candidates (the best price) is played through in the unit tests of the
# service and not repeated here. The subject here is solely whether the right candidates
# come out of the graph at all.

def pricing_request(customerId: str, productNumber: str, on: str) -> PriceCalculationRequest:
    return PriceCalculationRequest.model_validate(
        {"customerId": customerId, "productNumber": productNumber, "date": on}
    )


@pytest.mark.asyncio
async def test_the_calculation_reads_a_contract_discount(neo4j_session):
    # The contract carries a flat 10 % and applies to every product of it, not only to
    # those with a condition of their own.
    await create_master_data(neo4j_session)
    await create_priced_product(neo4j_session, "ACME-1000", listPriceCent=500000)
    await create_contract(
        neo4j_session, "1", "10% on assets", isGlobal=False, discountPercent=10.0,
        validFrom=date(2026, 1, 1), validTo=date(2026, 12, 31),
    )
    await link_customer_contract(neo4j_session, "C-1001", "1")

    basis = await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-1001", "ACME-1000", "2026-07-01"), neo4j_session
    )

    assert basis is not None
    assert basis.basePrice == Decimal("5000.00")
    assert basis.discountable is True
    assert [(c.type, c.percent) for c in basis.candidates] == [("ContractDiscount", 10.0)]


@pytest.mark.asyncio
async def test_a_fixed_price_condition_replaces_the_flat_contract_rate(neo4j_session):
    # At most one candidate comes out of a contract: a fixed price for the product beats the
    # flat rate of the same contract.
    await create_master_data(neo4j_session)
    await create_priced_product(neo4j_session, "ACME-1000", listPriceCent=500000)
    await create_contract(
        neo4j_session, "1", "Contract", isGlobal=False, discountPercent=10.0,
        validFrom=date(2026, 1, 1), validTo=date(2026, 12, 31),
    )
    await create_condition(neo4j_session, "1", "ACME-1000", fixedPriceCent=420000)
    await link_customer_contract(neo4j_session, "C-1001", "1")

    basis = await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-1001", "ACME-1000", "2026-07-01"), neo4j_session
    )

    assert basis is not None
    assert [(c.type, c.fixedPrice) for c in basis.candidates] == [("ContractFixedPrice", Decimal("4200.00"))]


@pytest.mark.asyncio
async def test_a_missing_discountable_flag_counts_as_true(neo4j_session):
    await create_master_data(neo4j_session)
    await create_priced_product(neo4j_session, "ACME-1000", listPriceCent=500000, discountable=None)

    basis = await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-1001", "ACME-1000", "2026-07-01"), neo4j_session
    )

    assert basis is not None
    assert basis.discountable is True


@pytest.mark.asyncio
async def test_the_calculation_reads_both_candidates_on_a_collision(neo4j_session):
    # Product and group discount both apply on 2025-06-01. Which one wins is the business of
    # the service — here only both candidates making it out of the graph counts.
    await create_master_data(neo4j_session)
    await create_priced_product(neo4j_session, "ACME-2003", listPriceCent=2900)
    await assign_product_group(neo4j_session, "ACME-2003", subcategoryId=39, productGroupId=5)
    await create_discount(neo4j_session, "2", 12.0, "2025-01-01", "2026-12-31", productNumber="ACME-2003")
    await create_discount(neo4j_session, "1", 5.0, "2025-01-01", "2025-12-31", productGroupId=5)

    basis = await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-1001", "ACME-2003", "2025-06-01"), neo4j_session
    )

    assert basis is not None
    assert sorted((c.type, c.percent) for c in basis.candidates) == [
        ("GroupDiscount", 5.0), ("ProductDiscount", 12.0),
    ]


@pytest.mark.asyncio
async def test_an_expired_group_discount_drops_out(neo4j_session):
    # The same product, but on 2026-06-01: the group discount ended on 2025-12-31.
    await create_master_data(neo4j_session)
    await create_priced_product(neo4j_session, "ACME-2003", listPriceCent=2900)
    await assign_product_group(neo4j_session, "ACME-2003", subcategoryId=39, productGroupId=5)
    await create_discount(neo4j_session, "2", 12.0, "2025-01-01", "2026-12-31", productNumber="ACME-2003")
    await create_discount(neo4j_session, "1", 5.0, "2025-01-01", "2025-12-31", productGroupId=5)

    basis = await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-1001", "ACME-2003", "2026-06-01"), neo4j_session
    )

    assert basis is not None
    assert [c.percent for c in basis.candidates] == [12.0]


@pytest.mark.asyncio
async def test_a_global_contract_applies_without_an_assignment(neo4j_session):
    # A global contract needs no HAS_CONTRACT edge to the customer.
    await create_another_customer(neo4j_session, "C-1002", "Sample Logistics AG")
    await create_priced_product(neo4j_session, "ACME-1000", listPriceCent=1308481)
    await create_contract(
        neo4j_session, "2", "15% summer campaign", isGlobal=True, discountPercent=15.0,
        validFrom=date(2026, 6, 1), validTo=date(2026, 8, 31),
    )

    basis = await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-1002", "ACME-1000", "2026-07-01"), neo4j_session
    )

    assert basis is not None
    assert basis.basePrice == Decimal("13084.81")
    assert [c.percent for c in basis.candidates] == [15.0]


@pytest.mark.asyncio
async def test_a_contract_outside_its_validity_on_the_request_date_drops_out(neo4j_session):
    # Validity is checked against the date of the request, not against today.
    await create_another_customer(neo4j_session, "C-1002", "Sample Logistics AG")
    await create_priced_product(neo4j_session, "ACME-1000", listPriceCent=1308481)
    await create_contract(
        neo4j_session, "2", "15% summer campaign", isGlobal=True, discountPercent=15.0,
        validFrom=date(2026, 6, 1), validTo=date(2026, 8, 31),
    )

    basis = await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-1002", "ACME-1000", "2026-09-01"), neo4j_session
    )

    assert basis is not None
    assert basis.candidates == []


@pytest.mark.asyncio
async def test_a_customer_without_a_contract_has_an_empty_candidate_list(neo4j_session):
    # No 404: a customer without a contract is the normal case, not an error.
    await create_another_customer(neo4j_session, "C-1003", "Demo Retail Ltd")
    await create_priced_product(neo4j_session, "ACME-2007", listPriceCent=13134)

    basis = await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-1003", "ACME-2007", "2026-07-01"), neo4j_session
    )

    assert basis is not None
    assert basis.candidates == []


@pytest.mark.asyncio
async def test_the_calculation_for_an_unknown_customer_returns_none(neo4j_session):
    await create_priced_product(neo4j_session, "ACME-2007", listPriceCent=13134)

    assert await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-9999", "ACME-2007", "2026-07-01"), neo4j_session
    ) is None


@pytest.mark.asyncio
async def test_the_calculation_for_an_unknown_product_returns_none(neo4j_session):
    await create_master_data(neo4j_session)

    assert await PriceCalculationRepository.read_pricing_basis(
        pricing_request("C-1001", "does-not-exist", "2026-07-01"), neo4j_session
    ) is None


@pytest.mark.asyncio
async def test_a_document_combines_line_discount_and_order_discount(neo4j_session):
    # 15 % on the line and 2 % on the order make neither 17 % (addition) nor the best price
    # of 15 % (wrong choice of axis), but the two-stage calculation: line amount first, the
    # order discount on the subtotal after it.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-1000", label="Starter Kit A")

    document = await DocumentService.create_document(
        new_document(
            type="Quote",
            orderDiscountPercent=2,
            taxPercent=20,
            lines=[{
                "productNumber": "ACME-1000", "quantity": 1,
                "unitPrice": "13084.81", "discountPercent": 15,
            }],
        ),
        "1", neo4j_session,
    )

    assert document.subtotal == Decimal("11122.09")
    assert document.orderDiscountAmount == Decimal("222.44")
    assert document.totalNet == Decimal("10899.65")
    assert document.totalGross == Decimal("13079.58")


@pytest.mark.asyncio
async def test_a_fixed_price_line_carries_no_order_discount(neo4j_session):
    # The fixed price takes precedence — through the whole stack (Cypher write, reading
    # back, totals), not only the pure function in isolation.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007")
    await create_product(neo4j_session, "ACME-1000", label="Starter Kit A")

    document = await DocumentService.create_document(
        new_document(
            type="Quote",
            orderDiscountPercent=5,
            lines=[
                {"productNumber": "ACME-1000", "quantity": 1, "unitPrice": "80.00", "hasFixedPrice": True},
                {"productNumber": "ACME-2007", "quantity": 1, "unitPrice": "100.00"},
            ],
        ),
        "1", neo4j_session,
    )

    read = await DocumentRepository.get_document(document.number, neo4j_session)
    assert read is not None
    fixed_price_line = next(line for line in read.lines if line.productNumber == "ACME-1000")
    assert fixed_price_line.hasFixedPrice is True

    assert document.subtotal == Decimal("180.00")
    assert document.orderDiscountAmount == Decimal("5.00")
    assert document.totalNet == Decimal("175.00")


# ==========================================
# ReportRepository.revenue
# ==========================================

@pytest.mark.asyncio
async def test_revenue_subtracts_the_order_discount(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", "Vibration Sensor")
    await create_raw_document(neo4j_session, "IN-TEST-0001", "Invoice", customerId="C-1001", documentDate="2026-08-05")
    await neo4j_session.run("MATCH (d:Document {number: 'IN-TEST-0001'}) SET d.orderDiscountPercent = 3.0")
    await create_raw_line(neo4j_session, "IN-TEST-0001", "ACME-2007", 8, 13134, lineNumber=1)

    rows = await ReportRepository.revenue(date(2026, 8, 1), date(2026, 8, 31), "customer", neo4j_session)

    # 8 x 131.34 EUR = 1,050.72 EUR, less 3 % order discount (31.52 EUR, rounded
    # commercially) = 1,019.20 EUR.
    hit = next(r for r in rows if r.group == "Example Industries GmbH")
    assert hit.revenue == Decimal("1019.20")


@pytest.mark.asyncio
async def test_revenue_does_not_count_delivery_notes(neo4j_session):
    # Otherwise the same operation would be recorded twice — delivery note and the invoice
    # following it carry the same lines.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", "Vibration Sensor")
    await create_raw_document(neo4j_session, "DN-TEST-0001", "DeliveryNote", customerId="C-1001", documentDate="2026-08-05")
    await create_raw_line(neo4j_session, "DN-TEST-0001", "ACME-2007", 8, 13134, lineNumber=1)

    rows = await ReportRepository.revenue(date(2026, 8, 1), date(2026, 8, 31), "customer", neo4j_session)

    assert rows == []


@pytest.mark.asyncio
async def test_revenue_without_a_cost_price_has_no_margin(neo4j_session):
    # An incomplete margin would not be a mistake anyone notices — so none at all.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "SERVICE-1", "Product without cost price")
    await create_raw_document(neo4j_session, "IN-TEST-0002", "Invoice", customerId="C-1001", documentDate="2026-08-06")
    await create_raw_line(neo4j_session, "IN-TEST-0002", "SERVICE-1", 1, 10000, lineNumber=1)

    rows = await ReportRepository.revenue(date(2026, 8, 1), date(2026, 8, 31), "customer", neo4j_session)

    assert len(rows) == 1
    assert rows[0].totalCost is None
    assert rows[0].margin is None


@pytest.mark.asyncio
async def test_revenue_with_a_cost_price_reports_the_margin(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", "Vibration Sensor", costPriceCent=8000)
    await create_raw_document(neo4j_session, "IN-TEST-0004", "Invoice", customerId="C-1001", documentDate="2026-08-06")
    await create_raw_line(neo4j_session, "IN-TEST-0004", "ACME-2007", 2, 10000, lineNumber=1)

    rows = await ReportRepository.revenue(date(2026, 8, 1), date(2026, 8, 31), "product", neo4j_session)

    assert len(rows) == 1
    assert (rows[0].group, rows[0].revenue, rows[0].totalCost, rows[0].margin) == (
        "Vibration Sensor", Decimal("200.00"), Decimal("160.00"), Decimal("40.00"),
    )
    assert rows[0].marginPercent == 20.0


@pytest.mark.asyncio
async def test_revenue_grouped_by_product_gives_the_same_total(neo4j_session):
    # The order discount hangs off the document, not the product — it is distributed
    # proportionally over the lines, otherwise groupBy=product would report a different
    # total than groupBy=customer.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", "Vibration Sensor")
    await create_product(neo4j_session, "ACME-2002", "O-Ring 10x2")
    await create_raw_document(neo4j_session, "IN-TEST-0003", "Invoice", customerId="C-1001", documentDate="2026-08-05")
    await neo4j_session.run("MATCH (d:Document {number: 'IN-TEST-0003'}) SET d.orderDiscountPercent = 3.0")
    await create_raw_line(neo4j_session, "IN-TEST-0003", "ACME-2007", 8, 13134, lineNumber=1)
    await create_raw_line(neo4j_session, "IN-TEST-0003", "ACME-2002", 5, 20, lineNumber=2)

    by_customer = await ReportRepository.revenue(date(2026, 8, 1), date(2026, 8, 31), "customer", neo4j_session)
    by_product = await ReportRepository.revenue(date(2026, 8, 1), date(2026, 8, 31), "product", neo4j_session)

    total_by_customer = sum((r.revenue for r in by_customer), Decimal("0.00"))
    total_by_product = sum((r.revenue for r in by_product), Decimal("0.00"))
    assert total_by_customer == total_by_product


@pytest.mark.asyncio
async def test_revenue_leaves_a_fixed_price_line_undiscounted(neo4j_session):
    # The same rule as the document totals: the order discount does not reach a fixed-price
    # line.
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-1000", "Starter Kit A")
    await create_raw_document(neo4j_session, "IN-TEST-0005", "Invoice", customerId="C-1001", documentDate="2026-08-05")
    await neo4j_session.run("MATCH (d:Document {number: 'IN-TEST-0005'}) SET d.orderDiscountPercent = 10.0")
    await create_raw_line(neo4j_session, "IN-TEST-0005", "ACME-1000", 1, 10000, lineNumber=1)
    await neo4j_session.run("MATCH (l:DocumentLine {id: 'IN-TEST-0005_1'}) SET l.hasFixedPrice = true")

    rows = await ReportRepository.revenue(date(2026, 8, 1), date(2026, 8, 31), "customer", neo4j_session)

    assert rows[0].revenue == Decimal("100.00")


@pytest.mark.asyncio
async def test_revenue_grouped_by_month(neo4j_session):
    await create_master_data(neo4j_session)
    await create_product(neo4j_session, "ACME-2007", "Vibration Sensor")
    await create_raw_document(neo4j_session, "IN-JUL", "Invoice", customerId="C-1001", documentDate="2026-07-31")
    await create_raw_document(neo4j_session, "IN-AUG", "Invoice", customerId="C-1001", documentDate="2026-08-01")
    await create_raw_line(neo4j_session, "IN-JUL", "ACME-2007", 1, 1000, lineNumber=1)
    await create_raw_line(neo4j_session, "IN-AUG", "ACME-2007", 2, 1000, lineNumber=1)

    rows = await ReportRepository.revenue(date(2026, 7, 1), date(2026, 8, 31), "month", neo4j_session)

    assert [(r.group, r.revenue) for r in rows] == [
        ("2026-07", Decimal("10.00")), ("2026-08", Decimal("20.00")),
    ]
