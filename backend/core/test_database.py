"""Unit tests of the driver singleton — with a mocked driver, no real database."""

from unittest.mock import AsyncMock

import pytest

from .database import Neo4jDatabase


def test_database_singleton():
    with pytest.raises(RuntimeError) as excinfo:
        Neo4jDatabase()
    assert "Direct instantiation is not allowed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_database_driver_cycle(monkeypatch):
    Neo4jDatabase._driver = None

    mock_driver = AsyncMock()
    monkeypatch.setattr(
        "core.database.AsyncGraphDatabase.driver",
        lambda *args, **kwargs: mock_driver
    )

    driver = Neo4jDatabase.get_driver()

    # Second call must not build a second driver — that is the whole point of the
    # singleton: one connection pool for the lifetime of the application.
    Neo4jDatabase.get_driver()

    assert Neo4jDatabase._driver is not None
    assert driver == mock_driver

    await Neo4jDatabase.close_driver()
    assert Neo4jDatabase._driver is None
    mock_driver.close.assert_awaited_once()
