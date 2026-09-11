"""Central application configuration from environment variables and `.env`."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration class of the application.

    Loads and validates environment variables from the system environment or from a
    `.env` file. Holds the Neo4j connection settings as well as general application
    settings such as logging and debug mode.
    """
    NEO4J_URI: str = Field(
        description="URI of the Neo4j database (e.g. bolt://localhost:7687 or neo4j+s://...)."
    )
    NEO4J_USERNAME: str = Field(
        description="Username for Neo4j authentication (usually 'neo4j')."
    )
    NEO4J_PASSWORD: str = Field(
        description="Password for the Neo4j database."
    )
    NEO4J_MAX_CONNECTION_POOL_SIZE: int = Field(
        default=50,
        description="Maximum number of concurrent connections in the Neo4j connection pool."
    )
    NEO4J_CONNECTION_ACQUISITION_TIMEOUT: int = Field(
        default=10,
        description="Seconds a session waits for a free pool connection before failing."
    )
    NEO4J_CONNECTION_TIMEOUT: int = Field(
        default=10,
        description="Seconds before establishing a new TCP connection to Neo4j times out."
    )
    NEO4J_MAX_CONNECTION_LIFETIME: int = Field(
        default=3600,
        description="Seconds after which a pooled connection is recycled."
    )

    JWT_SECRET_KEY: str = Field(
        description="Secret key used to sign JWTs."
    )
    JWT_ALGORITHM: str = Field(
        default="HS256",
        description="JWT signing algorithm."
    )
    JWT_EXPIRE_MINUTES: int = Field(
        default=480,
        description="Token lifetime in minutes."
    )

    LOG_LEVEL: str = Field(
        default="INFO",
        description="Minimum log level of the application, e.g. DEBUG, INFO, WARNING or ERROR."
    )
    LOG_FILE: str | None = Field(
        default="logs/app.log",
        description="Optional file path for log output. Without a path, logs go to the console only."
    )
    DEBUG: bool = Field(
        default=False,
        description="Enables debug mode: more detailed logs and extended error messages."
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"  # Ignores additional (undeclared) variables in the .env
    )


@lru_cache
def get_settings() -> Settings:
    """Instantiates the settings once and caches the result.

    Uses the singleton pattern via `@lru_cache` so the `.env` file is not re-read from
    disk on every call (e.g. as a dependency in FastAPI routers). That saves resources
    and keeps request handling fast.

    Returns:
        Settings: The globally valid, validated configuration instance.
    """
    return Settings()  # type: ignore
