"""JWT creation, token validation, password hashing and role checks."""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from core.config import get_settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(password: str) -> str:
    """Hashes a password with bcrypt and a freshly generated salt.

    The result already contains the salt — it is stored as a whole, there is no second
    column for it.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Checks a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(data: dict) -> str:
    """Creates a signed JWT from the given payload.

    Sets `exp` itself, to `JWT_EXPIRE_MINUTES` in the future. Everything else in `data`
    is copied into the token unchanged — it is signed but not encrypted, so it must
    never carry anything confidential.
    """
    settings = get_settings()
    payload = data.copy()
    payload["exp"] = datetime.now(UTC) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Validates signature and expiry of a JWT and returns its payload.

    Both failures end in a 401, with a different message: an expired token is something
    else than a tampered one for the user interface — the first only needs a fresh
    sign-in.
    """
    settings = get_settings()
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired."
        ) from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token."
        ) from e


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """FastAPI dependency — returns the signed-in user from the JWT."""
    return decode_token(token)


def has_role(user: dict, *roles: str) -> bool:
    """Checks whether the user holds one of the given roles — `Admin` always passes.

    Shared foundation for `require_any_role` and for endpoints whose role check depends
    on the request body (e.g. the document type in `POST /api/documents`) and therefore
    cannot be expressed as a plain `Depends` declaration.
    """
    user_roles = set(user.get("roles", []))
    return "Admin" in user_roles or bool(set(roles) & user_roles)


def require_any_role(*roles: str):
    """FastAPI dependency — blocks when the user holds none of the given roles.

    Many endpoints admit more than one role (e.g. `Sales` **or** `Purchasing`) —
    `require_any_role("Sales", "Purchasing")` checks with OR. A single argument covers
    the single-role case, so a separate `require_role` variant would be redundant.

    `Admin` passes every check without being listed here — see `has_role`.
    """
    def check(user: dict = Depends(get_current_user)):
        """The actual dependency — raises 403, or passes the user through."""
        if not has_role(user, *roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised.")
        return user
    return check
