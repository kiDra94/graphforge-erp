"""Unit tests for JWT creation and validation, password hashing and role checks."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException

from core.config import get_settings
from core.security import (
    create_access_token,
    decode_token,
    get_current_user,
    hash_password,
    require_any_role,
    verify_password,
)


# --- Password hashing --------------------------------------------------------------
def test_hash_password_does_not_return_plaintext():
    hashed = hash_password("demo1234")
    assert hashed != "demo1234"


def test_verify_password_accepts_the_correct_password():
    hashed = hash_password("demo1234")
    assert verify_password("demo1234", hashed) is True


def test_verify_password_rejects_a_wrong_password():
    hashed = hash_password("demo1234")
    assert verify_password("wrong", hashed) is False


# --- Token creation and decoding ---------------------------------------------------
def test_create_access_token_returns_the_original_claims():
    token = create_access_token({"sub": "1", "email": "a@b.com", "roles": ["Admin"]})
    payload = decode_token(token)

    assert payload["sub"] == "1"
    assert payload["email"] == "a@b.com"
    assert payload["roles"] == ["Admin"]


def test_create_access_token_sets_an_expiry_in_the_future():
    token = create_access_token({"sub": "1"})
    payload = decode_token(token)

    assert payload["exp"] > datetime.now(UTC).timestamp()


def test_decode_token_returns_401_for_an_expired_token():
    settings = get_settings()
    expired_token = jwt.encode(
        {"sub": "1", "exp": datetime.now(UTC) - timedelta(minutes=1)},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    with pytest.raises(HTTPException) as excinfo:
        decode_token(expired_token)

    assert excinfo.value.status_code == 401
    assert "expired" in excinfo.value.detail.lower()


def test_decode_token_returns_401_for_a_malformed_token():
    with pytest.raises(HTTPException) as excinfo:
        decode_token("this-is-not-a-jwt")

    assert excinfo.value.status_code == 401
    assert "invalid" in excinfo.value.detail.lower()


def test_decode_token_returns_401_for_a_wrong_signature():
    foreign_token = jwt.encode(
        {"sub": "1", "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "a-completely-different-secret-than-the-one-in-the-settings",
        algorithm="HS256",
    )

    with pytest.raises(HTTPException) as excinfo:
        decode_token(foreign_token)

    assert excinfo.value.status_code == 401


# --- get_current_user --------------------------------------------------------------
def test_get_current_user_reads_the_claims_from_the_token():
    token = create_access_token({"sub": "1", "roles": ["Sales"]})

    user = get_current_user(token)

    assert user["sub"] == "1"
    assert user["roles"] == ["Sales"]


def test_get_current_user_returns_401_for_a_malformed_token():
    with pytest.raises(HTTPException) as excinfo:
        get_current_user("broken")

    assert excinfo.value.status_code == 401


# --- require_any_role --------------------------------------------------------------
def test_require_any_role_lets_a_matching_role_through():
    check = require_any_role("Sales", "Admin")

    user = check(user={"sub": "1", "roles": ["Admin"]})

    assert user["roles"] == ["Admin"]


def test_require_any_role_blocks_a_wrong_role():
    check = require_any_role("Sales", "Admin")

    with pytest.raises(HTTPException) as excinfo:
        check(user={"sub": "1", "roles": ["Warehouse"]})

    assert excinfo.value.status_code == 403


def test_require_any_role_blocks_a_user_without_a_roles_field():
    check = require_any_role("Admin")

    with pytest.raises(HTTPException) as excinfo:
        check(user={"sub": "1"})

    assert excinfo.value.status_code == 403


def test_require_any_role_checks_with_or_across_the_users_own_roles():
    check = require_any_role("Admin")

    user = check(user={"sub": "1", "roles": ["Warehouse", "Admin"]})

    assert user["roles"] == ["Warehouse", "Admin"]


def test_admin_passes_a_check_that_does_not_list_it():
    """`Admin` is the blanket role — it must not have to appear in every call."""
    check = require_any_role("Purchasing")

    user = check(user={"sub": "1", "roles": ["Admin"]})

    assert user["roles"] == ["Admin"]
