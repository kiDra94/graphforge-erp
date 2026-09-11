"""Pydantic schemas of the catalog domain: read, write and bill-of-materials models."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.neo4j_types import Neo4jDatetime
from core.schemas import InputModel

# The two product kinds correspond to the labels :Part and :Assembly in the graph. As a
# shared type across router, service and repository, the Literal makes FastAPI reject an
# unknown value with a 422 instead of silently answering it with an empty result list.
ProductType = Literal["Part", "Assembly"]

# Steers what booking a document line with this product does to the warehouse. 'direct' is
# the normal case and therefore the default on read (via coalesce in the projection, the
# same way `active` works) — no blanket backfill is needed.
StockEffect = Literal["direct", "billOfMaterials", "none"]


class GroupRef(BaseModel):
    """A node of the category hierarchy as it appears on a product.

    Used for all three levels. `name` falls back to a placeholder built from the id when
    a level carries no maintained name, so the selection list stays usable either way.
    """
    id: int = Field(description="Id of the group.")
    name: str = Field(description="Name of the group, possibly a placeholder built from the id.")


class ProductSupplier(BaseModel):
    """A supplier offering this product, as it appears on the product.

    Reads the `SUPPLIES_PRODUCT` edge (procurement domain), embedded here for the
    comparison view in purchasing. Read-only — creating or changing a condition still goes
    through `POST /api/suppliers/{id}/products`.
    """
    supplierId: str = Field(description="Id of the supplying supplier.")
    leadTimeDays: int | None = Field(default=None, description="Lead time in days.")
    purchasePrice: Decimal | None = Field(default=None, description="Purchase price per unit in euro.")
    isPreferredSupplier: bool = Field(default=False, description="Whether this supplier is the preferred source for the product.")


class ProductBase(BaseModel):
    """The business field set of the read model — deliberately kept tolerant.

    Its only heir is `Product`, which merely adds the server-maintained timestamps and
    changes not a single type. The write models `ProductCreate` and `ProductUpdate`
    deliberately do NOT inherit from here: they tighten types (`str | None` -> `str`), and
    a subclass that changes inherited types is no longer a subtype. Pydantic tolerates it
    at runtime, type checkers reject it — rightly.

    Apart from `number` and `label`, every field is optional and carries NO value
    constraints.

    That is intentional. As the basis of the read model, a constraint here would protect
    nothing; it would merely turn an already stored database state into an HTTP 500.
    Nodes that grew over time do not satisfy the constraints — imports set neither
    `minStock` nor `targetStock`, carry products with a sales price of 0, and create thin
    nodes without a `unit` through MERGE.

    Value validation belongs at the system boundary and therefore happens exclusively in
    `ProductCreate` and `ProductUpdate`.
    """
    number: str = Field(description="The unique product number.")
    label: str = Field(description="The name or main description of the product.")
    shortText: str | None = Field(default=None, description="An optional shorter description.")
    shortTextEn: str | None = Field(default=None, description="The English short text, for documents with language='EN'.")
    unit: str | None = Field(default=None, description="The unit of measure (e.g. pcs, kg, l).")
    minStock: int | None = Field(default=None, description="The minimum stock level.")
    targetStock: int | None = Field(default=None, description="The target stock level.")
    listPrice: Decimal | None = Field(default=None, max_digits=10, decimal_places=2, description="The standard sales price in euro.")
    laborRate: Decimal | None = Field(default=None, max_digits=10, decimal_places=2, description="Service price in euro — a flat amount without time tracking, addable to a document as its own line.")
    costPrice: Decimal | None = Field(default=None, max_digits=10, decimal_places=2, description="The target purchase price in euro, maintained by purchasing.")
    serviceIntervalMonths: int | None = Field(default=None, description="The service interval in months (only relevant for wear parts).")
    isWearPart: bool | None = Field(default=None, description="Whether the product is a wear part.")
    stockEffect: StockEffect = Field(
        default="direct",
        description="What a document line with this product does to the warehouse: 'direct' books the product itself (the normal case), 'billOfMaterials' books the components of an asset built from it, 'none' books nothing (flat fees such as installation or commissioning). Products without a maintained value count as 'direct'.",
    )
    active: bool = Field(
        default=True,
        description="Whether the product is actively carried. Products without a maintained status count as active.",
    )
    description: str | None = Field(default=None, description="The full product description.")
    grossWeightKg: float | None = Field(default=None, ge=0, description="The gross weight in kilograms.")
    netWeightKg: float | None = Field(default=None, ge=0, description="The net weight in kilograms.")
    taxPercent: float | None = Field(default=None, ge=0, le=100, description="The VAT rate in percent.")
    gtin: str | None = Field(default=None, description="The GTIN/EAN of the product.")
    manufacturerNumber: str | None = Field(default=None, description="The product number at the manufacturer.")
    commodityCode: str | None = Field(default=None, description="The commodity code, needed for invoicing and export.")
    countryOfOrigin: str | None = Field(default=None, description="The country of origin, needed for export.")
    discountable: bool | None = Field(default=None, description="Whether a discount may be granted on this product at all.")

    # --- Assembly behaviour, read-only ---------------------------------------
    assemblyDeductsComponents: bool | None = Field(default=None, description="Whether the components are deducted instead of the assembly itself. Read-only.")
    assemblyPrintsComponents: bool | None = Field(default=None, description="Whether the components are printed on the document. Read-only.")
    assemblyPriceFromComponents: bool | None = Field(default=None, description="Whether the assembly's price is computed from its components. Read-only.")
    usesSerialNumbers: bool | None = Field(default=None, description="Whether serial numbers are tracked for this product. Read-only.")


class ProductSystemFields(BaseModel):
    """Mix-in schema for server-managed timestamps.

    Holds administrative metadata (creation and update time) generated and maintained
    exclusively server-side in UTC. Not meant as input fields for the client.
    """
    createdAt: Neo4jDatetime | None = Field(
        default=None,
        description="The creation timestamp in UTC. Set server-side in the repository; may be missing on nodes that came from an import."
    )
    updatedAt: Neo4jDatetime | None = Field(default=None, description="The timestamp of the last update.")


class ProductCreate(InputModel):
    """Schema for creating (POST) a new product.

    Tightens the field set at the system boundary: what a client creates has to be
    complete and plausible — even though the graph holds historically incomplete nodes the
    read model has to tolerate.

    Declares its fields independently rather than inheriting from `ProductBase`. The
    tightening `str | None` -> `str` would break the substitution principle as
    inheritance; the repeated lines are the price for keeping the read and write contracts
    independently changeable.

    Careful when extending: a new field in `ProductBase` does NOT land here
    automatically. Whether it is needed on creation is a separate decision every time —
    the same goes for `ProductUpdate`.
    """
    number: str = Field(description="The unique product number.")
    label: str = Field(description="The name or main description of the product.")
    shortText: str | None = Field(default=None, description="An optional shorter description.")
    shortTextEn: str | None = Field(default=None, description="The English short text, for documents with language='EN'.")
    unit: str = Field(description="The unit of measure (e.g. pcs, kg, l).")
    minStock: int = Field(gt=0, description="The minimum stock level (must be > 0).")
    targetStock: int = Field(gt=0, description="The target stock level (must be > 0).")
    listPrice: Decimal = Field(gt=Decimal("0.01"), max_digits=10, decimal_places=2, description="The standard sales price in euro (must be > 0).")
    laborRate: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2, description="Service price in euro — a flat amount without time tracking. Optional.")
    costPrice: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2, description="The target purchase price in euro. Optional — unlike the sales price it is not mandatory, because it is only settled after the first supplier negotiation.")
    serviceIntervalMonths: int | None = Field(gt=0, default=None, description="The service interval in months (only relevant for wear parts).")
    isWearPart: bool | None = Field(default=None, description="Whether the product is a wear part.")
    stockEffect: StockEffect | None = Field(default=None, description="What a document line with this product does to the warehouse. Without a value, 'direct'.")
    description: str | None = Field(default=None, description="The full product description.")
    grossWeightKg: float | None = Field(ge=0, default=None, description="The gross weight in kilograms.")
    netWeightKg: float | None = Field(ge=0, default=None, description="The net weight in kilograms.")
    taxPercent: float | None = Field(ge=0, le=100, default=None, description="The VAT rate in percent.")
    gtin: str | None = Field(default=None, description="The GTIN/EAN of the product.")
    manufacturerNumber: str | None = Field(default=None, description="The product number at the manufacturer.")
    commodityCode: str | None = Field(default=None, description="The commodity code.")
    countryOfOrigin: str | None = Field(default=None, description="The country of origin.")
    discountable: bool | None = Field(default=None, description="Whether a discount may be granted on the product.")
    subcategoryId: int | None = Field(default=None, description="Id of the subcategory the product is assigned to. Product group and category follow from it and are not sent along.")


class ProductUpdate(InputModel):
    """Schema for updating (PATCH) a product.

    Every field is optional, since a PATCH request only has to carry the fields that
    actually change.
    """
    number: str | None = Field(default=None, description="The new product number.")
    label: str | None = Field(default=None, description="The new label.")
    shortText: str | None = Field(default=None, description="The new short text.")
    shortTextEn: str | None = Field(default=None, description="The new English short text.")
    unit: str | None = Field(default=None, description="The new unit of measure.")
    minStock: int | None = Field(gt=0, default=None, description="The new minimum stock level.")
    targetStock: int | None = Field(gt=0, default=None, description="The new target stock level.")
    listPrice: Decimal | None = Field(gt=Decimal("0.01"), max_digits=10, decimal_places=2, default=None, description="The new standard sales price.")
    laborRate: Decimal | None = Field(ge=0, max_digits=10, decimal_places=2, default=None, description="The new service price. Omitted leaves an existing value untouched; an explicit null removes it.")
    costPrice: Decimal | None = Field(ge=0, max_digits=10, decimal_places=2, default=None, description="The new target purchase price. Omitted leaves an existing value untouched; an explicit null removes it.")
    serviceIntervalMonths: int | None = Field(gt=0, default=None, description="The new service interval in months.")
    isWearPart: bool | None = Field(default=None, description="Whether it counts as a wear part from now on.")
    stockEffect: StockEffect | None = Field(default=None, description="What a document line with this product does to the warehouse from now on. Omitted means unchanged.")
    active: bool | None = Field(
        default=None,
        description="Sets the product active or inactive. Omitted means unchanged.",
    )
    description: str | None = Field(default=None, description="The new product description.")
    grossWeightKg: float | None = Field(ge=0, default=None, description="The new gross weight in kilograms.")
    netWeightKg: float | None = Field(ge=0, default=None, description="The new net weight in kilograms.")
    taxPercent: float | None = Field(ge=0, le=100, default=None, description="The new VAT rate in percent.")
    gtin: str | None = Field(default=None, description="The new GTIN/EAN.")
    manufacturerNumber: str | None = Field(default=None, description="The new manufacturer number.")
    commodityCode: str | None = Field(default=None, description="The new commodity code.")
    countryOfOrigin: str | None = Field(default=None, description="The new country of origin.")
    discountable: bool | None = Field(default=None, description="Whether a discount may be granted from now on.")
    subcategoryId: int | None = Field(default=None, description="The new subcategory. Omitted means unchanged; a value REPLACES the previous assignment, since a product belongs to exactly one subcategory.")


class Product(ProductBase, ProductSystemFields):
    """Schema for returning (GET) product data to the frontend.

    Inherits from ProductBase and ProductSystemFields and adds the two fields **computed**
    from the graph, `type` and `hasBom`. Both are read-only: they appear in neither
    `ProductCreate` nor `ProductUpdate`, because they have no state of their own a client
    could set — they follow from the `CONTAINS` edges.

    Both carry a default so the read model stays constructible from a bare property
    dictionary (tests, future callers). In production the map projection in the repository
    supplies them on every query.
    """
    type: ProductType = Field(
        default="Part",
        description="Computed: 'Assembly' with at least one outgoing CONTAINS edge, otherwise 'Part'.",
    )
    hasBom: bool = Field(
        default=False,
        description="Computed: True when the product has a bill of materials. Derived from the graph structure, not from a stored property.",
    )
    category: GroupRef | None = Field(default=None, description="Category of the product, if assigned through the subcategory.")
    productGroup: GroupRef | None = Field(default=None, description="Product group of the product, if assigned through the subcategory.")
    subcategory: GroupRef | None = Field(default=None, description="Subcategory of the product, if assigned.")
    suppliers: list[ProductSupplier] = Field(default_factory=list, description="Suppliers offering this product (edge SUPPLIES_PRODUCT) with their conditions. Empty when none is on file.")

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "number": "ACME-2003",
                "label": "Filter Cartridge",
                "shortText": "Filter",
                "shortTextEn": "Filter",
                "unit": "pcs",
                "minStock": 20,
                "targetStock": 80,
                "listPrice": 24.50,
                "serviceIntervalMonths": 12,
                "isWearPart": True,
                "stockEffect": "direct",
                "active": True,
                "type": "Part",
                "hasBom": False,
                "category": None,
                "productGroup": None,
                "subcategory": None,
                "suppliers": [],
                "createdAt": "2026-01-15T10:15:30Z",
                "updatedAt": None
            }
        }
    )


class BomLineCreate(InputModel):
    """Schema for adding a component to a bill of materials (POST).

    Sent by the frontend to create a new `CONTAINS` edge between an assembly (parent) and
    a product (child).
    """
    componentNumber: str = Field(description="The product number of the component to add.")
    quantity: int = Field(gt=0, description="The quantity of the component needed in this assembly (must be > 0).")
    unit: str | None = Field(
        default=None,
        description="The unit of this line (e.g. pcs, m). Without a value, a unit already on the edge is kept.",
    )


class BomLine(BaseModel):
    """Schema for a resolved bill-of-materials line (GET).

    Represents an edge in the graph including its property (quantity) and the full data of
    the referenced target node.

    `quantity` is deliberately `Decimal` and not `int`: bills of materials contain
    fractional quantities for goods sold by length or weight (1.4 m of hose, say). Lines
    created through the API are whole numbers, imported ones are not — the read model has
    to represent both.
    """
    quantity: Decimal = Field(gt=0, description="The quantity of the component needed.")
    component: Product = Field(description="The full product data of the component.")
    subComponents: list[BomLine] | None = Field(
        default_factory=list,
        description="A recursive list of further sub-components, when this component is an assembly itself."
    )


BomLine.model_rebuild()


class BomLineDeleted(BaseModel):
    """Schema for the successful deletion of a bill-of-materials line.

    Returned after a `CONTAINS` edge between an assembly and a component was removed. It
    reports the current state of the assembly, in particular whether the assembly has
    become a plain part again.
    """
    number: str = Field(description="The product number of the assembly.")
    componentNumber: str = Field(description="The product number of the removed component.")
    remainingLines: int = Field(ge=0, description="The number of remaining components; 0 means the assembly is a part again.")


# --- Category hierarchy (GET /api/product-groups) -----------------------------

class SubcategoryOption(BaseModel):
    """A subcategory inside a product group, as the selection list returns it."""
    id: int = Field(description="Id of the subcategory.")
    name: str = Field(description="Name of the subcategory, a placeholder built from the id when none is maintained.")


class ProductGroupOption(BaseModel):
    """A product group inside a category, as the selection list returns it."""
    id: int = Field(description="Id of the product group.")
    name: str = Field(description="Name of the product group, a placeholder built from the id when none is maintained.")
    subcategories: list[SubcategoryOption] = Field(default_factory=list, description="The subcategories assigned to this product group.")


class CategoryOption(BaseModel):
    """A category with its product groups, as the selection list returns it."""
    id: int = Field(description="Id of the category.")
    name: str = Field(description="Name of the category.")
    productGroups: list[ProductGroupOption] = Field(default_factory=list, description="The product groups assigned to this category.")
