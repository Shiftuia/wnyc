# Deploying Wheelchair NYC on your own server

Written so it can be handed to a person or a coding agent as-is. Target: one Linux VM, the app behind a reverse
proxy with HTTPS, a Claude API key on the server, open to the public.

Two ways: **A. Docker Compose** (recommended: the app isolated behind an ingress and an egress sidecar, your reverse proxy in front) or
**B. plain systemd** (sections 1–3 below). Sections "Settings", "Security notes" and "Done when" apply to both.

## A. Docker Compose (isolated stack; how wnyc.shiftuia.com runs)

Needs Docker with the Compose plugin, ~2 GB RAM for the one-off data build (the app itself is capped at 1.5 GB and
one CPU), ~3 GB disk, and a reverse proxy that can reach the `wnyc-ingress` container. On the ThinkCentre that is
Traefik on the shared `proxy` network, reached from the internet through the Cloudflare tunnel.

```
visitor ─ Cloudflare ─ cloudflared ─ traefik ──proxy net──> wnyc-ingress (nginx, :8080)
                                                               │  wnyc-internal (internal, no host IP)
                                                               v
                                                     wnyc (app, :8765) ──> wnyc-egress (squid) ──> api.anthropic.com
                                                                                                   nominatim.openstreetmap.org
                                                                                                   routing.openstreetmap.de
```

| Container | Networks | What it may reach |
|---|---|---|
| `wnyc` (app) | `wnyc-internal` only | the two sidecars; nothing else (no default route, no DNS for outside names) |
| `wnyc-ingress` (nginx) | `wnyc-internal`, `proxy` | forwards to `wnyc:8765` only; sets `X-WNYC-Client` from `CF-Connecting-IP`; per-visitor rate limits |
| `wnyc-egress` (squid) | `wnyc-internal`, `wnyc-out` | HTTPS `CONNECT` to the three hosts above; private, loopback and link-local destinations refused |
| `bootstrap` (one-shot) | `wnyc-bootstrap` | the public dataset hosts (`scripts/fetch*.sh`); never runs with visitor input |

