"""Local-filesystem blob store (upstream default).

Bytes for the TMDB poster cache and TMDB logo cache land under
``/app/cache/<bucket>/<key>`` (matching upstream's two existing directories).
Behaviour mirrors upstream's previous cache.py functions exactly — TTL via
filesystem mtime, lazy stale-eviction on read.

I/O is wrapped in ``asyncio.to_thread`` so the event loop isn't blocked by
disk reads on a slow volume. Same call shape as the S3 backend so the public
selector can swap them transparently.
"""
import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

from config import TMDB_POSTER_CACHE_DIR, TMDB_LOGO_CACHE_DIR


# Bucket → on-disk base directory.
_BUCKETS: dict[str, str] = {
    "tmdb-posters": TMDB_POSTER_CACHE_DIR,
    "tmdb-logos":   TMDB_LOGO_CACHE_DIR,
}


def _base_for(bucket: str) -> str:
    try:
        return _BUCKETS[bucket]
    except KeyError as exc:
        raise ValueError(f"Unknown blob bucket: {bucket!r}") from exc


def _safe_path(base_dir: str, filename: str) -> str:
    """Defensive: resolve symlinks and ensure the result stays inside base_dir.
    Upstream's check verbatim."""
    path = os.path.realpath(os.path.join(base_dir, filename))
    if not path.startswith(os.path.realpath(base_dir)):
        raise ValueError(f"Path traversal attempt: {filename!r}")
    return path


def _remove_if_dir(path: str) -> bool:
    """Remove *path* if it is a directory (stale artefact from a previous bug)."""
    if os.path.isdir(path):
        try:
            os.rmdir(path)
            logger.info(f"Removed stale cache directory at {path}")
        except OSError:
            pass
        return True
    return False


async def init() -> None:
    for base in _BUCKETS.values():
        os.makedirs(base, exist_ok=True)


async def close() -> None:
    return None


def ping() -> bool:
    return all(os.path.isdir(base) for base in _BUCKETS.values())


def _get_sync(bucket: str, key: str, max_age_seconds: int) -> bytes | None:
    base = _base_for(bucket)
    path = _safe_path(base, key)

    if _remove_if_dir(path):
        return None
    if not os.path.exists(path):
        return None

    age_secs = time.time() - os.path.getmtime(path)
    if age_secs > max_age_seconds:
        logger.info(f"Blob cache expired for {bucket}:{key}")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None

    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        logger.error(f"Blob cache read error for {bucket}:{key}: {exc}")
        return None


def _put_sync(bucket: str, key: str, data: bytes) -> None:
    base = _base_for(bucket)
    path = _safe_path(base, key)
    try:
        _remove_if_dir(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except Exception as exc:
        logger.error(f"Blob cache write error for {bucket}:{key}: {exc}")


async def get(bucket: str, key: str, max_age_seconds: int) -> bytes | None:
    return await asyncio.to_thread(_get_sync, bucket, key, max_age_seconds)


async def put(bucket: str, key: str, data: bytes, content_type: str | None = None) -> None:
    # content_type is irrelevant for the FS backend but accepted so the
    # signature matches the S3 backend.
    await asyncio.to_thread(_put_sync, bucket, key, data)


def url_for(bucket: str, key: str) -> str | None:
    """The local backend never serves CDN URLs."""
    return None
