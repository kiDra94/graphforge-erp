"""Cent/euro conversion, shared by the sales repositories."""

from decimal import Decimal


def _cent(amount: Decimal | None) -> int | None:
    """Converts a euro Decimal into integer cents.

    The driver rejects `Decimal` as a query parameter outright; the conversion is
    therefore mandatory and happens exclusively here in the repository.
    """
    return None if amount is None else int((amount * 100).to_integral_value())


def _euro(cent: int | None) -> Decimal | None:
    """Converts integer cents from the graph into a euro Decimal.

    A value of 0 stays a recorded 0.00 EUR and is not swallowed into None.

    The result is fixed to two decimal places: `Decimal(1274300) / 100` would otherwise
    give `12743` and the response would carry "12743" instead of "12743.00". The totals
    calculated in the service are rounded to cents — without this fixing, the list would
    report a different amount than the detail view of the same document.
    """
    return None if cent is None else (Decimal(cent) / 100).quantize(Decimal("0.01"))
