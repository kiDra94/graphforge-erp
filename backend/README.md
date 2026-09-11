# GraphForge ERP — backend

A reference backend for a graph-based ERP: **FastAPI** on top of **Neo4j**, built along domain-driven lines.
Products with bills of materials, stock with a movement history, documents from quote to invoice, suppliers, serialised assets with a digital twin, and a notification list — 65 endpoints across eight domains.

The data in `seed.cypher` is invented. The architecture is not.

**How to run it** — the compose stack or a local backend against your own Neo4j, the demo data, sign-in and the demo accounts — is in [`docs/getting-started.md`](../docs/getting-started.md).

---

## What is worth looking at

If you are here to read code rather than to run it, these are the places where the interesting decisions sit:

| Where | What it shows |
| --- | --- |
| [`domains/sales/repository_sales/rules.py`](domains/sales/repository_sales/rules.py) | The rules of the document chain. A delivery note issues stock, an invoice behind it does not — the rule lives in one function, `_movement_type_for`, next to the other truth tables. |
| [`domains/inventory/repository_inventory.py`](domains/inventory/repository_inventory.py) | Stock as a cache over the movement history, so that `quantity = f(movements)` stays provable. One booking function every domain goes through. |
| [`domains/assets/repository_assets.py`](domains/assets/repository_assets.py) | A status that is derived rather than stored, and the same rule used twice: as a projection and as three disjoint WHERE fragments. |
| [`domains/catalog/repository_catalog.py`](domains/catalog/repository_catalog.py) | A bill-of-materials tree of any depth in one round trip, and a cycle guard that is a path query rather than a check beforehand. |
| [`core/exceptions.py`](core/exceptions.py) | Four business exceptions, four status codes, one answer shape — and a 422 that names the field but never the submitted value. |
| [`core/visibility.py`](core/visibility.py) | Role-based field masking, separate from the question of who may call an endpoint at all. |

Every module says in its docstring why it is shaped the way it is.
That is deliberate: the code is the artefact, and the reasoning belongs next to it rather than in a document that drifts out of date.

Two documents take the view the docstrings cannot: [`docs/architecture.md`](../docs/architecture.md) for the decisions that span domains, and [`docs/data-model.md`](../docs/data-model.md) for the graph itself — every node label, relationship and constraint with the reason it exists.

---

## Architecture

```
        HTTP
          │
    router_*.py      route, status code, role gate, field masking
          │
    service_*.py     business logic, logging, events — no Cypher
          │
  repository_*.py    Cypher, transactions, conversion into the read model
          │
        Neo4j
```

One folder per domain, and inside it the same four files plus their tests:

```
domains/<domain>/
├── router_<domain>.py        HTTP endpoints
├── service_<domain>.py       business logic
├── repository_<domain>.py    Cypher queries
├── schemas_<domain>.py       Pydantic models
└── test_*.py                 unit tests, next to the code they cover
```

The one exception is `sales`: its repository is a package with one module per aggregate (`customers`, `documents`, `contracts`, `pricing`, `reports`) plus the pure `rules`, because the document chain alone is larger than any other domain's repository.

The eight domains: `iam`, `catalog`, `inventory`, `procurement`, `sales`, `assets`, `notifications`, `realtime` — plus `core`, which holds what every domain needs (configuration, driver, security, logging, the WebSocket manager).

Three rules hold throughout, and the docstrings name them where they bite:

- **Existence checks run in the same transaction as the write.** A check in a query of its own beforehand is a TOCTOU window.
- **Uniqueness comes from the constraint, not from a check beforehand** — `CREATE`, catch the `ConstraintError`, translate it. The exceptions are the paths that want idempotence, and they say so.
- **No `session.run()` in a repository.** Only the helpers in [`core/neo4j_query.py`](core/neo4j_query.py); where several steps have to be atomic, a transaction function over `session.execute_write`.

---

## Tests

The tests sit on three levels, split by what they replace:

- **Unit tests live next to the code they cover** (`core/`, `domains/*/test_*.py`). They test pure functions against values and repositories against an intercepted query — what is checked there is the parameter binding and the conversion, not the wording of the Cypher, except where the wording carries a rule and says so.
- **API tests live in `tests/api/`.** They send a real HTTP request through the real app: middleware, routing, Pydantic validation, role dependencies and the global exception handlers all run; only the service layer is mocked away. That is the level where a route order, a role gate and an answer shape can be proven.
- **Integration tests live in `tests/integration/`.** They run the repositories against a real Neo4j that Testcontainers starts once per run, with the constraints and indexes from `seed.cypher` and an empty graph for every test. That is the level where the Cypher itself is proven: that a query returns the right rows, that a rejected booking leaves nothing behind, that stock and movement history stay consistent.

The first two levels need no database and finish in seconds; `poe test` runs exactly those.
The integration tests take a few minutes and need Docker.
Without Docker they are skipped locally, but fail in CI, where a skipped suite would look like a passing one.

The commands for every level, lint and type check are in [`docs/getting-started.md`](../docs/getting-started.md#tests-lint-type-check).

---

## Technology

| | |
| --- | --- |
| **FastAPI** | Routing, validation and OpenAPI out of the same type annotations |
| **Neo4j** (async driver) | Bills of materials, document chains and asset structures are graphs; resolving them in SQL would mean recursive CTEs for every one of them |
| **Pydantic v2** | Tolerant read models against the graph, strict write models at the system boundary |
| **loguru** | Structured logging with a `request_id` that holds across the whole call chain |
| **pyjwt + bcrypt** | Bearer tokens, hashed passwords |
| **pytest + Testcontainers** | Unit and API tests without a database, integration tests against a throwaway Neo4j |
| **ruff + pyright** | Linting and type checking, both configured explicitly rather than by preset |

---

## Notes

A few things are deliberately simpler than they would be in a production system, and the docstrings say so where it matters.
Money is held as integer cents and converted once in the repository.
The annual service list comes about from a reading request instead of a scheduler.
The WebSocket layer keeps its connections in the memory of one process, which is why the container runs a single worker.
