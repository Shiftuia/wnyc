# Deploying Wheelchair NYC on your own server

Written so it can be handed to a person or a coding agent as-is. Target: one Linux VM, the app behind a reverse
proxy with HTTPS, a Claude API key on the server, open to the public.

Two ways: **A. Docker Compose** (recommended: app + Caddy with automatic HTTPS in one command) or
**B. plain systemd** (sections 1–3 below). Sections "Settings", "Security notes" and "Done when" apply to both.

## A. Docker Compose

Needs Docker with the Compose plugin, 2 GB RAM, ~3 GB disk, ports 80/443 open, a DNS record for your domain
pointing at the server.

```bash
git clone https://github.com/Shiftuia/wnyc.git && cd wnyc
umask 077 && cat > .env <<'ENV'
ANTHROPIC_API_KEY=sk-ant-...
DOMAIN=wnyc.example.com
ENV
docker compose up -d --build
docker compose logs -f app        # first start: downloads ~0.7 GB and builds the data, 5–10 min
```

The app is ready when the log shows the build checks and goes quiet; `docker compose ps` turns `healthy`.
Caddy gets the certificate by itself. Data lives in the `wnyc-data` volume and survives rebuilds.
Settings from the table below go into the same `.env` (compose passes them through).

- Update: `git pull && docker compose up -d --build`
- Refresh data: `docker compose exec app rm -rf data/extra data/prepared.duckdb && docker compose restart app`
- Local test without a domain: leave `DOMAIN` unset and open `https://localhost` (self-signed).

The app port is not published; only Caddy is reachable from outside, and the app trusts Caddy's
`X-Forwarded-For` for the per-IP limit.

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

Keep the app on `127.0.0.1`; expose only the proxy. The client address for rate limiting is taken from
`X-Forwarded-For`, which uvicorn trusts only from `127.0.0.1` by default — so run the proxy on the same machine.

**Caddy** (simplest, automatic certificates) — `/etc/caddy/Caddyfile`:

```
wnyc.example.com {
    encode gzip
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1          # answers are streamed line by line
    }
}
```

**nginx** alternative:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
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
| `HOST` / `PORT` | `127.0.0.1` / `8765` | Where the app listens. |

Limits only count questions that actually reach the model; repeated questions are served from `data/cache/`, and
the map, budget dial and trip check never call the model. Rough cost on Haiku: about a cent per fresh question.

## Security notes (public repo, public site)

- The key lives only in `.env` on the server (mode 600) — never in the repo, never sent to the browser.
- The model writes its own SQL; the database connection is opened read-only with DuckDB external access disabled
  and locked, so a crafted question cannot read files (such as `.env`) or reach the network through SQL.
- Set a spend limit on the key in the Claude Console; the per-IP and daily limits above are a second line.

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
