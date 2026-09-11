"""Unit tests of the role-based field visibility.

Deliberately covered: a mistake here does not crash anything, it silently shows a cost
price to someone who must not see it. That failure mode is invisible without a test.
"""

from core.visibility import sees_cost_prices, sees_fixed_prices


def test_purchasing_sees_cost_prices():
    assert sees_cost_prices({"roles": ["Purchasing"]}) is True


def test_sales_does_not_see_cost_prices():
    assert sees_cost_prices({"roles": ["Sales"]}) is False


def test_backoffice_sees_fixed_prices():
    assert sees_fixed_prices({"roles": ["BackOffice"]}) is True


def test_sales_does_not_see_fixed_prices():
    """Sales negotiates without contract conditions — that is the point of the split."""
    assert sees_fixed_prices({"roles": ["Sales"]}) is False


def test_admin_sees_both():
    assert sees_cost_prices({"roles": ["Admin"]}) is True
    assert sees_fixed_prices({"roles": ["Admin"]}) is True


def test_a_user_without_roles_sees_neither():
    assert sees_cost_prices({}) is False
    assert sees_fixed_prices({}) is False
