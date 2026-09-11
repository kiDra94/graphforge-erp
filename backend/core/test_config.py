"""Unit tests of the configuration: loading from the environment, required fields, caching."""

import pytest
from pydantic import ValidationError

from core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    """Empties the settings cache before and after every test in this module.

    `get_settings` caches with `@lru_cache`, so the instance built from this module's
    monkeypatched environment would otherwise outlive the test and be handed to every
    later one — the JWT tests would then sign their tokens with a secret set here. The
    tests still pass that way, which is exactly what makes the coupling worth removing:
    it only surfaces once someone reorders the suite.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_load_valid_env_variables(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "neo4j://localhost:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "test_username")
    monkeypatch.setenv("NEO4J_PASSWORD", "test_password")
    monkeypatch.setenv("JWT_SECRET_KEY", "test_secret")

    get_settings.cache_clear()
    settings = get_settings()

    assert settings.NEO4J_URI == "neo4j://localhost:7687"
    assert settings.NEO4J_PASSWORD == "test_password"
    assert settings.NEO4J_USERNAME == "test_username"


def test_settings_missing_env_variables_raises_error(monkeypatch):
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    get_settings.cache_clear()

    with pytest.raises(ValidationError) as excinfo:
        get_settings()

    assert "NEO4J_URI" in str(excinfo.value)


def test_settings_caching(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("NEO4J_URI", "neo4j://localhost:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "test_username")
    monkeypatch.setenv("NEO4J_PASSWORD", "test_password")
    monkeypatch.setenv("JWT_SECRET_KEY", "test_secret")

    a = get_settings()
    b = get_settings()

    assert a is b
