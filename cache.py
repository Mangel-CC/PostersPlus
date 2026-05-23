"""Storage facade.

Historically this module owned all SQLite cache code. The ElfHosted fork moves
the per-backend logic into the ``storage`` package so a Postgres backend can be
selected via ``DATABASE_URL``. Public function names and signatures are
preserved exactly so every ``from cache import …`` callsite works unchanged on
either backend.

Cherry-pick guide:
  * Upstream changes to ``cache.py`` map almost 1-to-1 to
    ``storage/sqlite_backend.py``.
  * The Postgres backend (``storage/postgres_backend.py``) mirrors the same
    signatures; new upstream functions need a parallel addition there.
"""
from storage import (
    BACKEND_KIND,
    init_db,
    prune_caches,
    ping,
    close,
    get_cached_final_poster,
    set_cached_final_poster,
    get_cached_rating,
    set_cached_rating,
    get_cached_quality,
    set_cached_quality,
    get_cached_trending_snapshot,
    set_cached_trending_snapshot,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
    get_cached_tmdb_metadata,
    set_cached_tmdb_metadata,
    delete_cached_tmdb_metadata,
    is_digital_release,
    count_digital_releases,
    add_digital_releases,
)

__all__ = [
    "BACKEND_KIND",
    "init_db",
    "prune_caches",
    "ping",
    "close",
    "get_cached_final_poster",
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
    "is_digital_release",
    "count_digital_releases",
    "add_digital_releases",
]