`wnyc-internal` is `internal: true` with `com.docker.network.bridge.inhibit_ipv4`, so the host has no address on
that bridge: the app cannot reach the host (sshd, Traefik's published ports), the LAN, other services or
169.254.169.254, and Traefik is not on that network either (a container next to Traefik could otherwise ask Traefik
for any other backend). No privileged host change is needed for this.

All three long-running containers: read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, non-root,
CPU/memory/pids limits, `restart: unless-stopped`, no published ports, no host bind mounts, no Docker socket.

### First start

```bash
git clone https://github.com/Shiftuia/wnyc.git && cd wnyc
cp .env.example .env && chmod 600 .env          # put ANTHROPIC_API_KEY in it (or leave empty: fixed-rules mode)
docker compose --profile bootstrap run --rm bootstrap     # downloads ~0.7 GB, builds data/prepared.duckdb (~10 min)
GIT_REVISION=$(git rev-parse HEAD) docker compose up -d --build
docker exec -i wnyc .venv/bin/python - < scripts/isolation_probe.py     # must end with ALL PASS
```

`docker compose ps` shows `wnyc` healthy once `/health` (database opens, 263 zones) answers 200.

### Data, weights and volumes

| Volume | Mounted | Contents | Backup |
|---|---|---|---|
| `wnyc-data` | app: read-only `/app/data`; bootstrap: read-write | downloaded datasets (TLC trips, MTA, NYC Open Data, Zillow, CDC), `prepared.duckdb`, `zones.geojson`, offline street map (`basemap/`), `MANIFEST.sha256` | not needed: rebuilt by the bootstrap |
| `wnyc-cache` | app: read-write `/cache` | cached answers (`run_*.json`, contain the visitor's question), map PNGs, geocode and route lookups; pruned to 5,000 answers / 2,000 maps / 5,000 lookups | optional; safe to delete |

There are no model weights or embeddings: the model is Claude via the API (`claude-haiku-5-5`). The DuckDB
`spatial` extension and the matplotlib font cache are baked into the image at build time.

- Integrity check: `docker compose --profile bootstrap run --rm bootstrap verify` (sha256 of every file the
  server reads against `MANIFEST.sha256`).
- Refresh the live feeds (elevator outages, lotteries) and rebuild: `docker compose --profile bootstrap run --rm
  bootstrap refresh && docker compose restart wnyc`.
- Redeploy after a code change: `git pull && GIT_REVISION=$(git rev-parse HEAD) docker compose up -d --build`.
  The image carries `org.opencontainers.image.revision`; `/health` reports the same value.
- Release switch: `WNYC_PUBLIC=true` in `.env`, then `docker compose up -d wnyc-ingress` (Traefik picks up the
  router `Host(wnyc.shiftuia.com)`). The Cloudflare side (tunnel ingress + DNS) lives in the services repo
  `_maintenance/cloudflared`.

## B. Plain server (systemd)

### What you need

- Linux (tested shape: Ubuntu 22.04/24.04), **Python 3.11+** with `venv`, `curl`, `unzip`, `git`.
  On Debian/Ubuntu: `sudo apt install -y python3 python3-venv python3-pip curl unzip git`
- **2 GB RAM** (the one-off data build reads a 0.5 GB Parquet file), **~2 GB free disk**
  (data ≈ 0.75 GB, Python packages ≈ 0.55 GB).
- Outbound internet: the first run downloads public datasets (NYC TLC, MTA, NYC Open Data, Zillow, CDC, OpenFreeMap).
  At runtime the server calls the Claude API, Nominatim (address lookup) and routing.openstreetmap.de.
- A domain name pointing at the server (for HTTPS).
- An Anthropic API key. **Set a monthly spend limit on it in the Claude Console before going public.**

### 1. Get the code and build the data (once, ~5–10 min)

```bash
sudo useradd --system --create-home --home-dir /opt/wnyc wnyc      # optional: dedicated user
sudo -u wnyc git clone https://github.com/Shiftuia/wnyc.git /opt/wnyc/app
cd /opt/wnyc/app
sudo -u wnyc bash -c 'umask 077; echo "ANTHROPIC_API_KEY=sk-ant-..." > .env'   # never commit this file (.gitignored)
sudo -u wnyc ./run.sh
```

`run.sh` creates `.venv`, installs `requirements.txt`, downloads everything into `data/`, builds
`data/prepared.duckdb` (it prints row counts and checks; a few `FAIL` lines from optional enrichment downloads are
tolerated) and then starts the server on `127.0.0.1:8765`. When you see it running, stop it with Ctrl+C:
from now on systemd runs it.

Quick check while it runs (from another shell on the server):

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/            # 200
curl -s http://127.0.0.1:8765/api/bootstrap | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["model"], d["live"])'
# → claude-haiku-5-5 True      (False means the key was not found: check .env)
```

### 2. Run it as a service

`/etc/systemd/system/wnyc.service`:

```ini
[Unit]
Description=Wheelchair NYC
After=network-online.target
Wants=network-online.target

[Service]
User=wnyc
WorkingDirectory=/opt/wnyc/app
ExecStart=/opt/wnyc/app/.venv/bin/python app/server.py
Environment=HOST=127.0.0.1
Environment=PORT=8765
# optional overrides, see the table below
# Environment=WNYC_MODEL=claude-haiku-5-5
# Environment=WNYC_ASK_PER_IP_HOUR=20
# Environment=WNYC_ASK_PER_DAY=2000
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now wnyc
sudo systemctl status wnyc            # logs: journalctl -u wnyc -f
```

The key is read from `.env` in the app folder (an environment variable `ANTHROPIC_API_KEY` wins over `.env`).
The service user needs write access to `data/cache/` (cached answers and map images) and `data/basemap/`
(street-map tiles outside NYC are fetched once and kept).

### 3. HTTPS reverse proxy

Keep the app on `127.0.0.1`; expose only the proxy. For per-visitor limits the proxy must set `X-WNYC-Client` to
the real client address (replacing anything the client sent) and the service must run with
`Environment=WNYC_TRUST_CLIENT_HEADER=1`. `X-Forwarded-For` is ignored.

**Caddy** (simplest, automatic certificates) — `/etc/caddy/Caddyfile`:

```
wnyc.example.com {
    encode gzip
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1          # answers are streamed line by line
        header_up X-WNYC-Client {remote_host}
    }
}
```

**nginx** alternative:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-WNYC-Client $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;           # streamed answers
    proxy_read_timeout 180s;       # one answer can take up to ~1–2 min
}
```

Open ports 80/443 only (`sudo ufw allow 80,443/tcp`), not 8765.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Enables the model. Without it the app still works and answers with fixed rules (labelled in the UI). |
| `WNYC_MODEL` | `claude-haiku-5-5` | Model that writes the answer. `claude-fable-5-2` runs more of its own checks, is slower and costs ~80× more per token. |
| `WNYC_EFFORT` | `medium` | `low` / `medium` / `high` … |
| `WNYC_ASK_PER_IP_HOUR` | `20` | Model-answered questions per visitor IP per hour. |
| `WNYC_ASK_PER_DAY` | `2000` | Model-answered questions per day for the whole site (resets at server-local midnight and on restart). |
| `WNYC_ASK_CONCURRENCY` | `2` | Questions answered at the same time; more get a "busy" reply. |
| `WNYC_LOOKUPS_PER_IP_HOUR` | `120` | Address lookups and trip checks per visitor per hour (they call public OSM services). |
| `WNYC_MAX_TURNS` / `WNYC_MAX_TOKENS_PER_TURN` / `WNYC_MAX_OUTPUT_TOKENS` / `WNYC_MAX_INPUT_TOKENS` | `8` / `8000` / `40000` / `400000` | Per-question model budget. |
| `WNYC_SQL_TIMEOUT_S` | `5` | Time limit for one model-written SQL query. |
| `WNYC_TRUST_CLIENT_HEADER` | off | `1` = take the visitor key from `X-WNYC-Client` (set by your proxy). |
| `WNYC_CACHE_DIR` | `data/cache` | Writable cache directory (answers, maps, lookups). |
| `WNYC_BASEMAP_UPSTREAM` | `1` | `0` = never fetch missing map tiles from OpenFreeMap. |
| `HOST` / `PORT` | `127.0.0.1` / `8765` | Where the app listens. |

Limits only count questions that actually reach the model; repeated questions are served from `data/cache/`, and
the map, budget dial and trip check never call the model. Rough cost on Haiku: about a cent per fresh question.

## Security notes (public repo, public site)

Everything a visitor sends (the question, addresses, trip points) and everything the model writes is untrusted.

- **Key**: only in `.env` on the server (mode 600, gitignored, excluded from the image). The app reads it once at
  start and removes it from its own process environment; it is never sent to the browser or logged.
- **Model-written SQL** (`engine.run_sql`): one statement; `SELECT`/`WITH`/`DESCRIBE`/`SUMMARIZE` only; a
  keyword/function denylist on the text and on DuckDB's own parse tree (`json_serialize_sql`); every table must be
  one of the prepared tables (or a CTE); table functions limited to `range`/`generate_series`/`unnest`; no string
  literals that look like paths or URLs. The connection itself is read-only with external access disabled,
  512 MB memory, 1 thread, no spilling, and `lock_configuration`. Each query is interrupted after 5 s; results
  are capped at 50 rows, 300 characters per cell and 20 KB in total.
- **Agent budget per question**: 8 turns, 8,000 output tokens per turn, 40,000 output / 400,000 input tokens in
  total, 14 tool calls (per tool: geocode 3, score_zones 3, run_sql 6, render_map 2, plan_trip 2). The model has
  exactly five tools; a call to anything else (e.g. `bash` after a prompt injection) is rejected, never run.
  Over budget the answer falls back to the fixed-rules path, labelled in the UI.
- **Abuse limits**: questions 20 per visitor per hour and 2,000 per day for the site, at most 2 answered at once;
  geocode/trip lookups 120 per visitor per hour; nginx adds per-visitor request rates and connection caps;
  request bodies over 8 KB are refused; questions over 1,000 characters are rejected. The visitor key is
  Cloudflare's `CF-Connecting-IP` set by the ingress sidecar; `X-Forwarded-For` is never trusted.
- **Browser**: model output and dataset text are HTML-escaped before rendering (`esc()` in `map.js`, the answer
  renderer `md()` escapes first and only adds `<strong>`/lists); a Content-Security-Policy allows scripts only from
  this origin and the Umami host (no inline script), no plugins, no framing.
- **Files/URLs**: visitors cannot name a file or a URL; static, tile, font and sprite paths are validated, tile
  coordinates bounded, and no tiles are fetched upstream (`WNYC_BASEMAP_UPSTREAM=0`).
- **Logs**: no access logs in nginx or uvicorn (request lines carry addresses); squid logs host and status only.
- **Analytics**: Umami with `data-exclude-search` / `data-exclude-hash`, so `?q=<question>` never reaches it;
  the only custom event is the Support link click (`support-click`, placement `wnyc-header`).

Residual risks (no claim of immunity): prompt injection can still make the model write a wrong, odd or off-topic
answer, or spend its whole per-question budget; the egress proxy allows those three hosts for any purpose, so a
full code-execution bug in the app could still talk to them (but to nothing else); cached answers hold visitor
questions in `wnyc-cache`; requests that reach Traefik from the LAN can set their own `CF-Connecting-IP`; and the
per-visitor limits are in memory and reset on restart. Set a spend limit on the key in the Claude Console.

### Updating (systemd)

```bash
cd /opt/wnyc/app && sudo -u wnyc git pull && sudo systemctl restart wnyc
```

Refresh data (new month of trips, current elevator outages): delete what you want rebuilt and run the
build step again, e.g. `rm -rf data/extra data/prepared.duckdb && sudo -u wnyc ./run.sh` (Ctrl+C once it starts
serving), then `sudo systemctl restart wnyc`. Cached answers in `data/cache/run_*.json` can be deleted at any time.

## Done when

- `https://<domain>/` shows the landing page and `/map` the map with streets.
- Asking "I use a wheelchair, my budget is $3,500, I work at 350 5th Ave" gives a written answer in ~20 s, and
  About shows the model name.
- `curl https://<domain>/classic` → 404 (removed), and port 8765 is not reachable from outside.
