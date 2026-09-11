"""Unit tests of the notification schemas — pure validation, without a database.

Two groups:

* **The read model.** `Notification` is assembled entirely in Cypher; what arrives here is
  a map, not a node. What is checked is that the closed set of types stays closed and that
  the nested `concerns` block is not optional — a row without it would be a list entry
  nobody can act on.
* **The report at the system boundary.** `UnavailableReport` is the only input model of
  this domain. Its duplicate check is worth a test of its own: two entries for the same
  line would collapse into one notification through the business key, and which quantity
  survived would be decided by the order inside the UNWIND.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from domains.notifications.schemas_notifications import (
    Notification,
    NotificationConcern,
    UnavailableLine,
    UnavailableReport,
)


def _notification(**overrides) -> dict:
    """Builds the field set of a complete notification as the projection delivers it."""
    data: dict = {
        "id": "shortage_GR-2026-0001_2",
        "type": "shortage",
        "done": False,
        "forRole": "Purchasing",
        "concerns": {
            "type": "DocumentLine",
            "id": "PO-2026-0001_2",
            "description": "Purchase order PO-2026-0001, line 2: Sealing Ring 40mm",
            "productNumber": "ACME-2001",
            "quantity": None,
        },
        "createdAt": datetime(2026, 8, 28, 9, 15, tzinfo=UTC),
        "doneAt": None,
    }
    data.update(overrides)
    return data


# ==========================================
# Read model
# ==========================================

def test_a_complete_notification_is_valid():
    notification = Notification.model_validate(_notification())

    assert notification.type == "shortage"
    assert notification.done is False
    assert notification.concerns.productNumber == "ACME-2001"
    assert notification.doneAt is None


def test_a_ticked_off_notification_carries_a_date():
    notification = Notification.model_validate(
        _notification(done=True, doneAt=datetime(2026, 8, 29, 7, 0, tzinfo=UTC))
    )

    assert notification.done is True
    assert notification.doneAt is not None
    assert notification.doneAt.day == 29


@pytest.mark.parametrize("invalid_type", ["maintenance", "SHORTAGE", "", "info"])
def test_an_unknown_type_is_invalid(invalid_type):
    """The Literal keeps repository and schema congruent: a fourth kind without a creation
    rule does not exist, and it must not reach the answer through the graph either."""
    with pytest.raises(ValidationError):
        Notification.model_validate(_notification(type=invalid_type))


def test_all_three_types_are_accepted():
    """`shortage` and `unavailable` are deliberately two kinds — they are raised in
    different places and settled differently."""
    for kind in ("service", "shortage", "unavailable"):
        assert Notification.model_validate(_notification(type=kind)).type == kind


def test_a_concern_without_a_description_is_invalid():
    """The display text is pre-computed in Cypher. Without it the frontend would have to
    call a second endpoint per row just to learn what the row is about."""
    with pytest.raises(ValidationError):
        NotificationConcern.model_validate(
            {"type": "DocumentLine", "id": "PO-2026-0001_2"}
        )


def test_a_concern_without_a_product_and_quantity_stays_valid():
    """A service notification hangs off a component instance and carries no quantity; a
    thinly maintained line can be without a product."""
    concern = NotificationConcern.model_validate(
        {"type": "ComponentInstance", "id": "SN-2026-0001_ACME-2003",
         "description": "Asset SN-2026-0001 - Filter Cartridge"}
    )

    assert concern.productNumber is None
    assert concern.quantity is None


def test_an_unknown_concern_type_is_invalid():
    """Only two node types ever sit at the end of the CONCERNS edge — a third one would be
    a bug in the projection."""
    with pytest.raises(ValidationError):
        NotificationConcern.model_validate(
            {"type": "Product", "id": "ACME-2001", "description": "..."}
        )


def test_a_notification_without_a_concern_is_invalid():
    """The CASE in the projection yields `null` when neither branch matches. That is a
    broken row, and it is meant to fail loudly rather than reach the list empty."""
    data = _notification()
    del data["concerns"]

    with pytest.raises(ValidationError):
        Notification.model_validate(data)


def test_missing_mandatory_fields_are_invalid():
    with pytest.raises(ValidationError):
        Notification.model_validate({"id": "shortage_GR-2026-0001_2"})


def test_a_notification_without_timestamps_stays_valid():
    """`createdAt` is set on creation, `doneAt` only on the tick-off — an open notification
    legitimately carries none."""
    data = _notification()
    del data["createdAt"]
    del data["doneAt"]

    notification = Notification.model_validate(data)

    assert notification.createdAt is None
    assert notification.doneAt is None


# ==========================================
# Report at the system boundary
# ==========================================

def test_a_complete_report_is_valid():
    report = UnavailableReport(
        documentNumber="DN-2026-0001",
        lines=[
            UnavailableLine(lineNumber=1, quantity=Decimal("2")),
            UnavailableLine(lineNumber=2, quantity=Decimal("0.5")),
        ],
    )

    assert report.documentNumber == "DN-2026-0001"
    assert len(report.lines) == 2


def test_a_report_without_lines_is_invalid():
    """An empty report would create nothing and report success all the same."""
    with pytest.raises(ValidationError):
        UnavailableReport(documentNumber="DN-2026-0001", lines=[])


@pytest.mark.parametrize("invalid_quantity", [Decimal("0"), Decimal("-1")])
def test_the_quantity_has_to_be_greater_than_zero(invalid_quantity):
    """Nothing missing is not a report."""
    with pytest.raises(ValidationError):
        UnavailableLine(lineNumber=1, quantity=invalid_quantity)


@pytest.mark.parametrize("invalid_line_number", [0, -1])
def test_the_line_number_has_to_be_greater_than_zero(invalid_line_number):
    with pytest.raises(ValidationError):
        UnavailableLine(lineNumber=invalid_line_number, quantity=Decimal("1"))


def test_the_same_line_twice_is_invalid():
    """Both entries would yield the same notification through the business key; which of
    the two quantities then applies would be decided by the order inside the UNWIND."""
    with pytest.raises(ValidationError, match="1"):
        UnavailableReport(
            documentNumber="DN-2026-0001",
            lines=[
                UnavailableLine(lineNumber=1, quantity=Decimal("2")),
                UnavailableLine(lineNumber=1, quantity=Decimal("3")),
            ],
        )


def test_different_lines_of_the_same_document_are_valid():
    report = UnavailableReport(
        documentNumber="DN-2026-0001",
        lines=[
            UnavailableLine(lineNumber=1, quantity=Decimal("2")),
            UnavailableLine(lineNumber=3, quantity=Decimal("2")),
        ],
    )

    assert [line.lineNumber for line in report.lines] == [1, 3]


def test_an_unknown_field_is_rejected():
    """`InputModel` forbids extras: a client sending `quantitiy` is meant to learn about
    its typo, not to have the value silently dropped."""
    with pytest.raises(ValidationError):
        UnavailableReport.model_validate({
            "documentNumber": "DN-2026-0001",
            "lines": [{"lineNumber": 1, "quantity": 2, "reason": "damaged"}],
        })
