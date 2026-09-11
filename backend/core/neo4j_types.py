"""Translation between the temporal types of the Neo4j driver and Pydantic.

The driver returns `neo4j.time.Date` and `neo4j.time.DateTime`. Neither is a subclass of
`datetime.date` or `datetime.datetime`, so Pydantic rejects them with a validation error.
This module provides the two annotated types a read model uses to accept driver values
directly.

Cross-domain, because every domain reading timestamps from the graph has the same
problem.
"""

from datetime import date, datetime
from typing import Annotated, Any

from pydantic.functional_validators import BeforeValidator


def parse_neo4j_dt(value: Any) -> Any:
    """Converts a temporal Neo4j object into its native Python equivalent.

    Used as a Pydantic BeforeValidator, so it runs ahead of the actual type check. Values
    without `to_native()` are passed through untouched — which keeps the annotated types
    usable for values that are already native (tests, fixtures, hand-built dictionaries).

    Args:
        value (Any): The value to inspect, potentially a neo4j.time.Date/DateTime.

    Returns:
        Any: The native Python object when a to_native() method exists, otherwise the
            original value untouched.
    """
    if hasattr(value, "to_native"):
        return value.to_native()
    return value


# Two aliases, because the graph holds both types: business date fields sit on the node
# as `date`, the technical timestamps `createdAt`/`updatedAt` as `datetime`.
#
# The aliases are not interchangeable. A `date` property annotated as `Neo4jDatetime`
# does not fail loudly — Pydantic silently lifts a `date` to midnight — but the response
# then reads "2026-03-18T00:00:00" instead of "2026-03-18", which is a different value
# than the API promises.
Neo4jDatetime = Annotated[datetime, BeforeValidator(parse_neo4j_dt)]
Neo4jDate = Annotated[date, BeforeValidator(parse_neo4j_dt)]
