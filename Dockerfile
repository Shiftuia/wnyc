# Wheelchair NYC — app image. Data is NOT baked in: on first start run.sh downloads the public datasets into
# /app/data (a volume) and builds the DuckDB file (~5–10 min); later starts reuse it.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 wnyc

WORKDIR /app
COPY requirements.txt .
RUN python -m venv .venv && .venv/bin/pip install --no-cache-dir -r requirements.txt

COPY --chown=wnyc:wnyc . .
RUN mkdir -p data && chown wnyc:wnyc /app data

USER wnyc
# listen on all interfaces inside the container; only the proxy publishes a port
ENV HOST=0.0.0.0 PORT=8765 PYTHONUNBUFFERED=1
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20m --retries=3 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:8765/ || exit 1
CMD ["./run.sh"]
