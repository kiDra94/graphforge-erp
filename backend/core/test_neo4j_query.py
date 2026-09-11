"""Unit tests of the transaction helpers, against a fake session instead of Neo4j.

Every helper wraps `session.execute_read`/`execute_write` with its own inner transaction
function. Repository tests usually mock these helpers away — here the inner function
(`tx.run(...)` -> `.single()`/iteration/`.consume()`) actually runs, against a fake
session that behaves like the real driver.
"""

from unittest.mock import AsyncMock

import pytest

from core.neo4j_query import read_many, read_single, write_many, write_single, write_summary


class _FakeResult:
    def __init__(self, records):
        self._records = records

    async def single(self):
        return self._records[0] if self._records else None

    async def __aiter__(self):
        for record in self._records:
            yield record

    async def consume(self):
        return "a-summary"


class _FakeSession:
    """Actually runs the transaction function it is given, the way the real driver does."""

    def __init__(self, records):
        self._tx = AsyncMock()
        self._tx.run.return_value = _FakeResult(records)

    async def execute_read(self, transaction_function, *args, **kwargs):
        return await transaction_function(self._tx, *args, **kwargs)

    execute_write = execute_read


@pytest.mark.asyncio
async def test_read_single_returns_the_first_record():
    session = _FakeSession([{"number": "1"}, {"number": "2"}])

    result = await read_single(session, "MATCH (n) RETURN n", number="1")  # type: ignore[arg-type]

    assert result == {"number": "1"}
    session._tx.run.assert_awaited_once_with("MATCH (n) RETURN n", {"number": "1"})


@pytest.mark.asyncio
async def test_read_single_returns_none_without_a_hit():
    session = _FakeSession([])

    result = await read_single(session, "MATCH (n) RETURN n")  # type: ignore[arg-type]

    assert result is None


@pytest.mark.asyncio
async def test_read_many_returns_every_record():
    session = _FakeSession([{"number": "1"}, {"number": "2"}])

    result = await read_many(session, "MATCH (n) RETURN n")  # type: ignore[arg-type]

    assert result == [{"number": "1"}, {"number": "2"}]


@pytest.mark.asyncio
async def test_read_many_returns_an_empty_list_without_a_hit():
    session = _FakeSession([])

    result = await read_many(session, "MATCH (n) RETURN n")  # type: ignore[arg-type]

    assert result == []


@pytest.mark.asyncio
async def test_write_single_returns_the_written_record():
    session = _FakeSession([{"number": "new"}])

    result = await write_single(session, "CREATE (n) RETURN n", number="new")  # type: ignore[arg-type]

    assert result == {"number": "new"}


@pytest.mark.asyncio
async def test_write_single_returns_none_when_nothing_was_matched():
    session = _FakeSession([])

    result = await write_single(session, "MATCH (n) SET n.x = 1 RETURN n")  # type: ignore[arg-type]

    assert result is None


@pytest.mark.asyncio
async def test_write_many_returns_every_row():
    """The reason write_many exists: write_single's `.single()` raises on several rows."""
    session = _FakeSession([{"number": "1"}, {"number": "2"}])

    result = await write_many(session, "UNWIND $rows AS row CREATE (n) RETURN n")  # type: ignore[arg-type]

    assert result == [{"number": "1"}, {"number": "2"}]


@pytest.mark.asyncio
async def test_write_summary_returns_the_summary():
    session = _FakeSession([])

    result = await write_summary(session, "MATCH (n) DELETE n")  # type: ignore[arg-type]

    assert result == "a-summary"
