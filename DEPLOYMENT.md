# Deploying graphwright

Production topology:

| Piece | Provider | Notes |
| --- | --- | --- |
| Graph | **Neo4j AuraDB Free** | 200k nodes / 400k relationships, auto-pauses after 72 h idle |
| MCP server | **Render Free** web service | builds `Dockerfile`, sleeps after 15 min idle |
| Keep-warm pinger | **Cloudflare Worker** (`infra/`) | cron every 10 min, free plan, no card |

Everything below assumes the repo is pushed and `.env` holds the Aura credentials. The
app reads `.env` itself with `python-dotenv`, so the same code runs locally, in compose,
and on a PaaS — no compose needed on the server.

## Order of operations

Do these in order — each step produces a value the next one needs.

| # | Step | Produces | Consumed by |
| --- | --- | --- | --- |
| 1 | Create the AuraDB instance → [§1](#1-neo4j-auradb-free) | URI, user, password, database → `.env` | deploy, local runs |
| 2 | Create the Render account **and add a payment method** → [§2](#2-render-free-web-service) | workspace + API key → `scripts/.env` | the deploy script |
| 3 | Run `scripts/render_deploy.py` → [§2](#2-render-free-web-service) | service URL + Render-generated `MCP_AUTH_TOKEN` | pinger, client |
| 4 | Deploy the pinger → [§3](#3-keep-warm-pinger) | worker URL + cron | — |
| 5 | Point the client at the service → [§5](#5-client-config-for-the-deployed-service) | working `graphwright_read` / `graphwright_edit` | you |

Two prerequisites that are easy to get wrong:

- **The payment method is required before step 3**, not optional. Render rejects
  service creation through the API with `402 Payment information is required` even on the
  free plan. The card is verified, not charged, while you stay on Free.
- **The pinger cannot be set up before step 3**, because its secret is the token Render
  generates for that service. Deploy first, then copy the token.

Verification checkpoints, one per step: `.env` connects to Aura; `GET /owners` returns a
workspace; `/health` returns 200; a `/warm` line appears in Render's logs; the client
lists two tools.

## 1. Neo4j AuraDB Free

Create an instance at <https://console.neo4j.io> and copy the three connection facts:

| Field | Gotcha |
| --- | --- |
| URI | `neo4j+s://<instance-id>.databases.neo4j.io` — TLS only |
| User | Aura **often uses the instance id**, not `neo4j` |
| Database | Often the instance id too, not `neo4j` |

Get this wrong and you see `Unauthorized` (bad user) or `DatabaseNotFound` (bad database)
rather than a connection error. Verify before deploying:

```powershell
uv run python -c "import asyncio,os;from dotenv import load_dotenv;from neo4j import AsyncGraphDatabase as G;load_dotenv();d=G.driver(os.getenv('NEO4J_URI'),auth=(os.getenv('NEO4J_USER'),os.getenv('NEO4J_PASSWORD')));print(asyncio.run(d.verify_connectivity()))"
```

Limits: no backups, single zone, no vector optimisation, deleted after ~30 days paused.

## 2. Render free web service

Render does not support `docker-compose.yml` at any tier — it builds the `Dockerfile`. So
Neo4j cannot live in the same deployment.

**Add a payment method first** (<https://dashboard.render.com/billing>). On the free plan
it is verification, not a charge — but without it, API service creation fails with `402`.
The dashboard usually refuses to create a service without one as well.

### Via the API

```powershell
# 1. Create an API key: https://dashboard.render.com/u/settings#api-keys
# 2. Save it (gitignored, never in .env which may travel to a host)
Set-Content scripts\.env "RENDER_API_KEY=rnd_..." -NoNewline

# 3. Create the service if missing, then deploy and poll
uv run python scripts/render_deploy.py
uv run python scripts/render_deploy.py --status
```

Key lookup order: `RENDER_API_KEY` env var → `scripts/.env` → `scripts/.env.deploy` →
`~/.render-api-key`.

What the script does: reads `NEO4J_*` from `.env`, sets `MCP_STATELESS=1`, asks Render to
**generate** `MCP_AUTH_TOKEN` (`generateValue: true`), creates a `web_service` with
`plan: free`, `region: oregon`, `healthCheckPath: /health`, `dockerfilePath:
./Dockerfile`, triggers a deploy, and polls until `live`.

**Known limitation:** Render returned `402 Payment information is required` when creating
a service through the API on an account with no card. Once a card is on file, creation
works. If it 402s, create the service in the dashboard and use the script for redeploys.

The repo must be **public** for this path, otherwise install Render's GitHub App on the
owner first.

### Via the dashboard

| Field | Value |
| --- | --- |
| Type | Web Service → Public Git repository |
| Name | `graphwright` (the script looks it up by this name) |
| Branch | `main` |
| Runtime | **Docker** (`./Dockerfile`, build context `.`) |
| Region | Oregon (free tiers: Oregon, Ohio, Frankfurt, Singapore) |
| Instance type | **Free** |
| Health check | `/health` |
| Auto-deploy | on commit *(only with Render's GitHub App connected — see below)* |

Environment variables: the four `NEO4J_*` values from the
[configuration table](README.md#configuration), plus:

| Key | Value |
| --- | --- |
| `MCP_AUTH_TOKEN` | click **Generate** in the dashboard |
| `MCP_STATELESS` | `1` |

**Never set `MCP_PORT` on a PaaS** — the platform injects `PORT` and routes to it;
`MCP_PORT` takes precedence and would break the health check.

### Auto-deploy: only with the GitHub App

`autoDeployTrigger: commit` is a setting, not a guarantee. A service created from a
**public Git URL** has no webhook, so pushing does nothing — the deploy has to be started:

```powershell
uv run python scripts/render_deploy.py
```

To get push-to-deploy, install Render's GitHub App on the repo owner and reconnect the
service, after which the webhook drives builds. Until then, treat every push as "code in
the repo, not yet live".

### Verify the deployment

```powershell
curl.exe "https://<service>.onrender.com/health"                      # {"status":"ok"}
$t = "<MCP_AUTH_TOKEN>"
curl.exe -H "Authorization: Bearer $t" "https://<service>.onrender.com/warm"
# {"status":"ok","db_ms":171.7,"row":[{"ok":1}]}
```

`/health` is unauthenticated on purpose (platform health checks); `/warm` requires the
token because it touches Neo4j.

Render's logs: `GET /v1/logs?...&type=app&text=/warm`. Free plans have **no `request`
log type**, and `limit=100` only spans ~15 minutes because the health check logs every
10 seconds — always use the `text` filter.

### Free-plan caveats

- Sleeps after ~15 min without inbound traffic; the next call pays a ~60 s cold start.
- 512 MB RAM, 0.1 CPU, no persistent disk, no shell on the free instance.
- 750 instance-hours/month per workspace; exceeding it suspends free services.
- The URL is public. The bearer token is the only thing protecting a writable Cypher
  shell — never deploy without it.
- `NEO4J_PASSWORD` must **not** be set as an env var on a Neo4j container: the image maps
  `NEO4J_*` to `server.*` config and fails with *"Unrecognized setting… PASSWORD"*.

## 3. Keep-warm pinger

Two independent idle timers would otherwise end the service's usable life:

| Host | Idle limit | Reset by |
| --- | --- | --- |
| Render free | 15 min | any inbound HTTP request |
| AuraDB Free | 72 h | an actual **query** (`/health` does not touch the DB) |

`/warm` (bearer token) runs `RETURN 1`, so a single call resets both. `/health` stays
DB-free so platform health checks are cheap.

The worker that calls it lives in `infra/`: a Cloudflare Worker on a 10-minute cron,
restricted to 08:00-23:00 local, because 24/7 would consume ~730 of Render's 750 monthly
instance-hours. Setup, verification and troubleshooting are documented **once**, in
[`infra/README.md`](infra/README.md).

Without a pinger the service still works: the first call after idle takes ~60 s, so give
clients a 90 s startup timeout:

```jsonc
"timeout": { "startup": 90000, "catalog": 90000 }
```

## 4. Client config for the deployed service

Same block as the [client config in README.md](README.md#client-config), with two
substitutions:

- `url` → `https://<service>.onrender.com/mcp`
- `Authorization` → the Render-generated token, preferably via
  `"Bearer {env:GRAPHWRIGHT_TOKEN}"` so the secret stays out of the file

## 5. Secrets and rotation

| Secret | Where it lives | Can it be read back? |
| --- | --- | --- |
| `MCP_AUTH_TOKEN` (Render) | Render env var, generated by Render | **yes** — `GET /v1/services/{id}/env-vars` |
| `MCP_AUTH_TOKEN` (local) | `.env`, generated with `secrets.token_urlsafe` | yes, it is a file |
| Worker copy | Cloudflare worker secret | **no** — write-only |
| `RENDER_API_KEY` | `scripts/.env` (gitignored) | it *is* the reader; treat as root |

A Render API key can read every env var and deploy arbitrary code, so it is more powerful
than the MCP token. It never belongs in the repo, `.env`, or a chat.

Rotating the MCP token: Render dashboard → Environment → replace → redeploy →
`wrangler secret put MCP_AUTH_TOKEN` again → update client configs. The two environments
(local, Render) have independent tokens; rotate only what leaked.

## 6. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `401 Unauthorized` on `/mcp` | client token ≠ server token | local and Render tokens are different |
| `503` on `/warm` | Aura unreachable or bad credentials | response body carries the Neo4j error |
| `502` for ~60 s then fine | Render cold start | expected after 15 min idle |
| `402` from Render API | account has no payment method | add a card or create via dashboard |
| `Unauthorized` from Aura | wrong `NEO4J_USER` | Aura often uses the instance id |
| `DatabaseNotFound` | wrong `NEO4J_DATABASE` | same — often the instance id |
| Deploy hangs at *"dependency failed to start"* | `depends_on` waits on a service you removed | drop the dependency or restore the service |

Worker-specific symptoms (TLS handshake to `*.workers.dev`, 401/503 from the pinger, no
`/warm` lines in Render's logs) are covered in
[`infra/README.md`](infra/README.md#troubleshooting).

## 7. Deployment checklist

- [ ] Aura instance reachable with `.env` values (`verify_connectivity`)
- [ ] `MCP_AUTH_TOKEN` set on the host (server refuses to start without it)
- [ ] `MCP_STATELESS=1` on any scale-to-zero host
- [ ] `MCP_PORT` **not** set on a PaaS
- [ ] `/health` returns 200 over the public URL
- [ ] `/warm` returns 200 **with** the token, 401 without
- [ ] MCP client configured with the remote URL plus the header
- [ ] Pinger deployed; a `/warm` line appears in Render logs within 15 min
