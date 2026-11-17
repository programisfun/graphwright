# graphwright-warm

Keep-warm pinger for the [graphwright](https://github.com/programisfun/graphwright) MCP
service. Lives in its own repo because it is a separate deployment with its own
credential, schedule and hosting provider (Cloudflare Workers, not Render).

One `GET /warm` every 10 minutes does two jobs:

| Timer | Idle limit | Reset by |
| --- | --- | --- |
| Render free web service | 15 min | any inbound HTTP request |
| Neo4j AuraDB Free | 72 h | an actual query (`/health` does not touch the DB) |

`graphwright`'s `/warm` endpoint runs `RETURN 1`, so a single call satisfies both. It is
bearer-token protected; `/health` is the unauthenticated platform probe.

## Why a separate repo

- Different host (Cloudflare vs Render), different deploy command, different secret store.
- `wrangler.toml` holds the Render URL, so it changes when the service is renamed.
- The main repo doesn't need `wrangler` state, Node tooling, or a Cloudflare account.

## Setup

Everything below was run on Windows; on macOS/Linux use `export` instead of `$env:`.

### 1. Prerequisites

- Cloudflare account (free, no card): <https://dash.cloudflare.com/sign-up>
- Node.js + npm (`node --version`); `wrangler` runs via `npx`, nothing global
- The Render service URL and its `MCP_AUTH_TOKEN` (Render dashboard â†’ Environment, or
  `GET /v1/services/<id>/env-vars` with a Render API key)

### 2. Log in

```powershell
npx wrangler@latest login     # browser opens, click Allow
npx wrangler@latest whoami    # prints email + account id
```

### 3. Register the `workers.dev` subdomain (once per account)

`wrangler deploy` fails on a fresh account with *"register a workers.dev subdomain"*, and
the old onboarding URL now 404s. Use the API â€” `<account-id>` comes from `whoami`:

```powershell
$acct = "<account-id>"
$token = (Select-String -Path "$HOME\AppData\Roaming\xdg.config\.wrangler\config\default.toml" -Pattern 'oauth_token\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Invoke-RestMethod -Method Put `
  -Uri "https://api.cloudflare.com/client/v4/accounts/$acct/workers/subdomain" `
  -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" `
  -Body '{"subdomain":"aronluk-graphwright"}'
```

The worker then lives at `https://<worker-name>.<subdomain>.workers.dev`.

### 4. Configure

`wrangler.toml`:

```toml
[vars]
WARM_URL = "https://graphwright-kjn8.onrender.com"   # no trailing slash
TZ_OFFSET_HOURS = "8"    # local hour = UTC + this (8 = UTC+8, -5 = US Eastern)
START_HOUR = "8"
END_HOUR = "23"          # 15 h/day â‰ˆ 450 of Render's 750 monthly instance-hours
```

24/7 pinging would consume ~730 of the 750 hours and leave no headroom for deploys or a
second free service, hence the window.

### 5. Deploy

```powershell
npx wrangler@latest deploy --config wrangler.toml
```

```
Uploaded graphwright-warm
Deployed graphwright-warm triggers
  https://graphwright-warm.<subdomain>.workers.dev
  schedule: */10 * * * *
```

### 6. Store the token as a secret

Never in `wrangler.toml` (it is committed). Pipe it in so it never appears on screen:

```powershell
# value must be the token the WORKER uses to call Render
$t = (Get-Content ..\.env | Select-String '^MCP_AUTH_TOKEN=').Line.Split('=',2)[1]
$t.TrimEnd() | npx wrangler@latest secret put MCP_AUTH_TOKEN --config wrangler.toml
# âœ¨ Success! Uploaded secret MCP_AUTH_TOKEN
```

If Render generated its own token, use that one â€” the worker authenticates to Render, not
to your laptop.

### 7. Test the chain

`*.workers.dev` sometimes fails default TLS negotiation from some machines. Force IPv4 +
TLS 1.2:

```powershell
curl.exe -4 -s --tlsv1.2 -w "`nHTTP %{http_code}`n" https://graphwright-warm.<subdomain>.workers.dev/
# {"status":"ok","db_ms":1461.3,"row":[{"ok":1}]}
# HTTP 200
```

A 200 with `db_ms` proves worker â†’ Render â†’ Aura.

### 8. Confirm the schedule fires

Scheduled events start up to ~10 minutes after a deploy (Cloudflare documents up to 15), and
`wrangler tail` does not show them reliably. Two ways to check:

**Dashboard (simplest):** Workers & Pages -> `graphwright-warm` -> **Settings** ->
**Trigger Events** -> *View events*. It keeps the last 100 cron invocations. On a new
worker, events can take ~30 minutes to appear.

**Render app logs:** free plans have no `request` log type, so query `type=app` and match
the text.

```powershell
$key = (Get-Content ..\scripts\.env | Select-String '^RENDER_API_KEY=').Line.Split('=',2)[1]
$owner = (Invoke-RestMethod "https://api.render.com/v1/owners?limit=1" -Headers @{Authorization="Bearer $key"})[0].owner.id
Invoke-RestMethod "https://api.render.com/v1/logs?ownerId=$owner&resource=<service-id>&type=app&limit=100" `
  -Headers @{Authorization="Bearer $key"} |
  Select-Object -ExpandProperty logs |
  Where-Object { $_.message -match '/warm' } |
  ForEach-Object { "$($_.timestamp) $($_.message)" }
```

Expected, roughly every 10 minutes inside the window:

```
2026-11-17T05:20:58Z  INFO: 10.31.154.133:0 - "GET /warm HTTP/1.1" 200 OK
```

## Operations

| Task | Command |
| --- | --- |
| Different interval | edit `crons` in `wrangler.toml`, redeploy |
| Rotate token | `wrangler secret put MCP_AUTH_TOKEN`, update clients |
| Inspect live requests | `npx wrangler tail graphwright-warm` |
| Check schedule | `GET /accounts/<id>/workers/scripts/graphwright-warm/schedules` |
| Remove | `npx wrangler delete --name graphwright-warm` |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `SSLV3_ALERT_HANDSHAKE_FAILURE` | local TLS/IPv6 stack | `curl -4 --tlsv1.2` |
| `Could not automatically register "infra"` | no `workers.dev` subdomain | step 3 |
| 401 from `/warm` | wrong or missing secret | step 6, use the Render token |
| 503 from `/warm` | Aura unreachable / auth broken | response body carries the Neo4j error |
| No `/warm` lines in Render logs | schedule propagating, or outside the hour window | wait 10 min, check `TZ_OFFSET_HOURS` |

## Notes

- Free plan: 100k requests/day; this uses ~1.4k.
- The worker also exposes a `fetch` handler so you can trigger one ping by opening the
  URL. It is public â€” anyone can hit it â€” but the token stays server-side, so the worst
  case is one extra `RETURN 1`. Delete the `fetch` export if you want only the cron.
- Without any pinger the service still works; the first call after idle just takes ~60 s.
