"""Configuration of the Loguru logger for console and log file."""

import sys
from pathlib import Path

from loguru import logger

from core.config import get_settings


def configure_logging() -> None:
    """Sets up the log sinks for console and file.

    Removes Loguru's default sink first, so nothing is printed twice, then attaches
    stderr. A file sink is added only when `LOG_FILE` is set (rotating at 10 MB, kept for
    14 days, compressed).

    Both formats carry `extra[request_id]`. When the field is missing from a record,
    Loguru does NOT raise at the caller: it reports a handler error on stderr internally
    and the log line is lost without a trace. Inside a request the middleware sets the
    `request_id` once via `logger.contextualize(...)` — every log call down the stack,
    including deep in the service layer, then picks it up automatically without being
    passed it. Only code running outside a request (the lifespan on start and stop, for
    instance) has to bind it itself, there with the placeholder '-'.

    Called once at application start.
    """
    settings = get_settings()

    logger.remove()

    logger.add(
        sys.stderr,
        level=getattr(settings, "LOG_LEVEL", "INFO"),
        colorize=True,
        backtrace=True,
        diagnose=getattr(settings, "DEBUG", False),
        enqueue=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "{extra[request_id]} | "
            "<level>{message}</level>"
        ),
    )

    log_file = getattr(settings, "LOG_FILE", None)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level=getattr(settings, "LOG_LEVEL", "INFO"),
            rotation="10 MB",
            retention="14 days",
            compression="zip",
            enqueue=True,
            # No backtrace and no diagnose in the file sink: `diagnose` prints local
            # variables into the log, and those carry passwords and customer data.
            backtrace=False,
            diagnose=False,
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
                "{level: <8} | "
                "{name}:{function}:{line} | "
                "{extra[request_id]} | "
                "{message}"
            ),
        )
