"""Pydantic schemas of the procurement domain: read, write and response models."""

from decimal import Decimal

from pydantic import BaseModel, Field

from core.neo4j_types import Neo4jDatetime
from core.schemas import InputModel


class Supplier(BaseModel):
    """Schema for reading (GET) a supplier.

    Deliberately tolerant: apart from `id` every field is optional. Suppliers taken over
    from a predecessor system carry little more than a name and a city; the remaining
    master data fields are maintained inside the system and may simply be missing. A
    constraint here would protect nothing — it would merely turn an already stored
    database state into an HTTP 500.

    The `id` is the business key and therefore the only mandatory field: without it the
    node does not exist in the graph. There is no second number beside it — a display number
    that is a uuid like the id says nothing the id does not already say, and two keys for the
    same node mean somebody has to know which of them is meant.
    """
    id: str = Field(description="Id of the supplier, assigned by the server as 'S-' plus a uuid4.")
    name: str | None = Field(default=None, description="Name of the supplier.")
    email: str | None = Field(default=None, description="Email address of the supplier.")
    phone: str | None = Field(default=None, description="Phone number of the supplier.")
    street: str | None = Field(default=None, description="Street of the supplier's address.")
    city: str | None = Field(default=None, description="City of the supplier's address.")
    country: str | None = Field(default=None, description="Country of the supplier's address.")
    vatId: str | None = Field(default=None, description="VAT identification number of the supplier.")
    createdAt: Neo4jDatetime | None = Field(default=None, description="Time of creation, server-side in UTC. Empty on imported suppliers.")
    updatedAt: Neo4jDatetime | None = Field(default=None, description="Time of the last change, server-side in UTC.")


class SupplierCreate(InputModel):
    """Schema for creating (POST) a new supplier.

    Declares its fields independently instead of inheriting from `Supplier`: tightening
    `str | None` to `str` on the name would be a violation of the substitution principle
    if it came through inheritance.

    Only the name is mandatory. A supplier often comes into existence from a first quote,
    at a point where the VAT id and the address are not known yet — were they mandatory,
    purchasing would have to invent placeholders, and those would then sit in the master
    record permanently.

    `id`, `createdAt` and `updatedAt` are deliberately absent: the server assigns the id
    and sets the timestamps on write. A client allowed to send them could forge the
    creation history.
    """
    name: str = Field(description="Name of the supplier.")
    email: str | None = Field(default=None, description="Email address of the supplier.")
    phone: str | None = Field(default=None, description="Phone number of the supplier.")
    street: str | None = Field(default=None, description="Street of the supplier's address.")
    city: str | None = Field(default=None, description="City of the supplier's address.")
    country: str | None = Field(default=None, description="Country of the supplier's address.")
    vatId: str | None = Field(default=None, description="VAT identification number of the supplier.")


class SupplierUpdate(InputModel):
    """Schema for updating (PATCH) a supplier.

    Every field is optional, because a PATCH request only has to carry the fields that
    actually change (partial update).

    The `id` cannot be changed: the server assigns it, and it is the key existing edges
    point at.
    """
    name: str | None = Field(default=None, description="Name of the supplier.")
    email: str | None = Field(default=None, description="Email address of the supplier.")
    phone: str | None = Field(default=None, description="Phone number of the supplier.")
    street: str | None = Field(default=None, description="Street of the supplier's address.")
    city: str | None = Field(default=None, description="City of the supplier's address.")
    country: str | None = Field(default=None, description="Country of the supplier's address.")
    vatId: str | None = Field(default=None, description="VAT identification number of the supplier.")


class SupplierProductCreate(InputModel):
    """Schema for taking a product into a supplier's supply range (POST).

    Describes the conditions that hang off the `SUPPLIES_PRODUCT` edge. The supplier
    itself is part of the path and therefore not a field of this model.

    `isPreferredSupplier` is the only optional field and defaults to `false`: the normal
    case is a supplier that simply offers the product, while being preferred is the
    exception and gets set explicitly. The flag is not an exclusive right — several
    suppliers of the same product may carry it, and the reorder analysis then decides on
    the purchase price.

    `purchasePrice` arrives in euro and is converted into the internally stored integer
    cents in the repository layer.
    """
    productNumber: str = Field(description="Number of the product to be taken into the supply range.")
    leadTimeDays: int = Field(gt=0, description="Lead time of the product in days.")
    purchasePrice: Decimal = Field(ge=0.00, description="Purchase price of the product in euro.")
    isPreferredSupplier: bool = Field(default=False, description="Marks this supplier as the preferred source for the product.")


class SupplierProductResponse(BaseModel):
    """Response to taking a product into a supply range (200/201).

    No field is optional: the response mirrors the edge that was just written, and every
    value comes either from the request or from the path.

    Deliberately carries no `success` field — on a 2xx it would be `true` by definition.
    A failure never comes back as `success: false` but as 400, 404, 422 or 500 through
    the global exception handlers. Whether the condition was created or overwritten is
    said by the status code: 201 against 200.
    """
    supplierId: str = Field(description="Id of the supplier.")
    productNumber: str = Field(description="Number of the product taken into the supply range.")
    leadTimeDays: int = Field(description="Lead time of the product in days.")
    purchasePrice: Decimal = Field(ge=0.00, description="Purchase price of the product in euro.")
    isPreferredSupplier: bool = Field(description="Whether this supplier is the preferred source for the product.")


class ReorderSuggestion(BaseModel):
    """Schema for one line of the reorder analysis (GET /api/reorder-suggestions).

    There is no node of this shape — the line only comes into existence in the query,
    out of the product master, the stock levels and the supply ranges.

    The numeric types are deliberately mixed: `currentStock` and `suggestedQuantity` are
    floats, because stock is also kept in metres and kilograms; `minStock` is an integer,
    because the stock bounds sit in the graph as whole numbers. The quantity inherits its
    type from the stock, being its difference.

    Only the three supplier fields are optional: a product without a stored source of
    supply still appears in the suggestion — it is below its minimum stock, and that is
    the more important information. The remaining fields are guaranteed to be set by the
    filter of the query.
    """
    productNumber: str = Field(description="Number of the product.")
    label: str | None = Field(default=None, description="Label of the product.")
    currentStock: float = Field(description="Current total stock across all locations.")
    minStock: int = Field(description="Minimum stock of the product.")
    suggestedQuantity: float = Field(ge=0, description="Suggested order quantity, calculated as targetStock - currentStock. Never negative.")
    supplier: str | None = Field(default=None, description="Name of the selected supplier. Empty when no supplier is stored.")
    unitPrice: Decimal | None = Field(default=None, description="Purchase price per unit in euro. Empty when no supplier is stored.")
    leadTimeDays: int | None = Field(default=None, description="Lead time in days. Empty when no supplier is stored.")
