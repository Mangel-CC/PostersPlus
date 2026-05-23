"""Blob store backend selector.

Active backend chosen at import time by config.OBJECT_STORE_URL:

  * unset / empty  → blobstore.local (filesystem, upstream default)
  * s3:// URL      → blobstore.s3 (S3-compatible, opt-in)

Buckets currently in use:

  * ``tmdb-posters`` — base poster images fetched from TMDB (JPEG)
  * ``tmdb-logos``   — title logos fetched from TMDB (PNG)

Composite poster JPEGs remain in the relational backend (the final_poster_cache
table). They could move here in a future phase but the table is bounded by
COMPOSITE_MAX_ENTRIES and TTL so the trade-off is less compelling than for
the upstream TMDB blobs.
"""
import logging

from config import OBJECT_STORE_URL

logger = logging.getLogger(__name__)


_PUBLIC_API = (
    "init",
    "close",
    "ping",
    "get",
    "put",
    "url_for",
)


def _select_backend():
    url = (OBJECT_STORE_URL or "").strip()
    if url:
        if url.startswith(("s3://", "s3+http://", "s3+https://")):
            from blobstore import s3
            logger.info("Blob store backend: s3 (OBJECT_STORE_URL detected)")
            return s3
        raise RuntimeError(
            f"Unsupported OBJECT_STORE_URL scheme: {url.split('://', 1)[0]!r}. "
            "Set an s3:// URL or unset OBJECT_STORE_URL to use the local filesystem."
        )
    from blobstore import local
    logger.info("Blob store backend: local (default)")
    return local


_backend = _select_backend()

for _name in _PUBLIC_API:
    globals()[_name] = getattr(_backend, _name)

BACKEND_KIND: str = "s3" if _backend.__name__.endswith(".s3") else "local"

__all__ = list(_PUBLIC_API) + ["BACKEND_KIND"]


# Bucket constants — single source of truth so a typo can't fork the keyspace.
BUCKET_TMDB_POSTERS: str = "tmdb-posters"
BUCKET_TMDB_LOGOS: str   = "tmdb-logos"
