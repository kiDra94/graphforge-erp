# Backend image: FastAPI + Neo4j driver, dependencies through uv.
#
# The build context is this folder. What does NOT belong in the image is listed in
# .dockerignore — first of all the local .venv, which holds absolute paths of the
# development machine.

# Version tied to pyproject.toml: requires-python >= 3.14.
FROM python:3.14-slim

# Fetch uv as a binary from Astral's official image instead of installing it through pip —
# that saves a network round trip to PyPI and an installation step.
#
# The version is pinned deliberately rather than ":latest": a build in a few months should
# produce the same resolution as the build here. 0.11.31 is the version uv.lock (revision 3)
# was generated with.
#
# Note for deployment: ghcr.io is the GitHub Container Registry, a DIFFERENT source from
# Docker Hub. A corporate network with a proxy allowlist has to permit both, otherwise the
# build breaks here. Failing that, "RUN pip install uv==0.11.31" replaces this line.
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /uvx /bin/

# UV_COMPILE_BYTECODE   Produce .pyc at build time instead of on the first request — the
#                       first call after a start is then not noticeably slower.
# UV_LINK_MODE=copy     No hardlink attempt from the cache into the .venv. In a container the
#                       two often sit on different layers; uv warns on every build otherwise.
# UV_PYTHON_DOWNLOADS   Forbids uv to download a Python of its own. It is meant to use the
#                       Python of this image — if the version does not fit, that is an error
#                       and not a silent substitution.
# PYTHONUNBUFFERED      stdout/stderr unbuffered, so "docker compose logs" shows the lines
#                       immediately rather than in blocks.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

# Do not run as root: a fault in the application should not stand in the container with full
# rights. The user is created before the install, so the .venv belongs to them from the
# start — a "chown -R" afterwards would duplicate the whole directory tree in an extra layer.
RUN useradd --create-home --uid 1000 app
WORKDIR /app
RUN chown app:app /app
USER app

# Two separate steps, so Docker can cache the dependencies: as long as pyproject.toml and
# uv.lock do not change, the expensive installation is skipped on every further build — even
# when the source code did change.
#
# --locked aborts if uv.lock no longer matches pyproject.toml. A build must not silently
#          produce a different resolution than the one that was tested.
# --no-dev leaves out the development group (pytest, ruff, pyright) — those have no business
#          in a production image.
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY --chown=app:app . .
RUN uv sync --locked --no-dev

# Put the .venv on the PATH so "uvicorn" is directly startable. Going through "uv run" would
# re-check the environment on every container start, although it is already complete in the
# image.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Checks whether the application answers requests. Deliberately the root endpoint from
# main.py: it needs no authentication. No curl/wget in the slim image, hence Python.
#
# start-period covers the case that Neo4j is still coming up: main.py verifies the database
# connection in the lifespan, and before that the server does not answer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/', timeout=4)"]

# A single worker, and that on purpose: core/websocket.py keeps the set of open connections
# in the memory of the process. With several workers two browsers would land in different
# processes and never see each other's events.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
