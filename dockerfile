## ElfHosted fork — Phase 9 hardened multi-stage build.
##
## Builder stage installs wheels (build-essential needed for any source-only
## packages) and the final stage is a lean python:3.11-slim with only the
## site-packages and app sources.
##
## Runs as appuser from the start — no gosu / root-startup dance. On
## Kubernetes the deployment's SecurityContext (runAsNonRoot, runAsUser,
## fsGroup) handles per-container user policy and volume permissions, so
## the container doesn't need to drop privileges at runtime. For docker
## compose users on a fresh host volume: chown the host directory before
## the first `up`, or set the volume's uid/gid in compose.yaml.

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


## Final runtime image.

FROM python:3.11-slim

WORKDIR /app

# curl for the compose healthcheck; ca-certificates for TLS to TMDB / MDBList / S3.
# No compiler toolchain in the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy site-packages from the builder. /install is a `--prefix=`-style
# layout: bin/ lib/python3.11/site-packages/ etc.
COPY --from=builder /install /usr/local

# Create appuser with explicit uid/gid 568. Numeric so Kubernetes
# `runAsNonRoot` validation succeeds and matches ElfHosted's per-app
# convention (see helmrelease-postersplus.yaml securityContext).
RUN groupadd --gid 568 appuser \
    && useradd --uid 568 --gid 568 --shell /bin/sh --create-home appuser

# Copy app files and create cache dir while still root, then hand ownership over.
COPY . .
RUN mkdir -p /app/cache /app/cache/tmdb_posters /app/cache/tmdb_logos \
                /tmp/postersplus-prom \
    && chown -R 568:568 /app /tmp/postersplus-prom

# Numeric so kubelet can verify runAsNonRoot without resolving /etc/passwd.
USER 568

# Healthcheck via curl is lighter than spawning python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fs http://localhost:8000/live || exit 1

CMD ["/bin/sh", "entrypoint.sh"]
