"""Helpers for Cypher queries inside managed transactions.

All five functions wrap `session.execute_read` or `session.execute_write`. Repositories
use these helpers exclusively instead of `session.run`, so that Neo4j retries transient
failures automatically.
"""

from typing import Any

from neo4j import AsyncSession, ResultSummary


async def read_single(session: AsyncSession, query: str, **params: Any) -> dict[str, Any] | None:
    """Runs a read query inside a managed transaction.

    Wrapping the query in `session.execute_read` lets Neo4j retry transient failures
    (a leader switch in a cluster, for instance) automatically, rather than failing
    immediately as an auto-commit call (`session.run`) would.

    Args:
        session (AsyncSession): The asynchronous Neo4j database session.
        query (str): The Cypher query to run.
        **params (Any): Named query parameters bound into the Cypher statement.

    Returns:
        dict[str, Any] | None: The record found, or None when the query yields nothing.

    Raises:
        Neo4jError: On failures while running the query.
    """
    async def _read_single(tx, query, params):
        # Transaction function for session.execute_read: runs the query, returns one record.
        result = await tx.run(query, params)
        return await result.single()
    return await session.execute_read(_read_single, query, params)


async def read_many(session: AsyncSession, query: str, **params: Any) -> list[dict[str, Any]]:
    """Runs a read query inside a managed transaction and returns every row.

    Wrapping the query in `session.execute_read` lets Neo4j retry transient failures
    (a leader switch in a cluster, for instance) automatically, rather than failing
    immediately as an auto-commit call (`session.run`) would.

    Args:
        session (AsyncSession): The asynchronous Neo4j database session.
        query (str): The Cypher query to run.
        **params (Any): Named query parameters bound into the Cypher statement.

    Returns:
        list[dict[str, Any]]: Every record found, or an empty list when the query yields
            nothing.

    Raises:
        Neo4jError: On failures while running the query.
    """
    async def _read_many(tx, query, params):
        # Transaction function for session.execute_read: runs the query, returns all records.
        result = await tx.run(query, params)
        return [record async for record in result]
    return await session.execute_read(_read_many, query, params)


async def write_single(session: AsyncSession, query: str, **params: Any) -> dict[str, Any] | None:
    """Runs a write query inside a managed transaction.

    Wrapping the query in `session.execute_write` lets Neo4j retry transient failures
    automatically. Intended for write queries with a `RETURN` clause (e.g.
    `CREATE`/`SET ... RETURN`).

    Args:
        session (AsyncSession): The asynchronous Neo4j database session.
        query (str): The Cypher query to run.
        **params (Any): Named query parameters bound into the Cypher statement.

    Returns:
        dict[str, Any] | None: The returned record, or None when the query matched no
            node.

    Raises:
        Neo4jError: On failures while running the query.
    """
    async def _write_single(tx, query, params):
        # Transaction function for session.execute_write: runs the query, returns one record.
        result = await tx.run(query, params)
        return await result.single()
    return await session.execute_write(_write_single, query, params)


async def write_many(session: AsyncSession, query: str, **params: Any) -> list[dict[str, Any]]:
    """Runs a write query returning **several** rows.

    The write counterpart of `read_many`, needed for queries that walk an input list with
    `UNWIND` and produce one row per element. `write_single` will not do: its
    `result.single()` raises as soon as more than one record comes back.

    Args:
        session (AsyncSession): The asynchronous Neo4j database session.
        query (str): The Cypher query to run.
        **params (Any): Named query parameters bound into the Cypher statement.

    Returns:
        list[dict[str, Any]]: Every returned record, or an empty list.

    Raises:
        Neo4jError: On failures while running the query.
    """
    async def _write_many(tx, query, params):
        # Transaction function for session.execute_write: runs the query, returns all records.
        result = await tx.run(query, params)
        return [record async for record in result]
    return await session.execute_write(_write_many, query, params)


async def write_summary(session: AsyncSession, query: str, **params: Any) -> ResultSummary:
    """Runs a write query inside a managed transaction and returns its summary.

    Wrapping the query in `session.execute_write` lets Neo4j retry transient failures
    automatically. Intended for write queries without a `RETURN` clause (e.g. `DELETE`),
    where only the summary — the counters for deleted nodes, say — is needed.

    Args:
        session (AsyncSession): The asynchronous Neo4j database session.
        query (str): The Cypher query to run.
        **params (Any): Named query parameters bound into the Cypher statement.

    Returns:
        ResultSummary: The summary of the executed query, counters included.

    Raises:
        Neo4jError: On failures while running the query.
    """
    async def _write_summary(tx, query, params):
        # Transaction function for session.execute_write: runs the query, returns the summary.
        result = await tx.run(query, params)
        return await result.consume()
    return await session.execute_write(_write_summary, query, params)
