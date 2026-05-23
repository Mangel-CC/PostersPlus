"""In-process coordination backend (upstream default).

Holds the per-worker dicts that upstream uses for MDBList rate-limit backoff and
background-quality-fetch deduplication. Behaviour is identical to upstream;
this module just gives the state a stable address so a Redis backend can
substitute for it in hosted mode.

Render-coalescing and rating-fetch-coalescing primitives (asyncio.Future,
asyncio.Event) remain in main.py — they are inherently per-process and
shared-storage equivalents are out of scope (see RESILIENCE.md).
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# (namespace, key) -> expiry timestamp (monotonic loop time).
_backoff: dict[tuple[str, str], float] = {}

# (namespace, key) -> expiry timestamp (monotonic loop time). Used as a
# set-with-TTL so a stuck claim eventually clears.
_inflight: dict[tuple[str, str], float] = {}


async def init() -> None:
    """No-op for the in-process backend. Present so the public API is uniform."""
    return None


async def close() -> None:
    _backoff.clear()
    _inflight.clear()


def ping() -> bool:
    return True


async def is_backoff_active(namespace: str, key: str) -> bool:
    """True if a backoff window for this (namespace, key) is currently active."""
    until = _backoff.get((namespace, key))
    if until is None:
        return False
    now = asyncio.get_running_loop().time()
    if until <= now:
        # Lazy expiry — matches upstream behaviour.
        _backoff.pop((namespace, key), None)
        return False
    return True


async def set_backoff(namespace: str, key: str, ttl_seconds: float) -> None:
    """Mark a backoff window of ttl_seconds starting now."""
    until = asyncio.get_running_loop().time() + ttl_seconds
    _backoff[(namespace, key)] = until


async def clear_backoff(namespace: str, key: str) -> None:
    _backoff.pop((namespace, key), None)


async def claim_inflight(namespace: str, key: str, ttl_seconds: float = 300.0) -> bool:
    """Atomically claim a single-flight slot. Returns True if the caller now
    owns the slot, False if another caller holds it. Stuck claims are released
    after ttl_seconds so a crashed worker doesn't deadlock the slot forever."""
    now = asyncio.get_running_loop().time()
    expiry = _inflight.get((namespace, key))
    if expiry is not None and expiry > now:
        return False
    _inflight[(namespace, key)] = now + ttl_seconds
    return True


async def release_inflight(namespace: str, key: str) -> None:
    _inflight.pop((namespace, key), None)


async def prune_expired() -> None:
    """Best-effort cleanup of expired keys. Cheap on the in-process backend;
    the cache-prune loop calls this periodically. Redis backend has no-op
    because the server expires keys for us."""
    now = asyncio.get_running_loop().time()
    expired_backoff = [k for k, v in _backoff.items() if v <= now]
    for k in expired_backoff:
        _backoff.pop(k, None)
    expired_inflight = [k for k, v in _inflight.items() if v <= now]
    for k in expired_inflight:
        _inflight.pop(k, None)
    if expired_backoff or expired_inflight:
        logger.debug(
            "Coordinator prune: %d backoff, %d inflight",
            len(expired_backoff), len(expired_inflight),
        )
