"""Pydantic schemas of the inventory domain: read, write and response models."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.neo4j_types import Neo4jDatetime
from core.schemas import InputModel

# The five movement types. Typed as a Literal, FastAPI rejects an unknown value in the
# request body or query parameter with a 422 instead of accepting it silently or answering
# it with an empty result list.
MovementType = Literal["Receipt", "Reservation", "Issue", "Correction", "Transfer"]


class Location(BaseModel):
    """Schema for returning (GET /api/locations) a warehouse location.

    A pure read model — there is deliberately no `LocationCreate`. Locations are
    organisational master data that only change when a site is set up or closed; a write
    model is only needed once that maintenance is supposed to run through the API.
    """
    id: str = Field(description="Business key of the location. Held as a string in the graph, so it behaves like every other key.")
    name: str = Field(description="Display name, e.g. 'Central Warehouse'.")
    type: str = Field(description="Kind of location, e.g. 'Warehouse' or 'Vehicle'.")


class LocationStock(BaseModel):
    """One location inside the stock breakdown (`byLocation` in `Stock`).

    Deliberately a class of its own rather than a subclass of `Location`: the fields are
    named `locationId`/`locationName` instead of `id`/`name`, because the location is
    embedded flat into a larger response object — an unprefixed `id` would be ambiguous
    there (product? location? movement?). A subclass replacing inherited field names would
    not be a subtype anyway.
    """
    locationId: str = Field(description="Business key of the location.")
    locationName: str = Field(description="Display name of the location.")
    quantity: float = Field(description="Real stock at this location. Float rather than int, because stock is also held in metres and kilograms.")
    reserved: float = Field(description="Reserved quantity at this location (through open order confirmations).")
    available: float = Field(description="Computed as quantity - reserved, not stored. Kept per location, because stock at one location is not available for another.")


class Stock(BaseModel):
    """Schema for returning (GET /api/stock/{productNumber}) the total stock.

    There is no `StockLevel` node in exactly this shape — this is an aggregation over all
    `StockLevel` nodes of a product (one per location). Which is why the schema carries no
    timestamps: a `createdAt` would suggest this response is itself a persisted record,
    and it is not.

    `byLocation` only holds locations where the product ever had a stock record, not a
    zero entry for every location that exists. A product that was never stored at a
    location does not appear there — that is not a stock of 0, it is no record.
    """
    productNumber: str = Field(description="The product number the stock was computed for.")
    label: str | None = Field(default=None, description="Label of the product, for display without a second call against /api/products.")
    totalStock: float = Field(description="Sum of quantity across all locations.")
    reserved: float = Field(description="Sum of reserved across all locations.")
    available: float = Field(description="Computed as totalStock - reserved, not stored.")
    byLocation: list[LocationStock] = Field(default_factory=list, description="Breakdown per location that holds a stock record.")

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "productNumber": "ACME-2003",
                "label": "Filter Cartridge",
                "totalStock": 68,
                "reserved": 0,
                "available": 68,
                "byLocation": [
                    {"locationId": "1", "locationName": "Central Warehouse", "quantity": 62, "reserved": 0, "available": 62},
                    {"locationId": "2", "locationName": "Service Van 1", "quantity": 6, "reserved": 0, "available": 6},
                ],
            }
        },
    )


class StockMovement(BaseModel):
    """Schema for returning (GET /api/stock-movements) an entry of the audit log.

    Apart from `id`, every field is optional and carries NO value constraints, even though
    `quantity` and `type` should always be set. That is intentional: stock is computed
    entirely from the movement history, so imported movements exist and may be incomplete.
    A constraint would protect nothing here; it would merely turn an already stored
    database state into an HTTP 500.

    `createdAt` sits directly on this schema instead of in a mix-in, because it is the
    node's only time field — splitting it out pays off from a second one onwards. There is
    deliberately no `updatedAt`: a stock movement is immutable. There is no PATCH endpoint
    and no business reason to change a booking after the fact instead of writing a
    correcting one.
    """
    id: str = Field(description="Business key of the movement. Assigned by the server on creation.")
    productNumber: str | None = Field(default=None, description="The product affected.")
    quantity: float | None = Field(default=None, description="Quantity booked. Positive except for Correction and the two lines of a Transfer, where the sign carries the direction.")
    type: MovementType | None = Field(default=None, description="One of the five movement types.")
    locationName: str | None = Field(default=None, description="Display name of the location affected.")
    documentNumber: str | None = Field(default=None, description="The related document, if any. null for internal operations such as a stocktaking correction.")
    customerName: str | None = Field(default=None, description="Customer reference through the CONCERNS_CUSTOMER edge. null for internal bookings.")
    purchasePrice: Decimal | None = Field(default=None, description="Only set on a receipt — the basis of the moving average price. Empty for other movement types.")
    createdAt: Neo4jDatetime | None = Field(
        default=None,
        description="Time of the booking, set server-side in UTC. May be missing on imported movements.",
    )

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "mov-3f1c8a2e",
                "productNumber": "ACME-2003",
                "quantity": 50,
                "type": "Receipt",
                "locationName": "Central Warehouse",
                "documentNumber": "GR-2026-0001",
                "customerName": None,
                "purchasePrice": 11.00,
                "createdAt": "2026-02-12T08:00:00Z",
            }
        },
    )


class StockMovementCreate(InputModel):
    """Schema for creating (POST /api/stock-movements) a new stock movement.

    Tightens the field set at the system boundary and therefore declares its fields
    independently instead of inheriting from `StockMovement`: the tightening
    `str | None` -> `str` would break the substitution principle as inheritance.

    `quantity` deliberately carries NO `gt=0` constraint. The rule depends on the type —
    for `Correction` the sign carries the information (an increase or decrease found during
    stocktaking), for the other four the value has to be positive. A blanket `gt=0` would
    reject every downward correction. The check therefore belongs in the service layer,
    where `type` and `quantity` are known together.
    """
    productNumber: str = Field(description="The product to book.")
    quantity: float = Field(description="The quantity to book. Positive except for Correction, where the sign carries the direction. Always positive for a Transfer — locationId is the source, targetLocationId the destination.")
    type: MovementType = Field(description="One of the five movement types. Determines how quantity and reserved change on the stock level.")
    locationId: str = Field(description="The location affected. For a Transfer, the source location.")
    targetLocationId: str | None = Field(default=None, description="Only set for type='Transfer', and mandatory there — the destination location. locationId stays the source in that case.")
    documentNumber: str | None = Field(default=None, description="The related document, if any. Left empty for stocktaking corrections and transfers without a document.")
    customerId: str | None = Field(default=None, description="Sets the CONCERNS_CUSTOMER edge. null for internal operations such as a stocktaking correction or a transfer.")
    purchasePrice: Decimal | None = Field(default=None, description="Only relevant on a receipt — the basis of the moving average price. To be omitted for other movement types.")
    note: str | None = Field(default=None, description="Free text, e.g. a reference to the delivery note.")

    @model_validator(mode="after")
    def target_location_only_on_a_transfer(self) -> StockMovementCreate:
        """Requires targetLocationId exactly when it is needed.

        A transfer without a destination would leave the value of where the goods go
        unsaid — that would not be a transfer any more, but an incomplete issue. On any
        other movement type a set targetLocationId would have no effect at all and would
        only pretend that it had one.
        """
        if self.type == "Transfer":
            if self.targetLocationId is None:
                raise ValueError("targetLocationId is mandatory for a transfer.")
            if self.targetLocationId == self.locationId:
                raise ValueError(
                    "targetLocationId has to differ from locationId: a transfer to the "
                    "same location would not be a movement."
                )
        elif self.targetLocationId is not None:
            raise ValueError(
                f"targetLocationId is only allowed on a transfer, not on '{self.type}'."
            )
        return self

    @model_validator(mode="after")
    def purchase_price_only_on_a_receipt(self) -> StockMovementCreate:
        """Rejects a purchase price that does not belong to a receipt.

        A price arises when goods are procured, not when they are issued or moved. Without
        this check the value would have two possible fates, both bad: it would be silently
        discarded while the client believes it was stored — or it would be written along
        and distort the moving average price, which is computed from quantity and purchase
        price of the receipts.
        """
        if self.type != "Receipt" and self.purchasePrice is not None:
            raise ValueError(
                f"purchasePrice is only allowed on a receipt, not on '{self.type}'."
            )
        return self

    @model_validator(mode="after")
    def no_customer_on_a_correction_or_transfer(self) -> StockMovementCreate:
        """Rejects a customer reference on a stocktaking correction or a transfer.

        Both are internal operations without a business partner — a correction settles a
        counting difference, a transfer moves goods between our own locations. A customer
        reference would create the CONCERNS_CUSTOMER edge and thereby show a movement in
        the per-customer report that never happened with that customer.
        """
        if self.type in ("Correction", "Transfer") and self.customerId is not None:
            raise ValueError(
                f"customerId is not allowed on '{self.type}': that is an internal "
                "operation without a customer reference."
            )
        return self


class StockMovementResponse(BaseModel):
    """Response to POST /api/stock-movements (201).

    Deliberately carries no `success` field: on a `201` it would be `true` by definition.
    A failure never comes back as `success: false`, but as a `400`, `404` or `500` through
    the global exception handlers.

    What is returned instead is the state after the booking. Without it the frontend would
    have to fire an extra GET against the stock after every booking.
    """
    newQuantity: float = Field(description="The product's new stock at this location after the booking. For a transfer, the stock at the source location.")
    reserved: float = Field(description="The new reserved quantity at this location after the booking.")
    movementId: str = Field(description="The server-assigned key of the new movement. For a transfer, the id of the outgoing booking at the source.")
    targetNewQuantity: float | None = Field(default=None, description="Only set for type='Transfer' — the new stock at the destination location after the booking.")
    targetMovementId: str | None = Field(default=None, description="Only set for type='Transfer' — the id of the incoming booking at the destination.")
