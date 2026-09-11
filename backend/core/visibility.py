"""Role-based visibility of individual response fields.

Omitting a field is a different thing from blocking an endpoint: `core/security.py`
decides who may call an endpoint at all; the functions here decide which fields of an
otherwise permitted response stay visible. They are called in the router, after the
service call and before returning.
"""

from core.security import has_role

# Purchasing sees cost prices and contribution margins; everyone else, sales included,
# does not — a sales representative negotiating with a customer has no business seeing
# what the goods cost us.
_SEES_COST_PRICES = ("Purchasing",)

# BackOffice maintains fixed prices and contract discounts. Sales negotiates without
# them, so the values stay hidden there.
_SEES_FIXED_PRICES = ("BackOffice",)


def sees_cost_prices(user: dict) -> bool:
    """Whether the user may see cost prices and contribution margins."""
    return has_role(user, *_SEES_COST_PRICES)


def sees_fixed_prices(user: dict) -> bool:
    """Whether the user may see fixed prices and contract discounts."""
    return has_role(user, *_SEES_FIXED_PRICES)
