"""Storage facade.

Upstream owns all cache logic in this module. The ElfHosted fork moves the
per-backend implementation into the ``storage`` package so a Postgres backend
can be selected via ``DATABASE_URL``, while composite poster BYTES move to the
``blobstore`` package (local FS default, S3/CDN via ``OBJECT_STORE_URL``).

Public function names and signatures are preserved exactly so every
``from cache import …`` callsite works unchanged on either backend. A thin
metrics wrapper around the most-trafficked lookups feeds cache hit/miss
counters to /metrics; the wrappers are otherwise pass-throughs. Backend
selection lives in storage/__init__.py.

Note: the final-poster trio (get/set/is_fresh) is ASYNC here because the
bytes live in the blobstore. Callers must ``await`` them.

Cherry-pick guide:
  * Upstream changes to cache logic map almost 1-to-1 to
    storage/sqlite_backend.py (which is seeded from upstream cache.py).
  * The Postgres backend mirrors the same signatures; a new upstream cache
    function needs a parallel addition in storage/postgres_backend.py and an
    entry in storage/__init__.py's _PUBLIC_API.
"""
# Pass-through re-exports (no instrumentation).
from storage import (
    BACKEND_KIND,
    init_db,
    prune_caches,
    ping,
    close,
    get_cached_final_poster_url,
    set_cached_rating,
    set_cached_quality,
    get_cached_trending_snapshot,
    set_cached_trending_snapshot,
    set_cached_tmdb_poster,
    set_cached_tmdb_logo,
    set_cached_tmdb_metadata,
    delete_cached_tmdb_metadata,
    is_digital_release,
    count_digital_releases,
    add_digital_releases,
    get_cached_imdb_to_tmdb,
    set_cached_imdb_to_tmdb,
    set_cached_release_status,
    set_cached_text_detection,
    get_cache_stats,
)
# Instrumented lookups — imported under private aliases, wrapped below.
from storage import (
    get_cached_final_poster      as _raw_get_final_poster,
    set_cached_final_poster      as _raw_set_final_poster,
    is_cached_final_poster_fresh as _raw_is_final_fresh,
    get_cached_rating            as _raw_get_rating,
    get_cached_quality           as _raw_get_quality,
    get_cached_tmdb_metadata     as _raw_get_tmdb_metadata,
    get_cached_tmdb_poster       as _raw_get_tmdb_poster,
    get_cached_tmdb_logo         as _raw_get_tmdb_logo,
    get_cached_release_status    as _raw_get_release_status,
    get_cached_text_detection    as _raw_get_text_detection,
)
import metrics as _metrics


def _record(table: str, hit: bool) -> None:
    _metrics.cache_lookups_total.labels(
        table=table, result="hit" if hit else "miss",
    ).inc()


# --- Final composite poster (async — bytes live in the blobstore) ---------

async def get_cached_final_poster(cache_key):
    r = await _raw_get_final_poster(cache_key)
    _record("final_poster", r is not None)
    return r


async def is_cached_final_poster_fresh(cache_key) -> bool:
    """Lightweight freshness probe — metadata row + TTL only, no blob fetch.
    Lets /poster and /p 302 straight to the CDN when a public URL exists."""
    fresh = await _raw_is_final_fresh(cache_key)
    _record("final_poster", fresh)
    return fresh


async def set_cached_final_poster(cache_key, jpeg_bytes):
    """Async pass-through to the storage backend's blobstore-aware writer."""
    await _raw_set_final_poster(cache_key, jpeg_bytes)


# --- Sync lookups ----------------------------------------------------------

def get_cached_rating(imdb_id):
    r = _raw_get_rating(imdb_id)
    _record("rating", r is not None)
    return r


def get_cached_quality(imdb_id, release_date=None):
    r = _raw_get_quality(imdb_id, release_date)
    _record("quality", r is not None)
    return r


def get_cached_tmdb_metadata(cache_key):
    r = _raw_get_tmdb_metadata(cache_key)
    _record("tmdb_metadata", r is not None)
    return r


def get_cached_tmdb_poster(cache_key):
    r = _raw_get_tmdb_poster(cache_key)
    _record("tmdb_poster", r is not None)
    return r


def get_cached_tmdb_logo(cache_key):
    r = _raw_get_tmdb_logo(cache_key)
    _record("tmdb_logo", r is not None)
    return r


def get_cached_release_status(cache_key):
    r = _raw_get_release_status(cache_key)
    _record("release_status", r is not None)
    return r


def get_cached_text_detection(cache_key):
    # None means "not cached"; True/False are both cache hits.
    r = _raw_get_text_detection(cache_key)
    _record("text_detection", r is not None)
    return r


__all__ = [
    "BACKEND_KIND",
    "init_db",
    "prune_caches",
    "ping",
    "close",
    "get_cache_stats",
    "get_cached_final_poster",
    "get_cached_final_poster_url",
    "is_cached_final_poster_fresh",
    "set_cached_final_poster",
    "get_cached_rating",
    "set_cached_rating",
    "get_cached_quality",
    "set_cached_quality",
    "get_cached_trending_snapshot",
    "set_cached_trending_snapshot",
    "get_cached_tmdb_poster",
    "set_cached_tmdb_poster",
    "get_cached_tmdb_logo",
    "set_cached_tmdb_logo",
    "get_cached_tmdb_metadata",
    "set_cached_tmdb_metadata",
    "delete_cached_tmdb_metadata",
    "get_cached_release_status",
    "set_cached_release_status",
    "get_cached_text_detection",
    "set_cached_text_detection",
    "is_digital_release",
    "count_digital_releases",
    "add_digital_releases",
    "get_cached_imdb_to_tmdb",
    "set_cached_imdb_to_tmdb",
]
