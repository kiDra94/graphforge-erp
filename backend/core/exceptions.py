"""Business exceptions and their global FastAPI handlers.

Each exception stands for exactly one HTTP status: NotFoundError 404, DuplicateKeyError
409, BusinessLogicError 400, DatabaseError 500. Routers and services raise them, the
handlers registered here translate them into a JSON response.

**Every error response carries the same key `message` holding a string.** That includes
the two cases FastAPI produces itself, which are deliberately overridden here:

- `RequestValidationError` (422) returns `detail` as a *list* of objects by default.
- `HTTPException` (401, 403, 405) returns `detail` as a string.

Without these two handlers every client would have to tell three shapes apart and read
them differently depending on the status code. With them, `body.message` is enough.
"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

# Prefixes Pydantic puts in front of the error location. To the user, "password" is the
# information — not "body.password". They did not fill in a body object, they filled in
# a form field.
_LOC_PREFIXES: frozenset[str] = frozenset({"body", "query", "path", "header", "cookie"})


# --- Business exceptions ---
class NotFoundError(Exception):
    """Raised when a requested resource does not exist.

    Examples: a product or an asset is not in the graph.
    Translated automatically into HTTP 404 (Not Found).
    """

class DuplicateKeyError(Exception):
    """Raised when an entity already exists.

    Example: a Neo4j constraint violation on a product number.
    Translated automatically into HTTP 409 (Conflict).
    """

class BusinessLogicError(Exception):
    """Raised when a business rule or validation fails.

    Example: quantity <= 0 in a bill of materials.
    Translated automatically into HTTP 400 (Bad Request).
    """

class DatabaseError(Exception):
    """Raised on unexpected database failures.

    Examples: a dropped connection, or Neo4j errors the client did not cause.
    Translated automatically into HTTP 500 (Internal Server Error).
    """


# --- FastAPI exception handlers ---
async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catches NotFoundError globally and translates it into an HTTP 404 response.

    Args:
        request (Request): The incoming FastAPI request.
        exc (Exception): The raised NotFoundError.

    Returns:
        JSONResponse: JSON response with status code 404 and the error message.
    """
    req_logger = logger.bind(request_id=getattr(request.state, "request_id", "-"))
    req_logger.warning(f"404 Not Found: {request.method} {request.url.path} - {exc!s}")
    return JSONResponse(status_code=404, content={"message": str(exc)})


async def duplicate_key_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catches DuplicateKeyError globally and translates it into an HTTP 409 response.

    Args:
        request (Request): The incoming FastAPI request.
        exc (Exception): The raised DuplicateKeyError.

    Returns:
        JSONResponse: JSON response with status code 409 and the error message.
    """
    req_logger = logger.bind(request_id=getattr(request.state, "request_id", "-"))
    req_logger.warning(f"409 Conflict: {request.method} {request.url.path} - {exc!s}")
    return JSONResponse(status_code=409, content={"message": str(exc)})


async def business_logic_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catches BusinessLogicError globally and translates it into an HTTP 400 response.

    Args:
        request (Request): The incoming FastAPI request.
        exc (Exception): The raised BusinessLogicError.

    Returns:
        JSONResponse: JSON response with status code 400 and the error message.
    """
    req_logger = logger.bind(request_id=getattr(request.state, "request_id", "-"))
    req_logger.warning(f"400 Business Error: {request.method} {request.url.path} - {exc!s}")
    return JSONResponse(status_code=400, content={"message": str(exc)})


async def database_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catches DatabaseError globally and translates it into an HTTP 500 response.

    Hides the real database details from the API client for security reasons and returns
    a generic message instead.

    Args:
        request (Request): The incoming FastAPI request.
        exc (Exception): The raised DatabaseError.

    Returns:
        JSONResponse: JSON response with status code 500 and a generic message.
    """
    req_logger = logger.bind(request_id=getattr(request.state, "request_id", "-"))
    req_logger.exception(f"500 Database Error: {request.method} {request.url.path} - {exc!s}")
    return JSONResponse(
        status_code=500, content={"message": "An internal database error occurred."}
    )


def _field_path(loc: tuple) -> str:
    """Turns Pydantic's error location into a readable field name.

    `("body", "password")` becomes `password`, `("body", "lines", 0, "quantity")` becomes
    `lines.0.quantity`. The leading `body`/`query`/... is dropped: it names the transport
    layer, not the field the user filled in.

    Args:
        loc (tuple): The `loc` entry of a single Pydantic error.

    Returns:
        str: The field path, or "request" when nothing is left after the prefix.
    """
    parts = list(loc)
    if parts and parts[0] in _LOC_PREFIXES:
        parts = parts[1:]
    return ".".join(str(part) for part in parts) or "request"


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translates a schema violation into the same response shape as every other error.

    **Why this is needed.** FastAPI answers a `RequestValidationError` with
    `{"detail": [ {...}, ... ]}` — a list of objects, not a string. A client expecting
    `message` gets nothing here; one rendering `detail` raw puts JSON on the screen.
    This handler brings both cases onto `message`.

    **Why `input` is discarded.** Pydantic puts the offending value into every error
    object. On `POST /api/auth/login` without an e-mail address, that value is the
    submitted password in plaintext — and it would end up in the response, in the browser
    log and in every tool that records error responses. Only the field name and the
    reason are passed on, never the value.

    The field name survives, and that is the point: with an unknown key
    (`extra="forbid"`, see `core.schemas.InputModel`) the response names exactly the key
    the client sent too many.

    Args:
        request (Request): The incoming FastAPI request.
        exc (Exception): The raised `RequestValidationError`.

    Returns:
        JSONResponse: JSON response with status code 422 and the error message.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    single = [f"{_field_path(e.get('loc', ()))}: {e.get('msg', 'invalid')}" for e in errors]
    text = "; ".join(single) or "The request does not match the expected schema."

    req_logger = logger.bind(request_id=getattr(request.state, "request_id", "-"))
    # Deliberately only `text`, never `exc.errors()`: the latter carries `input` and with
    # it the submitted values into the log.
    req_logger.warning(f"422 Validation Error: {request.method} {request.url.path} - {text}")
    return JSONResponse(status_code=422, content={"message": f"Invalid input: {text}"})


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Brings the errors FastAPI raises itself onto the same response shape.

    Mainly affects `401` (missing or invalid token) and `403` (missing role), both coming
    from `core.security`, as well as `405` on a wrong HTTP method. FastAPI returns
    `detail` instead of `message` there.

    The exception's headers are passed through. That is not a detail: a `401` carries
    `WWW-Authenticate`, and without that header the response is no longer protocol
    compliant.

    Args:
        request (Request): The incoming FastAPI request.
        exc (Exception): The raised `HTTPException`.

    Returns:
        JSONResponse: JSON response with the original status code and `message`.
    """
    if not isinstance(exc, StarletteHTTPException):
        raise exc

    req_logger = logger.bind(request_id=getattr(request.state, "request_id", "-"))
    req_logger.warning(
        f"{exc.status_code} HTTP Error: {request.method} {request.url.path} - {exc.detail}"
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": str(exc.detail)},
        headers=getattr(exc, "headers", None),
    )
