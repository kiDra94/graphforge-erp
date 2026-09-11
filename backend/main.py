"""Entry point of the FastAPI application: app assembly, middleware and router registration."""

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.database import Neo4jDatabase, close_db_session
from core.exceptions import (
    BusinessLogicError,
    DatabaseError,
    DuplicateKeyError,
    NotFoundError,
    business_logic_handler,
    database_error_handler,
    duplicate_key_handler,
    http_exception_handler,
    not_found_handler,
    validation_error_handler,
)
from core.logging_setup import configure_logging
from domains.assets.router_assets import router as assets_router
from domains.catalog.router_catalog import router as catalog_router
from domains.catalog.router_catalog import router_product_groups
from domains.iam.router_iam import router as iam_router
from domains.inventory.router_inventory import (
    router_locations,
    router_movements,
    router_stock,
)
from domains.notifications.router_notifications import router as notifications_router
from domains.procurement.router_procurement import (
    router_reorder_suggestions,
    router_suppliers,
)
from domains.realtime.router_realtime import router as realtime_router
from domains.sales.router_sales import (
    router_contracts,
    router_customers,
    router_documents,
    router_pricing,
    router_reports,
)

configure_logging()
# `request_id` is the unique key per HTTP request. Outside a request there is none, so the
# dash is bound here once instead of leaving the field empty in the log format.
app_logger = logger.bind(request_id="-")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Accompanies start and end of the application with log lines and the DB connection.

    The connectivity check belongs here and not into the first request: a wrong URI or a
    wrong password is a configuration error, and it should surface on startup rather than
    as a 500 on whichever endpoint someone happens to call first.

    Args:
        app (FastAPI): The running FastAPI instance.

    Yields:
        None: Hands control over to the application until it shuts down.
    """
    app_logger.info("Starting backend")
    driver = Neo4jDatabase.get_driver()
    await driver.verify_connectivity()
    app_logger.info("Neo4j connectivity verified")
    yield
    await close_db_session()
    app_logger.info("Backend shut down cleanly")


# The three documentation paths sit under /api, not at the root.
#
# In the Docker setup a reverse proxy stands in front and forwards only /api to the
# backend; everything else goes to the frontend. The prefix is NOT stripped, because the
# routers carry it themselves. Under the default values (/docs, /redoc, /openapi.json) the
# Swagger interface would therefore be unreachable behind the proxy — the call would land
# at the frontend.
#
# The interface loads its openapi.json over `openapi_url`, so all three values have to move
# together; otherwise Swagger shows an empty page.
app = FastAPI(
    title="GraphForge ERP backend",
    description=(
        "Reference backend of a graph-based ERP: FastAPI, Neo4j, domain-driven design. "
        "Every endpoint expects a bearer token from POST /api/auth/login."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# CORS does not take effect in the deployed setup at all.
#
# Application and API sit behind the same reverse proxy and therefore on the same origin
# ("/" to the frontend, "/api/" to the backend). The frontend calls over relative paths, so
# the browser sends no Origin header and never asks for permission. In development a dev
# server proxy takes the same role — there, too, everything is same-origin.
#
# What remains is exactly one case: an absolute API base configured to run a dev frontend
# against a backend on another machine. That one is cross-origin and needs this middleware.
# Dropping it would kill that case.
#
# `allow_credentials` stays False on purpose, and the safety of this setting hangs off it:
# authentication runs over a bearer token in the Authorization header, not over cookies. A
# foreign page may ask with `allow_origins=["*"]`, but it does not hold the token — that
# one lives in our own origin's storage and is unreadable from outside, so every protected
# endpoint answers it with a 401. Dangerous would be the combination "*" PLUS credentials;
# browsers refuse that one themselves.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Assigns a `request_id` per request and logs start, end and duration.

    Sets the id in `request.state` (for the exception handlers, which get the `Request`
    object directly) as well as through `logger.contextualize()` (for the service layer,
    which does not). That way all three levels — middleware, exception handler, service —
    carry the same `request_id` for the same request.

    Args:
        request (Request): The incoming request.
        call_next (Callable[[Request], Awaitable[Response]]): Passes the request on to the
            rest of the middleware chain and the route, and returns the response that came
            out of it.

    Returns:
        Response: The route's answer, with the `X-Request-ID` header added.
    """
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id

    # contextualize() binds the request_id to a ContextVar that holds across the entire
    # call chain of this request — deep in the service layer as well, which never sees the
    # request object itself. Every logger.info(...) in there picks it up automatically,
    # without having to be handed the id.
    with logger.contextualize(request_id=request_id):
        start = time.perf_counter()
        logger.info(f"HTTP {request.method} {request.url.path} started")

        try:
            response = await call_next(request)
        except Exception:
            logger.exception(f"Unhandled exception on {request.method} {request.url.path}")
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            f"HTTP {request.method} {request.url.path} -> {response.status_code} "
            f"in {duration_ms:.2f} ms"
        )

    response.headers["X-Request-ID"] = request_id
    return response


# Register the exception handlers. The four business exceptions are raised by the service
# layer and translated into a status code exactly once, here — a router that formed its own
# HTTPException would be the second place the same rule lives.
app.add_exception_handler(NotFoundError, not_found_handler)
app.add_exception_handler(DuplicateKeyError, duplicate_key_handler)
app.add_exception_handler(BusinessLogicError, business_logic_handler)
app.add_exception_handler(DatabaseError, database_error_handler)

# The two handlers FastAPI brings along are overridden on purpose: they answer with
# `detail` instead of `message` — on a 422 even with a list of objects instead of a text.
# Without these lines every client would have to know three answer shapes. The reasoning
# in detail sits in core/exceptions.py.
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)

# Bind the routers to the FastAPI app. One line per router rather than a loop: the order
# decides nothing here (every router carries its own prefix), but a list that can be read
# top to bottom says which domains the application is assembled from.
app.include_router(iam_router)
app.include_router(catalog_router)
app.include_router(router_product_groups)
app.include_router(router_locations)
app.include_router(router_stock)
app.include_router(router_movements)
app.include_router(router_suppliers)
app.include_router(router_reorder_suggestions)
app.include_router(router_customers)
app.include_router(router_documents)
app.include_router(router_contracts)
app.include_router(router_pricing)
app.include_router(router_reports)
app.include_router(assets_router)
app.include_router(notifications_router)
app.include_router(realtime_router)


@app.get("/", tags=["Health"])
async def root() -> dict:
    """Reports that the application is answering.

    The only endpoint without authentication, and it says nothing about the data: the
    container health check calls it, and a health check must not need a token.

    Returns:
        dict: Status and version.
    """
    return {"status": "ok", "service": "graphforge-erp-backend", "version": app.version}
