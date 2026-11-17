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

| Variable | Default | Notes |
| --- | --- | --- |
| `NEO4J_URI` | `bolt://localhost:7687` | |
| `NEO4J_USER` | `neo4j` | |
| `NEO4J_PASSWORD` | `graphwright` | matches `docker-compose.yml` |
| `NEO4J_DATABASE` | `neo4j` | |
| `MCP_TRANSPORT` | `streamable-http` | or `stdio` |
| `MCP_HOST` | `127.0.0.1` | keep on loopback |
| `MCP_PORT` | `8000` | |

## Example calls

```jsonc
// read
{ "cypher": "MATCH (p:Person)-[r:KNOWS]->(f) RETURN p.name AS who, f.name AS friend", "max_rows": 100 }

// edit (atomic: all statements or none)
{ "cypher": [
    "MERGE (p:Person {name: $name}) SET p.updated_at = datetime()",
    "MERGE (c:Company {name: $company})",
    "MERGE (p)-[:WORKS_AT {since: date($since)}]->(c)"
  ],
  "params": { "name": "Kendra", "company": "Acme", "since": "2026-06-01" } }
```

Use `$params` instead of string interpolation; values are passed to Neo4j separately.

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
