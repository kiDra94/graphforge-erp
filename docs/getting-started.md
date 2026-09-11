---
title: "Getting started"
tags:
  - setup
  - docker
  - neo4j
---

# Getting started

There are two ways to run the backend.
Both end at **http://localhost:8000/api/docs** — under `/api`, not at the root, because behind a reverse proxy only `/api` reaches the backend.

- [Option A — Docker Compose](#option-a--docker-compose-the-whole-stack): database, backend and demo data, nothing installed locally.
- [Option B — local backend](#option-b--local-backend-against-your-own-neo4j): the backend through `uv`, against a Neo4j of your own.

Afterwards: [sign in](#sign-in) with one of the [demo accounts](#demo-accounts), and [run the checks](#tests-lint-type-check).
To put a public demo online, see [deployment.md](deployment.md).

---

## Requirements

| Option A | Option B |
| --- | --- |
| Docker with Compose | **Python 3.14** or newer |
| | **[uv](https://github.com/astral-sh/uv)** |
| | A running **Neo4j 5** instance (Docker is the easiest way to get one) |

---

## Option A — Docker Compose: the whole stack

Clone the repository and work from its root:

```bash
git clone https://github.com/kiDra94/graphforge-erp.git
cd graphforge-erp
cp .env.example .env                        # set NEO4J_PASSWORD and JWT_SECRET_KEY
docker compose up -d                        # neo4j + backend
docker compose --profile seed up seed       # once: schema and demo data
```

A JWT secret can be generated with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The backend waits for the database's health check before it starts, so the first `up` takes a little while.
The seed sits behind a profile on purpose: it is a one-shot job and should not rerun on every `up`.

The Neo4j browser is available at **http://localhost:7474**.

---

## Option B — local backend against your own Neo4j

### 1. Configuration

```bash
cd backend
cp .env.example .env
```

Put the connection details of your Neo4j instance into `backend/.env` and replace the JWT secret (see the command above).
`pydantic-settings` reads environment variables ahead of the `.env`, so in a container the file is not needed — see [`core/config.py`](../backend/core/config.py).

> `backend/.env.example` configures the backend when it runs through `uv`.
> The `.env.example` in the repository root configures the compose stack.
> They are not interchangeable.

### 2. Database

If you do not have a Neo4j instance yet:

```bash
docker run -d --name neo4j -p 7687:7687 -p 7474:7474 \
  -e NEO4J_AUTH=neo4j/your-password neo4j:5-community
```

### 3. Demo data

```bash
docker exec -i neo4j cypher-shell -u neo4j -p your-password < ../seed.cypher
```

[`seed.cypher`](../seed.cypher) creates the schema — constraints and indexes — and a small, consistent data set: 13 products with bills of materials, three customers, three suppliers, a full document chain, two serialised assets with their digital twin, and six employees covering every role.

The script is idempotent: every write is a `MERGE` on a business key, so a second run changes nothing.
Its last section prints one row per label, so a failed seed shows up immediately rather than as an empty list in the API later.

### 4. Dependencies and start

```bash
uv sync
uv run poe dev      # with auto-reload
uv run poe start    # plain start, as in the container
```

---

## Sign in

```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"identifier": "max.mustermann@acme.example", "password": "demo1234"}'
```

The token from the response goes into the `Authorization: Bearer <token>` header of every further request.
In the Swagger UI, the *Authorize* button takes it.

### Demo accounts

All six share the password `demo1234` and differ in their roles — which is the point, because most endpoints answer differently depending on who is asking.

| Sign-in | Role | Sees, among other things |
| --- | --- | --- |
| `max.mustermann@acme.example` | Sales | quotes, the service forecast |
| `erika.musterfrau@acme.example` | Engineering | products, bills of materials, asset releases |
| `john.doe@acme.example` | Purchasing | purchase prices, margins, reorder suggestions |
| `jane.roe@acme.example` | BackOffice, Accounting | order confirmations, invoices, contract rates |
| `sam.sample@acme.example` | Warehouse | stock, transfers, goods receipts |
| `alex.admin@acme.example` | Admin | everything — `Admin` passes every role check |

A quick way to see field masking at work: request `GET /api/products/ACME-1000` as `max.mustermann` and as `john.doe` and compare `costPrice`.

---

## Tests, lint, type check

```bash
cd backend
uv run poe test               # unit and API tests, no database needed
uv run poe test-unit          # unit tests only
uv run poe test-api           # API tests only
uv run poe test-unit-cov      # unit tests with coverage
uv run poe test-integration   # integration tests against a real Neo4j
uv run poe test-all           # everything
uv run poe lint               # ruff
uv run poe typecheck          # pyright, standard mode
```

The integration tests need nothing but a running Docker: Testcontainers starts a Neo4j for the run and removes it afterwards, so neither the compose stack nor your own database is touched.
Without Docker they are skipped.

How the test levels are split and why is described in the [backend README](../backend/README.md#tests).
