#!/bin/sh
set -e

# Fix ownership of the cache volume mount so appuser can read/write it.
# This runs as root before we drop privileges — necessary because Docker
# creates the host-side directory as root when the volume is first mounted.
mkdir -p /app/cache/tmdb_posters /app/cache/tmdb_logos
chown -R appuser:appuser /app/cache

# ElfHosted fork — Phase 6: enable prometheus_client multiprocess mode so
# /metrics aggregates counters/histograms across all uvicorn worker
# processes. The directory must be writable by appuser and empty at
# startup (stale files from a previous run would survive a restart and
# inflate counters).
export PROMETHEUS_MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-/tmp/postersplus-prom}"
mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
chown -R appuser:appuser "$PROMETHEUS_MULTIPROC_DIR"
rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db

# Drop from root to appuser and exec uvicorn.
# gosu correctly transfers signals (SIGTERM etc.) to the child process,
# unlike 'su -c' which leaves an extra shell in the process tree.
exec gosu appuser uvicorn main:app --host 0.0.0.0 --port 8000 --workers "${WORKERS:-1}"
