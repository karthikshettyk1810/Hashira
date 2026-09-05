# Hashira

**System Intelligence Runtime** — a durable, evidence-aware, time-aware model of a
software system, for humans and for whichever AI agent you happen to be using.

Hashira is not a coding assistant, a foundation model, or a vector-memory product.
It indexes a real codebase into a **System IR**: a technology-neutral graph of
entities (modules, classes, functions, database columns, HTTP routes...) and
relationships (calls, reads, writes, imports, exposes...) with full provenance —
then exposes that graph over MCP so an agent can answer *"what could break if I
change this?"* with cited evidence instead of a guess.

> The pillar underneath intelligent software engineering.

## Status: alpha

**v0.1.0a1.** Indexing, the graph, impact analysis, and the MCP read surface all
work end to end against real Python codebases — this is not a contracts-only
preview. It is also young: the adapter set is Python-first, the CLI has one
subcommand, and every analysis result discloses its own known blind spots rather
than claiming completeness (see **Known limitations** below). Expect rough edges,
expect the schema and CLI to change before 1.0, and read the coverage information
on every result before trusting it as exhaustive.

## Installation

```bash
pip install hashira
```

Requires **Python 3.11 or 3.12**.

The base install gives you the indexing engine (Python/Django/FastAPI/SQLAlchemy/Git
adapters) and SQLite storage. Two extras add optional pieces:

```bash
pip install "hashira[mcp]"       # the `hashira mcp` server (needed to talk to an agent)
pip install "hashira[postgres]"  # PostgreSQL storage instead of SQLite
```

## Quick start

Hashira today has no `hashira index` command — indexing is driven from a short
Python script against the public `IndexingService` API. This is the current,
fully-supported way to build a graph; it is not a stopgap standing in for a CLI
that already exists elsewhere in this project.

**1. Index a project.** The minimal case — any Python project, no framework/data
adapters — needs only the Python and Git adapters:

```python
# index_myproject.py
import subprocess
from pathlib import Path

from hashira.adapters._compose import compose_normalizers
from hashira.adapters.git import GitAdapter
from hashira.adapters.python import PythonAdapter
from hashira.application import IndexingService
from hashira.core import System
from hashira.storage.sqlite import SqliteDatabase

REPO = Path("/path/to/your/project")  # see the monorepo caveat below
DB_PATH = "hashira.db"  # created here if it doesn't exist

db = SqliteDatabase(DB_PATH)
with db.unit_of_work() as uow:
    system = System(name="My Project", slug="my-project")
    uow.systems.save(system)
    uow.commit()

service = IndexingService(
    db.unit_of_work,
    [PythonAdapter()],
    compose_normalizers(),  # add enrichers here — see below — for Django/FastAPI/SQLAlchemy
    history_adapters=[GitAdapter()],
)

revision = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
).stdout.strip()

result = service.index(REPO, system_id=system.id, revision=revision)
print(f"indexed {revision[:10]}: {len(result.errors)} errors")
```

```bash
python index_myproject.py
```

If your project uses FastAPI and/or SQLAlchemy, pass their `enrich_normalized_run`
functions into `compose_normalizers(...)` and add the matching adapter instance to
`framework_adapters`/`data_adapters`:

```python
from hashira.adapters.fastapi import FastAPIAdapter
from hashira.adapters.fastapi.normalizer import enrich_normalized_run as enrich_fastapi
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter
from hashira.adapters.sqlalchemy.normalizer import enrich_normalized_run as enrich_sqlalchemy

service = IndexingService(
    db.unit_of_work,
    [PythonAdapter()],
    compose_normalizers(enrich_fastapi, enrich_sqlalchemy),
    framework_adapters=[FastAPIAdapter()],
    data_adapters=[SQLAlchemyAdapter()],
    history_adapters=[GitAdapter()],
)
```

For Django, pass `DjangoAdapter()` as a `framework_adapters` entry the same way
(`hashira.adapters.django`) — it has no separate `enrich_normalized_run` to
compose in, since `DjangoAdapter.enrich` is a `FrameworkAdapter` in its own right.

