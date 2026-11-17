# graphwright

An MCP server that lets an AI client **read and edit a Neo4j graph with plain Cypher**.
Two tools, no abstractions, no schema to learn:

| Tool | Access mode | What it does |
| --- | --- | --- |
| `read` | `READ` | Runs one Cypher query, returns rows as JSON. Writes are rejected by Neo4j itself. |
| `edit` | `WRITE` | Runs any Cypher — `CREATE`, `MERGE`, `SET`, `DELETE`, `DETACH DELETE`, index/constraint DDL. Accepts a list to run several statements in one transaction. |

Deployment (AuraDB + Render + keep-warm pinger) is documented separately in
[`DEPLOYMENT.md`](DEPLOYMENT.md).

## ⚠️ Trust model

There is **no query inspection and no dry run**. The client is expected to be an LLM and is
trusted to do destructive things — that is the point. The only guardrail is on `read`,
which uses Neo4j's `READ` access mode so writes fail.

A single admin token (`MCP_AUTH_TOKEN`, required) gates the HTTP endpoint. Anyone holding
it can run `DETACH DELETE` or `DROP CONSTRAINT`, so treat it like a database shell
credential and keep the endpoint on loopback unless it is behind HTTPS plus that token.

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
Relationship types drift more than labels in practice, because both spellings read
naturally to a model.

## Run

```powershell
docker compose up -d          # graphwright only; Neo4j is external (AuraDB)
uv run python scripts/smoke_test.py
```

| Service | Address | Notes |
| --- | --- | --- |
| `graphwright-mcp` | <http://127.0.0.1:8000/mcp> | MCP over Streamable HTTP, `restart: unless-stopped` |
| Neo4j | from `NEO4J_URI` | AuraDB Free (`neo4j+s://...`), or a local instance you run yourself |

For development, run the server from source instead of the container:

```powershell
uv sync
uv run graphwright            # same endpoint, 127.0.0.1:8000
$env:MCP_TRANSPORT='stdio'; uv run graphwright    # stdio instead of HTTP
docker compose build graphwright && docker compose up -d   # rebuild after code changes
```

`docker-compose.yml` runs the MCP server only. Neo4j is external because one `.env` cannot
hold both a local container password and the Aura credentials; restore the bundled service
from git history if you want it back:

```powershell
git show 702e1a8:docker-compose.yml    # the version with the neo4j service
```

## Client config

Point any MCP client at the endpoint and send the token:

```jsonc
{
  "mcp": {
    "servers": {
      "graphwright": {
        "type": "remote",
        "url": "http://127.0.0.1:8000/mcp",
        "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
      }
    }
  }
}
```

Tools then appear as `graphwright_read` and `graphwright_edit`. The header is not optional:
without it every call returns 401, and there is no unauthenticated mode.

## Configuration

Settings come from `.env` (gitignored; copy `.env.example`). The app loads it with
`python-dotenv`, so the same file works for `uv run graphwright` and for the container
(compose passes it as `env_file`). The Neo4j URI is used verbatim inside and outside the
container.

| Variable | Default | Notes |
| --- | --- | --- |
| `NEO4J_URI` | `bolt://localhost:7687` | use `neo4j+s://...` for AuraDB |
| `NEO4J_USER` | `neo4j` | Aura instances often use the instance id |
| `NEO4J_PASSWORD` | — | Aura password |
| `NEO4J_DATABASE` | `neo4j` | Aura instances often name it after the instance id |
| `MCP_TRANSPORT` | `streamable-http` | or `stdio` |
| `MCP_HOST` | `127.0.0.1` | `0.0.0.0` inside the container, or when `PORT` is set |
| `MCP_PORT` | `8000` | falls back to `PORT` (PaaS convention) if unset |
| `MCP_AUTH_TOKEN` | — | **required**; generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `MCP_STATELESS` | off | `1` makes each request self-contained; set it on scale-to-zero hosts |

`MCP_PORT` takes precedence over `PORT`, so **do not set `MCP_PORT` on a PaaS** — the
platform routes traffic to `PORT`.

### Authentication model

One role only: **admin**. A single shared token grants every tool, including `edit` and
therefore `DETACH DELETE`. There are no scopes, no per-user identity and no audit trail.

- **Required, everywhere.** Local, container, Render — no exceptions. Without it the server
  exits at startup: no loopback exemption, no opt-in flag.
- Compared with `hmac.compare_digest` (constant time).
- `/health` is exempt so platform health checks work; `/warm` is not.
- Tokens shorter than 16 characters only produce a warning.

### Stateless mode

By default the MCP SDK keeps a session per connection in memory. That breaks on any host
that scales to zero or redeploys: the instance holding the session is gone, and the next
request may land elsewhere. `MCP_STATELESS=1` makes each POST self-contained, so a
spin-down, a redeploy, or a second instance is harmless. Compose and `render.yaml` set it.

The cost: no server-initiated notifications, no elicitation, no stream resumption. For two
request/response tools that is not a loss; it becomes one the moment you want progress
bars or confirmation prompts.

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
├── server.py          # MCPServer, the two tools, /health and /warm, transport selection
├── neo4j_client.py    # driver, READ vs WRITE execution, counters
└── serialization.py   # nodes/relationships/paths/temporal -> JSON
scripts/
├── smoke_test.py      # end-to-end check over HTTP
├── render_deploy.py   # create/deploy the Render service via API
└── hf_deploy.py       # Hugging Face Space deploy (blocked: Docker Spaces need PRO)
infra/
├── warm.js            # Cloudflare Worker keep-warm pinger
├── wrangler.toml      # cron, Render URL, local hour window
└── README.md          # pinger runbook
Dockerfile             # python:3.12-slim + uv, runs the MCP server
docker-compose.yml     # graphwright service only (Neo4j is external)
DEPLOYMENT.md          # Aura + Render + pinger, step by step
```
