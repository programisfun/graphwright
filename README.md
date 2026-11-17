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

Neo4j is no longer a service in `docker-compose.yml`, because one `.env` cannot hold
both the local container password and the Aura credentials. Restore the bundled
service from git history if you want it back:

```powershell
git show 702e1a8:docker-compose.yml    # the version with the neo4j service
```

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
        "url": "http://127.0.0.1:8000/mcp",
        "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
      }
    }
  }
}
```

Tools then appear as `graphwright_read` and `graphwright_edit`. The header is not
optional once `MCP_AUTH_TOKEN` is set — without it every call returns 401.

## Configuration

Settings come from `.env` (gitignored; copy `.env.example`). The app loads it with
`python-dotenv`, so the same file works for `uv run graphwright` and for the container
(compose passes it as `env_file`). Neo4j is external (AuraDB); the URI in `.env` is used
verbatim inside and outside the container.

| Variable | Default | Notes |
| --- | --- | --- |
| `NEO4J_URI` | `bolt://localhost:7687` | use `neo4j+s://...` for AuraDB |
| `NEO4J_USER` | `neo4j` | Aura instances often use the instance id |
| `NEO4J_PASSWORD` | — | Aura password |
| `NEO4J_DATABASE` | `neo4j` | Aura instances often name it after the instance id |
| `MCP_TRANSPORT` | `streamable-http` | or `stdio` |
| `MCP_HOST` | `127.0.0.1` | `0.0.0.0` inside the container, or when `PORT` is set |
| `MCP_PORT` | `8000` | falls back to `PORT` (PaaS convention) if unset |
| `MCP_AUTH_TOKEN` | — | **required**: admin token; every request needs `Authorization: Bearer <token>` and all tools are allowed |
| `MCP_STATELESS` | off | `1` makes every request self-contained (no in-memory MCP session). Set it on scale-to-zero hosts; compose sets it to mirror production |

### Stateless mode

By default the MCP SDK keeps a session per connection in memory. That breaks on any
host that scales to zero or redeploys: the instance holding the session is gone, and
the next request may land elsewhere. `MCP_STATELESS=1` makes each POST self-contained,
so a spin-down, redeploy, or a second instance is harmless.

Verified locally with the flag on: `graphwright: http://0.0.0.0:8000/mcp (bearer token, stateless)`,
and `read` / `edit` round-trips still work. Compose and `render.yaml` both set it.

### Authentication model

One role only: **admin**. A single shared token (`MCP_AUTH_TOKEN`) grants every tool,
including `edit` and therefore `DETACH DELETE`. There are no scopes, no per-user
identity and no audit trail.

- **Required, everywhere.** Local, container, Render — no exceptions. Without it the
  server exits at startup: no loopback exemption, no opt-out flag.
- Compared with `hmac.compare_digest` (constant time).
- `/health` is exempt so platform health checks work.
- Tokens shorter than 16 characters only produce a warning; generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Rotating the token = edit `.env`, `docker compose up -d --force-recreate graphwright`,
  and update every client config.

`MCP_PORT` takes precedence over `PORT`, so **do not set `MCP_PORT` on a PaaS** — the
platform routes traffic to `PORT`.

## Deploying to Render (free plan)

Render does not support `docker-compose.yml` — it builds the `Dockerfile` instead, so
Neo4j cannot live in the same deployment. Pair it with **Neo4j AuraDB Free**:

| | Where |
| --- | --- |
| Neo4j | AuraDB Free (200k nodes / 400k relationships, auto-pauses after 72h idle) |
| graphwright | Render free web service, built from `Dockerfile` |

### Via the API (no dashboard clicking)

```powershell
# 1. Create an API key: https://dashboard.render.com/u/settings#api-keys
# 2. Save it next to the script (gitignored), or in $HOME\.render-api-key
Set-Content scripts\.env.deploy "RENDER_API_KEY=rnd_..." -NoNewline

# 3. Create the service and deploy
uv run python scripts/render_deploy.py
```

Key lookup order: `RENDER_API_KEY` env var → `scripts/.env.deploy` → `~/.render-api-key`.
Keep it out of `.env`: that file holds app config you may copy to a host, while a Render
key can create or delete every service in the account.

The script reads `NEO4J_*` from `.env`, lets Render generate `MCP_AUTH_TOKEN`, creates
the service if it does not exist (`plan: free`, `region: oregon`, health check
`/health`), triggers a deploy, and polls until it goes live. `--status` prints the
current service without deploying.

The repo must be public for this path (it is), otherwise Render needs the GitHub App
installed on the owner first.

### Via the dashboard

1. Create an AuraDB Free instance, note the `neo4j+s://...` URI, user and database
   (Aura often uses the instance id for both, not `neo4j`).
2. Push this repo, then apply `render.yaml` (Blueprint) or create a Docker web service.
3. Set `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` and a strong
   `MCP_AUTH_TOKEN` in the dashboard. `MCP_STATELESS=1` is set by the blueprint.
4. Client config points at the public URL with the token:

```jsonc
{
  "mcp": {
    "servers": {
      "graphwright": {
        "type": "remote",
        "url": "https://<service>.onrender.com/mcp",
        "headers": { "Authorization": "Bearer {env:GRAPHWRIGHT_TOKEN}" }
      }
    }
  }
}
```

Free-plan caveats worth knowing before you rely on it:

- The service **sleeps after ~15 minutes idle**; the next MCP call pays a cold start
  (~30-60 s, longer for the first build).
- AuraDB Free **auto-pauses after 72h of inactivity**, and the two sleep timers are
  independent — the first request after a quiet week may wait on both.
- 512 MB RAM, no persistent disk, no shell access to the free instance.
- **The URL is public.** Without `MCP_AUTH_TOKEN` anyone who finds it has a Cypher
  shell on your graph. `render.yaml` generates a token for you; copy it from the
  dashboard into your client config.
- `/health` is intentionally unauthenticated so Render's health check passes.

The app reads `.env` itself (`python-dotenv`), so the same code runs locally, in
compose, and on a PaaS with environment variables — no compose needed on the server.

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
