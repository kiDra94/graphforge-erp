"""Unit tests for the translation of temporal Neo4j types into Pydantic."""

from datetime import date, datetime

from pydantic import BaseModel

from core.neo4j_types import Neo4jDate, Neo4jDatetime, parse_neo4j_dt


class _Neo4jTimeStub:
    """Stub for neo4j.time.Date/DateTime — carries nothing but to_native()."""

    def __init__(self, native_object):
        self._native_object = native_object

    def to_native(self):
        return self._native_object


def test_parse_neo4j_dt_converts_a_driver_object():
    native_object = datetime(2026, 3, 18, 12, 0)
    stub = _Neo4jTimeStub(native_object)

    assert parse_neo4j_dt(stub) == native_object


def test_parse_neo4j_dt_leaves_already_native_values_untouched():
    native_value = datetime(2026, 3, 18, 12, 0)

    assert parse_neo4j_dt(native_value) is native_value


def test_parse_neo4j_dt_leaves_non_temporal_values_untouched():
    assert parse_neo4j_dt("2026-03-18") == "2026-03-18"
    assert parse_neo4j_dt(None) is None


class _Model(BaseModel):
    timestamp: Neo4jDatetime
    day: Neo4jDate


def test_neo4jdatetime_accepts_a_driver_object_with_to_native():
    model = _Model.model_validate({
        "timestamp": _Neo4jTimeStub(datetime(2026, 3, 18, 12, 0)),
        "day": _Neo4jTimeStub(date(2026, 3, 18)),
    })

    assert model.timestamp == datetime(2026, 3, 18, 12, 0)
    assert model.day == date(2026, 3, 18)


def test_neo4jdatetime_also_accepts_native_python_values():
    model = _Model.model_validate({
        "timestamp": datetime(2026, 3, 18, 12, 0),
        "day": date(2026, 3, 18),
    })

    assert model.timestamp == datetime(2026, 3, 18, 12, 0)
    assert model.day == date(2026, 3, 18)
