"""Neo4j driver as a singleton, plus the session dependency for FastAPI."""

from collections.abc import AsyncGenerator

from loguru import logger
from neo4j import AsyncDriver, AsyncGraphDatabase

from core.config import get_settings


class Neo4jDatabase:
    """Singleton managing the asynchronous Neo4j driver instance.

    Guarantees that exactly one connection pool (driver) exists for the whole runtime of
    the application, so connections are pooled rather than re-established per request.
    """
    _driver: AsyncDriver | None = None

    def __init__(self) -> None:
        """Blocks direct instantiation of the singleton.

        Raises:
            RuntimeError: Whenever the class is instantiated directly.
        """
        raise RuntimeError(
            "Direct instantiation is not allowed. Use Neo4jDatabase.get_driver() instead."
        )

    @classmethod
    def get_driver(cls) -> AsyncDriver:
        """Creates the asynchronous Neo4j driver, or returns the existing one.

        Pulls the credentials from the central configuration (`get_settings`) on first
        creation.

        Returns:
            AsyncDriver: The active Neo4j database driver.
        """
        if cls._driver is None:
            settings = get_settings()
            logger.bind(request_id="-").info("Initialising the Neo4j driver")
            cls._driver = AsyncGraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD),
                max_connection_pool_size=settings.NEO4J_MAX_CONNECTION_POOL_SIZE,
                connection_acquisition_timeout=settings.NEO4J_CONNECTION_ACQUISITION_TIMEOUT,
                connection_timeout=settings.NEO4J_CONNECTION_TIMEOUT,
                max_connection_lifetime=settings.NEO4J_MAX_CONNECTION_LIFETIME,
            )
        return cls._driver

    @classmethod
    async def close_driver(cls) -> None:
        """Closes the driver connection safely and resets the instance.

        Important on shutdown, to avoid memory leaks and sockets left open.
        """
        if cls._driver is not None:
            logger.bind(request_id="-").info("Closing the Neo4j driver")
            await cls._driver.close()
            cls._driver = None


async def get_db_session() -> AsyncGenerator:
    """FastAPI dependency providing a database session.

    Used in routers via `Depends(get_db_session)`. Creates one asynchronous session per
    request (from the singleton driver) and closes it automatically afterwards.

    Yields:
        AsyncGenerator: An asynchronous Neo4j database session.
    """
    driver = Neo4jDatabase.get_driver()
    async with driver.session() as session:
        yield session


async def close_db_session() -> None:
    """Application-wide helper for releasing resources.

    Called from the FastAPI lifespan event during shutdown, to make sure the singleton
    driver is closed properly and no connections stay open.
    """
    await Neo4jDatabase.close_driver()
