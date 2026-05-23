## ElfHosted fork — Phase 9 hardened multi-stage build.
##
## Builder stage installs wheels (build-essential needed for any source-only
## packages) and the final stage is a lean python:3.11-slim with only the
## site-packages and app sources. No compiler toolchain in the runtime image.
##
## NB: the base image is pinned to a digest. Bump the digest deliberately
## when you want to track a new python:3.11-slim release.

FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0 AS builder

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

FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0

WORKDIR /app

# Install curl for the compose healthcheck and as a debugging convenience.
# Other production deps come from /install (wheels built in the builder).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy site-packages from the builder. /install is a `--prefix=`-style
# layout: bin/ lib/python3.11/site-packages/ etc.
COPY --from=builder /install /usr/local

RUN adduser --disabled-password --gecos '' appuser

# Copy app files and create cache dir while still root, then hand ownership over.
COPY . .
RUN mkdir -p /app/cache /app/cache/tmdb_posters /app/cache/tmdb_logos \
    && chown -R appuser:appuser /app

USER appuser

# Healthcheck via curl is lighter than spawning python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fs http://localhost:8000/live || exit 1

CMD ["/bin/sh", "entrypoint.sh"]
