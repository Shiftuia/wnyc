# Wheelchair NYC: one image, two roles (see compose.yaml).
#   wnyc       serves app/server.py. Read-only rootfs, data volume mounted read-only, no downloads at start.
#   bootstrap  one-shot: scripts/bootstrap.sh downloads the public datasets into the data volume, builds the
#              DuckDB file and writes data/MANIFEST.sha256.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b AS base

# curl + unzip are for the bootstrap downloads (scripts/fetch*.sh) only
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 wnyc

WORKDIR /app
COPY requirements.txt .
RUN python -m venv .venv && .venv/bin/pip install --no-cache-dir -r requirements.txt

USER wnyc
# Bake the DuckDB spatial extension and matplotlib's font cache into the image: the served container has a
# read-only root filesystem and no route to extensions.duckdb.org.
ENV MPLCONFIGDIR=/home/wnyc/.mplconfig
RUN /app/.venv/bin/python -c "import duckdb; c = duckdb.connect(); c.install_extension('spatial'); c.load_extension('spatial')" \
    && /app/.venv/bin/python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot"

USER root
COPY --chown=root:root . .
RUN mkdir -p data /cache && chown wnyc:wnyc data /cache

FROM base AS test
COPY requirements-dev.txt .
RUN .venv/bin/pip install --no-cache-dir -r requirements-dev.txt
USER wnyc
CMD [".venv/bin/python", "-m", "pytest", "-q", "tests"]

FROM base AS app
ARG GIT_REVISION=unset
LABEL org.opencontainers.image.source="https://github.com/Shiftuia/wnyc" \
      org.opencontainers.image.revision="${GIT_REVISION}"
ENV HOST=0.0.0.0 PORT=8765 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WNYC_GIT_REVISION=${GIT_REVISION}
USER wnyc
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4).status == 200 else 1)"]
CMD [".venv/bin/python", "app/server.py"]
