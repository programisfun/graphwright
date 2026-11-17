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

## Keeping it warm

Two independent idle timers will otherwise bite you:

| Host | Idle limit | What resets it |
| --- | --- | --- |
| Render free | 15 min | any inbound HTTP request |
| AuraDB Free | 72 h | an actual **query** (`/health` does not touch the DB) |

So the server exposes two probes:

| Endpoint | Auth | Does | Use |
| --- | --- | --- | --- |
| `/health` | none | nothing — returns `{"status":"ok"}` | platform health checks |
| `/warm` | bearer token | runs `RETURN 1`, returns `db_ms` | keep-warm pinger |

One `/warm` call satisfies both timers: it is inbound traffic for Render *and* a query
for Aura.

```powershell
$t = (Get-Content .env | Select-String '^MCP_AUTH_TOKEN=').Line.Split('=',2)[1]
Invoke-WebRequest "https://graphwright-kjn8.onrender.com/warm" -Headers @{Authorization="Bearer $t"}
# {"status":"ok","db_ms":48.9,"row":[{"ok":1}]}
```

### Pinger: Cloudflare Worker (free, no card)

Exact steps, in order. Everything below was run on Windows; the same works on macOS/Linux
with `export` instead of `$env:`.

#### 1. Prerequisites

- A Cloudflare account (free, no card): <https://dash.cloudflare.com/sign-up>
- Node.js + npm (`node --version`) — `wrangler` runs through `npx`, nothing to install globally
- The Render service URL and its `MCP_AUTH_TOKEN` (from the Render dashboard, or
  `GET /v1/services/<id>/env-vars` with your Render API key)

#### 2. Log in to Cloudflare

```powershell
cd C:\Users\aron.luk\Desktop\ref\graphwright\infra
npx wrangler@latest login
```

A browser opens; click **Allow**. Verify:

```powershell
npx wrangler@latest whoami
# 👋 You are logged in with an OAuth Token, associated with the email ...
```

#### 3. Register the `workers.dev` subdomain (once per account)

`wrangler deploy` fails with *"register a workers.dev subdomain"* on a fresh account, and
the old onboarding URL now 404s. Register it through the API — replace `<account-id>` with
the one `whoami` prints:

```powershell
$acct = "<account-id>"
$token = (Select-String -Path "$HOME\AppData\Roaming\xdg.config\.wrangler\config\default.toml" -Pattern 'oauth_token\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Invoke-RestMethod -Method Put -Uri "https://api.cloudflare.com/client/v4/accounts/$acct/workers/subdomain" `
  -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" `
  -Body '{"subdomain":"aronluk-graphwright"}'
```

Pick any unique lowercase name. Your worker will appear at
`https://<worker-name>.<subdomain>.workers.dev`.

#### 4. Point the worker at your service

Edit `infra/wrangler.toml`:

```toml
[vars]
WARM_URL = "https://graphwright-kjn8.onrender.com"   # your Render URL, no trailing slash
TZ_OFFSET_HOURS = "8"    # local hour = UTC + this (8 = UTC+8, -5 = US Eastern)
START_HOUR = "8"         # only ping between these local hours
END_HOUR = "23"          # keeps usage near 450 of Render's 750 monthly instance-hours
```

#### 5. Deploy

```powershell
npx wrangler@latest deploy --config wrangler.toml
```

```
Uploaded graphwright-warm
Deployed graphwright-warm triggers
  https://graphwright-warm.<subdomain>.workers.dev
  schedule: */10 * * * *
```

#### 6. Store the token as a worker secret

Never put it in `wrangler.toml` (that file is committed). Pipe it in so it never appears
on screen or in shell history:

```powershell
$t = (Get-Content ..\.env | Select-String '^MCP_AUTH_TOKEN=').Line.Split('=',2)[1]
$t.TrimEnd() | npx wrangler@latest secret put MCP_AUTH_TOKEN --config wrangler.toml
# ✨ Success! Uploaded secret MCP_AUTH_TOKEN
```

If the Render service generated its own token, use that value instead of the local one —
the worker must authenticate to **Render**, not to your laptop.

#### 7. Test it

`*.workers.dev` sometimes fails this machine's default TLS negotiation. Force IPv4 +
TLS 1.2:

```powershell
curl.exe -4 -s --tlsv1.2 -w "`nHTTP %{http_code}`n" https://graphwright-warm.<subdomain>.workers.dev/
# {"status":"ok","db_ms":1461.3,"row":[{"ok":1}]}
# HTTP 200
```

