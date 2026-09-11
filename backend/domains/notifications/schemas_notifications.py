"""Pydantic schemas of the notifications domain: read, input and response models.

Three cases that differ in business terms (the service list at the turn of the year, a
quantity deviation in a goods receipt, an unavailable quantity when shipping) share one
mechanism: a tickable list per target role. There is neither mail delivery nor a
scheduler.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from core.neo4j_types import Neo4jDatetime
from core.schemas import InputModel

# The three cases. A fourth kind without a creation rule would not exist — the Literal
# keeps repository and schema congruent, in the same way as `MovementType`.
#
# `shortage` and `unavailable` are deliberately two kinds and not one: a shortage is what
# a supplier delivered less of than the purchase order asked for, an unavailable quantity
# is what was not on the shelf when a delivery note was picked. They reach the same role
# and read alike, but they are raised in different places and are settled differently.
NotificationType = Literal["service", "shortage", "unavailable"]


class NotificationConcern(BaseModel):
    """The target of the `CONCERNS` edge, small and generic instead of a union per type.

    `type`/`id` address the node in business terms (`ComponentInstance` on `service`,
    `DocumentLine` on `shortage` and `unavailable`), `description` is a pre-computed
    display text — without it the frontend would have to call a second endpoint for every
    row just to learn what it is about.

    `productNumber` and `quantity` are the two fields the recipient can work on rather than
    having to take the display text apart: purchasing reorders straight out of an
    unavailable-quantity notification.
    """
    type: Literal["ComponentInstance", "DocumentLine"] = Field(
        description="The node type at the end of the CONCERNS edge."
    )
    id: str = Field(description="Business key of the node concerned.")
    description: str = Field(description="Pre-computed display text for the list.")
    productNumber: str | None = Field(
        default=None,
        description="Product behind the node concerned. Null when the node carries none.",
    )
    quantity: Decimal | None = Field(
        default=None,
        description="The quantity reported as unavailable. Only set on type='unavailable'.",
    )


class UnavailableLine(InputModel):
    """One document line that could not be shipped in full."""

    lineNumber: int = Field(gt=0, description="Line number of the line concerned.")
    quantity: Decimal = Field(gt=0, description="The quantity missing at the location.")


class UnavailableReport(InputModel):
    """Schema for `POST /api/notifications/unavailable`.

    The quantity is **sent along and stored**, unlike the actual quantity of a shortage,
    which the repository computes from the graph at read time. The reason: it is not a
    derivable figure but a person's statement at a point in time — what was within reach at
    their own location when the shipment was due.
    """

    documentNumber: str = Field(
        description="Number of the delivery note whose lines are missing."
    )
    lines: list[UnavailableLine] = Field(
        min_length=1,
        description="The lines concerned, each with its line number and missing quantity.",
    )

    @model_validator(mode="after")
    def no_line_twice(self) -> UnavailableReport:
        """Rejects the same line number twice.

        Two entries for the same line would yield the same notification through the
        business key; which of the two quantities then applies would be decided by the
        order inside the UNWIND.
        """
        numbers = [line.lineNumber for line in self.lines]
        duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
        if duplicates:
            raise ValueError(
                "lines names line numbers more than once: "
                + ", ".join(str(n) for n in duplicates) + "."
            )
        return self


class Notification(BaseModel):
    """Schema for one row of the notification list (GET) and the tick-off answer (PUT).

    `id` is a business key (`service_{componentInstanceId}_{year}`,
    `shortage_{goodsReceiptNumber}_{lineNumber}`, respectively
    `unavailable_{documentNumber}_{lineNumber}`), not a technical one — that makes every
    creation idempotent by itself, see `NotificationRepository`.
    """
    id: str = Field(description="Business key of the notification.")
    type: NotificationType = Field(description="Which of the three cases.")
    done: bool = Field(description="Whether the notification has been ticked off.")
    forRole: str = Field(description="The role this notification is delivered to.")
    concerns: NotificationConcern = Field(description="What it is about.")
    createdAt: Neo4jDatetime | None = Field(
        default=None, description="Time of creation, set server-side in UTC."
    )
    doneAt: Neo4jDatetime | None = Field(
        default=None,
        description="Time of the tick-off, set server-side in UTC. Null while not done.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "shortage_GR-2026-0001_2",
                "type": "shortage",
                "done": False,
                "forRole": "Purchasing",
                "concerns": {
                    "type": "DocumentLine",
                    "id": "PO-2026-0001_2",
                    "description": "Purchase order PO-2026-0001, line 2: Sealing Ring 40mm — ordered 100, received 80",
                    "productNumber": "ACME-2001",
                    "quantity": None,
                },
                "createdAt": "2026-08-28T09:15:00Z",
                "doneAt": None,
            }
        }
    }
