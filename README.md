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

**v0.1.0a2.** Indexing, the graph, impact analysis, and the MCP read surface all
work end to end against real Python codebases — this is not a contracts-only
preview. It is also young: the adapter set is Python-first, the CLI has two
subcommands, and every analysis result discloses its own known blind spots rather
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

```bash
pip install "hashira[mcp]"
hashira index ./my-project
hashira mcp --db ./my-project/.hashira/hashira.db --system my-project
```

**1. Index a project.**

```bash
hashira index ./my-project
```

Give it any directory — your own project, not this one. This is a real,
production command, not a stopgap: it runs every adapter Hashira ships
(Python, Django, FastAPI, SQLAlchemy, Git) against the given root and writes
the result to a SQLite database. There is no separate "which framework does my
project use" flag to get right or get wrong — each adapter only ever produces
entities for constructs it actually recognizes (a plain-Python project simply
gets nothing from the Django/FastAPI/SQLAlchemy adapters, at negligible cost),
so offering all of them is both the simplest design and the correct one.

```
Indexing /Users/you/my-project as system 'my-project' (revision: 3f2a1c9e4b...)...
  files processed:         128
  entities upserted:       412
  relationships upserted:  530

Database: /Users/you/my-project/.hashira/hashira.db
Next:     hashira mcp --db /Users/you/my-project/.hashira/hashira.db --system my-project
```

By default the database is created at `<project_root>/.hashira/hashira.db` —
that's real, ordinary state (git-ignore it), not a hidden cache; use `--db` to
put it somewhere else. The system slug defaults to the project directory's
name, slugified; use `--system` to pick your own. **Re-running `hashira index`
on the same project is safe and expected** — it reuses the existing system
(matched by slug) rather than creating a second one, which is what lets
Hashira track identity across revisions as your code changes.

If the target isn't a Git repository, indexing still works, just without
revision history. If it is one, but the directory you pointed at isn't the
Git repository's own root, `hashira index` tells you so explicitly — see
**Known limitations** below for why that distinction matters and isn't
resolved automatically.

**2. Start the MCP server** (needs `pip install "hashira[mcp]"` — `hashira
index` alone doesn't):

```bash
hashira mcp --db ./my-project/.hashira/hashira.db --system my-project
```

This runs the MCP read surface over stdio for the system slug you indexed. It
is read-only: no tool can modify your code, trigger a re-index, or call an
LLM.

**3. Connect an MCP-compatible coding agent.** For a client that reads a JSON
MCP config (e.g. Claude Desktop, Claude Code), add an entry like:

```json
{
  "mcpServers": {
    "hashira": {
      "command": "hashira",
      "args": ["mcp", "--db", "/absolute/path/to/my-project/.hashira/hashira.db", "--system", "my-project"]
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

**Advanced / programmatic use.** `hashira index` always wires up every
adapter, unconditionally — that's the right default for the CLI, but if you're
scripting Hashira directly (a CI job, a custom pipeline) rather than using the
CLI, the same `IndexingService`/adapter classes `hashira index` calls are a
public API; see `src/hashira/cli/main.py::_run_index` for exactly how they're
composed if you want to assemble your own subset.

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

**Monorepo / import-root caveat.** Point `hashira index` at your project's
actual Python import root, not necessarily your Git repository root. If your
repository looks like `repo/backend/` (Python) + `repo/frontend/` (not
Python), index `repo/backend`, not `repo`. Indexing at the wrong root doesn't
error — it silently under-resolves cross-file relationships (imports, calls,
field reads/writes), which is a worse failure mode than a crash because
nothing tells you it happened. `hashira index` detects and reports when the
root you gave it differs from the Git repository root it's inside (a note on
stderr, not an error — indexing still proceeds with the root you actually
gave it), but it does not try to guess your real import root for you: real
repositories diverge in too many shapes (`backend/src/myapp/`, several
sibling service directories, `apps/{api,worker,admin}/`) for a heuristic to
be trustworthy yet. See [docs/ADAPTERS.md](docs/ADAPTERS.md) for the full
account.

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