That single call proves the whole chain: worker → Render → Aura query.

#### 8. Confirm the schedule actually fires

Scheduled events take up to ~10 minutes to start after a deploy, and `wrangler tail`
does **not** show them reliably. Check Render's logs instead (app logs, not request logs —
free plans have no request-log type):

```powershell
$key = (Get-Content ..\scripts\.env | Select-String '^RENDER_API_KEY=').Line.Split('=',2)[1]
$owner = (Invoke-RestMethod "https://api.render.com/v1/owners?limit=1" -Headers @{Authorization="Bearer $key"})[0].owner.id
Invoke-RestMethod "https://api.render.com/v1/logs?ownerId=$owner&resource=<service-id>&type=app&limit=100" `
  -Headers @{Authorization="Bearer $key"} |
  Select-Object -ExpandProperty logs |
  Where-Object { $_.message -match '/warm' } |
  ForEach-Object { "$($_.timestamp) $($_.message)" }
```

Expect a line roughly every 10 minutes inside your window:

```
2026-11-17T05:20:58Z  INFO: 10.31.154.133:0 - "GET /warm HTTP/1.1" 200 OK
```

#### 9. Change or remove it

| Task | Command |
| --- | --- |
| Different interval | edit `crons` in `wrangler.toml`, `npx wrangler deploy` |
| Rotate the token | `npx wrangler secret put MCP_AUTH_TOKEN` again, then update the client |
| Pause without deleting | `npx wrangler delete --name graphwright-warm` and redeploy to restore |
| Inspect live logs | `npx wrangler tail graphwright-warm` (requests only) |
| Check schedule via API | `GET /accounts/<id>/workers/scripts/graphwright-warm/schedules` |

#### Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `SSLV3_ALERT_HANDSHAKE_FAILURE` | local TLS/IPv6 stack, not Cloudflare | `curl -4 --tlsv1.2` |
| `Could not automatically register "infra"` | no `workers.dev` subdomain | step 3 |
| Worker returns 401 | wrong/absent secret | step 6, use the Render token |
| 503 from `/warm` | Aura unreachable or auth broken | check Render env vars; the response body carries the Neo4j error |
| No `/warm` lines in Render logs | schedule still propagating, or outside the hour window | wait 10 min; check `TZ_OFFSET_HOURS` |

Without a pinger the first call after idle still works — it just takes ~60 s while
Render wakes, which is why the client timeouts should be raised to 90 s.

## Deploying to Hugging Face Spaces

> **Blocked as of 2026:** free `cpu-basic` now covers **static Spaces only** — creating a
> Docker or Gradio Space returns `402 Payment Required` ("requires a PRO subscription").
> The script is kept for when a PRO plan exists; it works unchanged once the account can
> create Docker Spaces.

Free-ish Docker onboarding: 2 vCPU / 16 GB, sleeps after ~48 h idle, secrets injected as
env vars. Deployable entirely through the API.

```powershell
# once: write token from https://huggingface.co/settings/tokens (no card)
# add this line to scripts/.env
# HF_TOKEN=hf_...

uv run python scripts/hf_deploy.py            # create Space, upload, set secrets, poll
uv run python scripts/hf_deploy.py --status   # stage + URL
```

The script creates `<you>/graphwright` as a Docker Space, uploads the repo while
excluding every `.env`, pushes `NEO4J_*` and `MCP_AUTH_TOKEN` as **Space secrets**,
sets `MCP_HOST=0.0.0.0`, `MCP_PORT=7860`, `MCP_STATELESS=1`, writes the Space README
with `sdk: docker` front matter, and waits until the runtime reports `RUNNING`.

| | Value |
| --- | --- |
| MCP endpoint | `https://<user>-graphwright.hf.space/mcp` |
| Health | `https://<user>-graphwright.hf.space/health` |
| Visibility | public Space; the bearer token protects the graph, not the URL |
| Custom domain | PRO only |

Note the port: HF routes to **7860** by default, so `MCP_PORT=7860` (not 8000) is
required, otherwise the health check and MCP endpoint are unreachable.

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

**Known limitation:** Render rejects service creation through the API with
`402 Payment information is required`, even on the free plan. Create the service once in
the dashboard, then use this script for redeploys (deploying an existing service works).

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