This creates (or updates) the SQLite database at `DB_PATH` — an ordinary file, in
whatever directory you ran the script from. There's nothing implicit about its
location; point `hashira mcp --db` at that same path.

**2. Start the MCP server** (needs `pip install "hashira[mcp]"`):

```bash
hashira mcp --db hashira.db --system my-project
```

This runs the MCP read surface over stdio for the system slug you indexed. It is
read-only: no tool can modify your code, trigger a re-index, or call an LLM.

**3. Connect an MCP-compatible coding agent.** For a client that reads a JSON MCP
config (e.g. Claude Desktop, Claude Code), add an entry like:

```json
{
  "mcpServers": {
    "hashira": {
      "command": "hashira",
      "args": ["mcp", "--db", "/absolute/path/to/hashira.db", "--system", "my-project"]
    }
  }
}
```

Use an absolute path for `--db` — the server may be launched from a different
working directory than the one you indexed from.

**4. Ask it things.** Once connected, an agent has eight tools: `search_entities`,
`get_entity`, `get_relationships`, `reverse_impact`, `forward_impact`,
`summarize_impact`, `query_at_revision`, and `follow_lineage`. A typical flow:
`search_entities` to find something by name, `reverse_impact` to see what depends
on it (with full evidence per hop), `summarize_impact` first if that result is
large, `get_entity`/`get_relationships` to drill into one node or edge.

## Supported adapters

| Kind | Adapter | What it understands |
| --- | --- | --- |
| Language | `PythonAdapter` | Modules, classes, functions, imports, calls, inheritance — via `ast`, no execution |
| Framework | `DjangoAdapter` | Models, views, URL routing |
| Framework | `FastAPIAdapter` | Routes, request/response schemas |
| Data | `SQLAlchemyAdapter` | ORM models, columns, field-level reads/writes |
| History | `GitAdapter` | Commits, renames, revision-scoped queries |

Non-Python languages, and frameworks/ORMs beyond this list, aren't indexed yet.

## Known limitations

Every impact-analysis result carries its own `coverage` field — check it before
treating a result as exhaustive. `coverage.status` is `"PARTIAL"` whenever any
known gap could apply, with typed `limitations` describing what kind of edge
might be missing (raw SQL, dynamic dispatch, framework reflection, and a few
narrower cases — see [docs/IR.md](docs/IR.md) for the full, current list). This
is disclosure, not completeness: a `PARTIAL` result is Hashira telling you where
not to trust it, not a guarantee that everything outside those categories is
correct.

**Monorepo / import-root caveat.** Point `IndexingService.index()` at your
project's actual Python import root, not necessarily your Git repository root.
If your repository looks like `repo/backend/` (Python) + `repo/frontend/` (not
Python), index `repo/backend`, not `repo`. Indexing at the wrong root doesn't
error — it silently under-resolves cross-file relationships (imports, calls,
field reads/writes), which is a worse failure mode than a crash because nothing
tells you it happened. See [docs/ADAPTERS.md](docs/ADAPTERS.md) for the full
account and why this isn't auto-detected yet.

## Design commitments

- **Model-independent.** The core imports no LLM SDK. Ever. Enforced by test.
- **Deterministic before probabilistic.** Parsers, Git, schemas and telemetry
  first; models only to enrich what determinism could not settle.
- **Evidence-first.** Facts, derivations, inferences and hypotheses never
  collapse into one another.
- **Temporal by default.** Relationships are revision-bounded; history is
  readable, not overwritten.
- **Local-first.** SQLite by default, PostgreSQL when you want a shared store.

## Development

```bash
git clone <this repository>
cd hashira
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/pytest
```

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, dependency direction, merge guardrails |
| [docs/IR.md](docs/IR.md) | System IR contract, versioning, coverage/limitations, deviations from the spec |
| [docs/ADAPTERS.md](docs/ADAPTERS.md) | How each adapter works, and how to write a new one |
| [docs/EVENTS.md](docs/EVENTS.md) | Event model, idempotency, temporal semantics |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phases, milestones, and real-world validation findings |

License: Apache-2.0.
