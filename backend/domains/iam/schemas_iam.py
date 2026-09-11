"""Pydantic schemas for the IAM domain (login, token, users)."""

from pydantic import BaseModel, EmailStr, Field

from core.schemas import InputModel


class LoginRequest(InputModel):
    """Credentials for `POST /api/auth/login`."""

    identifier: str = Field(description="E-mail address or initials of the employee.")
    password: str = Field(description="Password in plaintext.")


class TokenResponse(BaseModel):
    """Response of a successful sign-in.

    Field names and `token_type: "bearer"` follow OAuth2 — hence `access_token` in snake
    case here, rather than the camel case used everywhere else in this project.
    """

    access_token: str = Field(description="The JWT for subsequent API requests.")
    token_type: str = Field(default="bearer")


class EmployeeOut(BaseModel):
    """Read model of an employee.

    Deliberately carries no password field — neither the plaintext nor the hash ever
    leaves the server.
    """

    id: str
    name: str
    initials: str | None = None
    email: EmailStr
    roles: list[str]
    active: bool = True


class EmployeeCreate(InputModel):
    """Input model for creating an employee.

    `roles` lists the functional roles only. The additive base role *User* is set by the
    repository regardless — an empty list is therefore valid and yields a read-only
    account.
    """

    name: str = Field(description="Full name.")
    initials: str = Field(
        min_length=2,
        max_length=5,
        pattern=r"^[A-Za-z0-9]+$",
        description="Initials, e.g. 'MM'. 2-5 alphanumeric characters, unique.",
    )
    email: EmailStr
    password: str = Field(description="Initial password.")
    roles: list[str] = Field(description="List of roles, e.g. ['Sales'].")


class PasswordChangeRequest(InputModel):
    """Input model for changing one's own password.

    The current password is mandatory: a valid token alone must not be enough to
    overwrite it. An administrator resets someone else's password through
    `EmployeeUpdate` instead, without knowing the old one.
    """

    currentPassword: str = Field(description="Current password, for verification.")
    newPassword: str = Field(min_length=8, description="New password (at least 8 characters).")


class EmployeeUpdate(InputModel):
    """Input model for changing an employee — every field optional.

    Only fields actually sent are written. Omitting `password` means "unchanged"; setting
    it replaces the hash without knowing the previous one. `active: false` locks the
    account instead of deleting the employee — they stay on record as the author of past
    changes.
    """

    name: str | None = None
    initials: str | None = Field(
        default=None,
        min_length=2,
        max_length=5,
        pattern=r"^[A-Za-z0-9]+$",
        description="Initials, e.g. 'MM'. 2-5 alphanumeric characters, unique.",
    )
    email: EmailStr | None = None
    roles: list[str] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, description="New password (optional).")
