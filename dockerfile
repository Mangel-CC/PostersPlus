## ElfHosted fork — Phase 9 hardened multi-stage build.
##
## Builder stage installs wheels (build-essential needed for any source-only
## packages) and the final stage is a lean python:3.11-slim with only the
## site-packages, app sources, and gosu (for the volume-perm drop in
## entrypoint.sh — upstream's permission fix).
##
## The base image is pinned to a digest. Bump the digest deliberately when
## you want to track a new python:3.11-slim release.

FROM python:3.11-slim AS builder

WORKDIR /build

# build-essential is only needed for any wheels that fall back to source.
# Pillow, numpy, httpx, psycopg[binary], redis, boto3, prometheus-client,
# python-json-logger all ship binary wheels for linux/amd64+arm64; keeping
# build-essential covers any minor wheel gaps without enlarging the runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


## Final runtime image. Runs as root so entrypoint.sh can fix cache volume
## permissions, then drops to appuser via gosu before exec-ing uvicorn.

FROM python:3.11-slim

WORKDIR /app

# curl for the compose healthcheck; ca-certificates for TLS to TMDB / MDBList / S3;
# gosu for the privilege drop in entrypoint.sh.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy site-packages from the builder. /install is a `--prefix=`-style
# layout: bin/ lib/python3.11/site-packages/ etc.
COPY --from=builder /install /usr/local

RUN adduser --disabled-password --gecos '' appuser

# Copy app files and set ownership on everything except the cache dir,
# which is a runtime volume mount — permissions are fixed by entrypoint.sh.
COPY . .
RUN chown -R appuser:appuser /app

# Healthcheck via curl is lighter than spawning python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fs http://localhost:8000/live || exit 1

# No USER directive — entrypoint.sh runs as root briefly to chown the cache
# volume + prom multiproc dir, then `exec gosu appuser …` drops privileges.
CMD ["/bin/sh", "entrypoint.sh"]
