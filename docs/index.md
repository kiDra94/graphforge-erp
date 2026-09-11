---
title: "Documentation"
---

# Documentation

Two documents, and the split between them is the one that matters:

- **[architecture.md](architecture.md)** — how the backend is built and why: the layers, the conventions every domain follows, the decisions that were made deliberately and what they cost. Read this one to understand where a change belongs.
- **[data-model.md](data-model.md)** — what is in the graph: every node label, relationship type, property and constraint, each with the business reason for its existence. Read this one to understand what a query can reach.

The binding source for the data model is [`seed.cypher`](../seed.cypher); for the architecture it is the code itself. Both documents describe what exists — where one of them and the code disagree, the code wins and the document is wrong.

Running the thing is covered in [getting-started.md](getting-started.md).
