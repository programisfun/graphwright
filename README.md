# graphwright

An MCP server that lets an AI client **read and edit a Neo4j graph with plain Cypher**.
Two tools, no abstractions, no schema to learn:

| Tool | Access mode | What it does |
| --- | --- | --- |
| `read` | `READ` | Runs one Cypher query, returns rows as JSON. Writes are rejected by Neo4j itself. |
| `edit` | `WRITE` | Runs any Cypher — `CREATE`, `MERGE`, `SET`, `DELETE`, `DETACH DELETE`, index/constraint DDL. Accepts a list to run several statements in one transaction. |

## ⚠️ Trust model

There is **no authentication, no query inspection, and no dry run**. The client is
expected to be an LLM and is trusted to do destructive things — that is the point.
The only guardrail is on `read`, which uses Neo4j's `READ` access mode so writes fail.

Consequences:

- Bind to loopback (`127.0.0.1`) only, or put auth in front. A public port here is a
  remote `DETACH DELETE` waiting to happen.
- Anyone who can reach the port can also run `DROP CONSTRAINT` or create millions of
  nodes. Treat it like a database shell, because it is one.

### Vocabulary discipline (prompt, not enforcement)

The tool descriptions tell the client to run `CALL db.labels()` and
`CALL db.relationshipTypes()` **before introducing a new node label or relationship
type**, and to reuse the existing names (`Person` not `People`, `WORKS_AT` not
`EMPLOYED_BY`). For an existing label, it is also told to inspect the label's whole
property vocabulary before writing:

```cypher
MATCH (n:Person) UNWIND keys(n) AS k
RETURN k, count(*) AS nodes, collect(DISTINCT valueType(n[k])) AS types
ORDER BY nodes DESC
```

```
k            nodes  types
created_at   3      ["DATE NOT NULL", "STRING NOT NULL"]
score        2      ["INTEGER NOT NULL", "FLOAT NOT NULL"]
```

Names and types in one query — so one label does not accumulate `createdAt` /
`created_at`, nor a Date next to a String. A second `MATCH (n:Person) RETURN n LIMIT 25`
shows representative values. This is guidance only — nothing blocks a duplicate label.
If drift matters, check `read("CALL db.labels()")` yourself, or add a label whitelist.
Relationship types drift more than labels in practice, because both spellings read
naturally to a model.

## Run

```powershell
docker compose up -d          # Neo4j + graphwright, both loopback-only
uv run python scripts/smoke_test.py
```

| Service | Address | Notes |
| --- | --- | --- |
| `graphwright-mcp` | <http://127.0.0.1:8000/mcp> | MCP over Streamable HTTP, `restart: unless-stopped` |
| `graphwright-neo4j` | 7474 (UI), 7687 (bolt) | `neo4j` / `graphwright` |

For development, run the server from source instead of the container:

```powershell
uv sync
uv run graphwright            # same endpoint, 127.0.0.1:8000
$env:MCP_TRANSPORT='stdio'; uv run graphwright    # stdio instead of HTTP
docker compose build graphwright && docker compose up -d   # rebuild after code changes
```

The password is written twice in `docker-compose.yml` (`NEO4J_AUTH` and the
service's `NEO4J_PASSWORD`); change both together.

Smoke test (expects the HTTP server running):

```powershell
uv run python scripts/smoke_test.py
```

## Client config

```jsonc
{
  "mcp": {
    "servers": {
      "graphwright": {
        "type": "remote",
        "url": "http://127.0.0.1:8000/mcp"
      }
    }
  }
}
```

Tools then appear as `graphwright_read` and `graphwright_edit`.

## Configuration

Settings come from `.env` (gitignored; copy `.env.example`). Compose reads it twice:
for `${NEO4J_PASSWORD}` substitution and as the container's `env_file` (optional —
without it the built-in defaults apply). The container overrides `NEO4J_URI` to
`bolt://neo4j:7687`, because inside the compose network the host `localhost` would be
the container itself. `NEO4J_USER` and `NEO4J_DATABASE` are passed through.

| Variable | Default | Notes |
| --- | --- | --- |
| `NEO4J_URI` | `bolt://localhost:7687` | host-side value; use `neo4j+s://...` for AuraDB |
| `NEO4J_USER` | `neo4j` | |
| `NEO4J_PASSWORD` | `graphwright` | also sets `NEO4J_AUTH` for the Neo4j container |
| `NEO4J_DATABASE` | `neo4j` | |
| `MCP_TRANSPORT` | `streamable-http` | or `stdio` |
| `MCP_HOST` | `127.0.0.1` | `0.0.0.0` inside the container |
| `MCP_PORT` | `8000` | |

Do not add `NEO4J_PASSWORD` as an environment variable on the **neo4j** service: the
image maps it to a `server.password` setting that does not exist, and startup fails
with "Unrecognized setting. No declared setting with name: PASSWORD". The healthcheck
interpolates `${NEO4J_PASSWORD}` instead.

## Example calls

```jsonc
// read
{ "cypher": "MATCH (p:Person)-[r:KNOWS]->(f) RETURN p.name AS who, f.name AS friend", "max_rows": 100 }

// edit — one statement, so the clauses share variable scope
{ "cypher": "MERGE (p:Person {name: $name}) "
          + "SET p.updated_at = datetime() "
          + "MERGE (c:Company {name: $company}) "
          + "MERGE (p)-[:WORKS_AT {since: date($since)}]->(c)",
  "params": { "name": "Kendra", "company": "Acme", "since": "2026-06-01" } }
```

Use `$params` instead of string interpolation; values are passed to Neo4j separately.

### Statements in a list do not share variables

A list runs in one transaction, but each statement has its own scope. This is wrong:

```jsonc
[ "MERGE (p:Person {name: $name})",
  "MERGE (p)-[:WORKS_AT]->(:Company {name: 'Acme'})" ]   // creates a bare node, not Kendra
```

The second statement's `p` is a **new, unbound variable**, so Neo4j creates an empty
label-less node instead of connecting to the first one. Either re-`MATCH` in the second
statement, or put both clauses in a single statement as shown above.

## Layout

```
src/graphwright/
├── server.py          # MCPServer, the two tools, transport selection
├── neo4j_client.py    # driver, READ vs WRITE execution, counters
└── serialization.py   # nodes/relationships/paths/temporal -> JSON
scripts/smoke_test.py  # end-to-end check over HTTP
Dockerfile             # python:3.12-slim + uv, runs the MCP server
docker-compose.yml     # Neo4j 5.26 + graphwright service
```
