"""PostgreSQL storage backend — opt-in via DATABASE_URL.

Mirrors the public surface of storage.sqlite_backend so upstream's API is
preserved exactly. Operates against a connection pool so multiple replicas /
workers can share a single Postgres instance.

Phase 10 split (matches the SQLite backend):
  * TMDB poster/logo bytes — per-pod filesystem (TMDB's own CDN is the
    source of truth; sharing this cache across replicas is not worth
    the complexity).
  * Composite (rendered) bytes — delegated to the ``blobstore`` package
    so they can land in S3 + Cloudflare instead of clogging the Postgres
    backups / replication stream as BYTEA. This module keeps only the
    cache metadata row.
"""
import logging
import os
import time
import json
from datetime import datetime

logger = logging.getLogger(__name__)

import psycopg
from psycopg_pool import ConnectionPool

import blobstore
from config import (
    DATABASE_URL,
    DB_POOL_MIN_SIZE,
    DB_POOL_MAX_SIZE,
    DAYS_CONSIDERED_NEW,
    NEW_CACHE_DURATION,
    OLD_CACHE_DURATION,
    TRENDING_CACHE_DURATION,
    TMDB_POSTER_CACHE_DIR,
    TMDB_POSTER_CACHE_DURATION,
    TMDB_LOGO_CACHE_DIR,
    TMDB_LOGO_CACHE_DURATION,
    TMDB_METADATA_CACHE_DURATION,
    COMPOSITE_CACHE_TTL,
    COMPOSITE_MAX_ENTRIES,
    QUALITY_OLD_CACHE_DURATION,
    DIGITAL_RELEASE_MAX_AGE_DAYS,
    RATING_MIN_VOTES,
)

# Pure helpers (TTL math + filesystem-cache primitives) shared with the
# SQLite backend so we don't duplicate them. These have no Postgres-side
# state, just static functions that take a cache key.
from storage.sqlite_backend import (
    _rating_ttl,
    _quality_ttl,
    _safe_cache_path,
    _remove_if_dir,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
)


_pool: ConnectionPool | None = None

# Release status cache TTL — status changes slowly (Cinema → Streaming →
# BluRay is one-way), so 7 days is plenty. Mirrors the SQLite backend.
_RELEASE_STATUS_TTL_DAYS = 7

# Postgres advisory lock key for serialising schema bootstrap across replicas.
# Arbitrary 64-bit int unique to this app.
_SCHEMA_LOCK_KEY = 0x504F5354_2B505553  # "POST+PUS"


def _get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("Database not initialized")
    return _pool


def _bootstrap_schema(conn) -> None:
    """Idempotent DDL. Wrapped in a single transaction; called with the
    advisory lock held."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rating_cache (
                imdb_id        TEXT PRIMARY KEY,
                ratings_json   TEXT,
                genre          TEXT,
                cached_at      BIGINT,
                release_date   TEXT,
                award_wins     TEXT NOT NULL DEFAULT '',
                award_noms     TEXT NOT NULL DEFAULT '',
                awards_fetched INTEGER NOT NULL DEFAULT 0,
                festival_label TEXT,
                age_rating     INTEGER,
                is_cult        INTEGER NOT NULL DEFAULT 0,
                is_true_story  INTEGER NOT NULL DEFAULT 0,
                is_metacritic  INTEGER NOT NULL DEFAULT 0,
                rating_min_votes INTEGER
            )
        """)
        # Idempotent rating_min_votes migration for instances that pre-date the
        # rating-policy invalidation feature. Safe to run on every startup.
        cur.execute("""
            ALTER TABLE rating_cache ADD COLUMN IF NOT EXISTS rating_min_votes INTEGER
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quality_cache (
                imdb_id      TEXT PRIMARY KEY,
                tokens       TEXT,
                cached_at    BIGINT,
                release_date TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trending_cache (
                media_type    TEXT PRIMARY KEY,
                rankings_json TEXT,
                cached_at     BIGINT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tmdb_metadata_cache (
                cache_key           TEXT PRIMARY KEY,
                title               TEXT,
                release_year        TEXT,
                genre_ids           TEXT,
                is_textless         INTEGER,
                poster_path         TEXT,
                logos_json          TEXT,
                cached_at           BIGINT,
                credits_json        TEXT,
                production_cos_json TEXT,
                runtime             INTEGER,
                number_of_seasons   INTEGER,
                number_of_episodes  INTEGER,
                original_language   TEXT,
                original_title      TEXT,
                backdrop_path       TEXT,
                tmdb_status         TEXT,
                vote_count          INTEGER,
                text_backdrop_path  TEXT,
                original_poster_path TEXT,
                poster_langs_json   TEXT
            )
        """)
        # Idempotent additive migrations for instances that pre-date these
        # columns. Postgres supports ADD COLUMN IF NOT EXISTS, so each is a
        # no-op on installs that already have the column. Safe on every startup.
        for col, definition in (
            ("credits_json",        "TEXT"),
            ("production_cos_json", "TEXT"),
            ("runtime",             "INTEGER"),
            ("number_of_seasons",   "INTEGER"),
            ("number_of_episodes",  "INTEGER"),
            ("original_language",   "TEXT"),
            ("original_title",      "TEXT"),
            ("backdrop_path",       "TEXT"),
            ("tmdb_status",         "TEXT"),
            ("vote_count",          "INTEGER"),
            ("text_backdrop_path",  "TEXT"),
            ("original_poster_path","TEXT"),
            ("poster_langs_json",   "TEXT"),
        ):
            cur.execute(
                f"ALTER TABLE tmdb_metadata_cache ADD COLUMN IF NOT EXISTS {col} {definition}"
            )
        # Phase 10: composite-poster bytes live in blobstore (FS or S3); the
        # table holds only metadata so the relational backend can do TTL
        # bookkeeping cheaply.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS final_poster_cache (
                cache_key  TEXT PRIMARY KEY,
                cached_at  BIGINT NOT NULL
            )
        """)
        # Migrate pre-Phase-10 deployments that have the BYTEA column.
        # Idempotent: IF EXISTS makes it a no-op on new installs.
        cur.execute("""
            ALTER TABLE final_poster_cache DROP COLUMN IF EXISTS jpeg_bytes
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS final_poster_cache_cached_at_idx
                ON final_poster_cache (cached_at)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS digital_release_cache (
                imdb_id   TEXT PRIMARY KEY,
                posted_at BIGINT NOT NULL
            )
        """)
        # Phase 11: imdb_id -> tmdb_id mapping for the preset endpoint.
        # No TTL — these mappings are effectively permanent.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS imdb_to_tmdb_cache (
                imdb_id    TEXT NOT NULL,
                media_type TEXT NOT NULL,
                tmdb_id    TEXT NOT NULL,
                PRIMARY KEY (imdb_id, media_type)
            )
        """)
        # Release status cache — populated on demand when the "release_status"
        # sash slot is enabled. Stored separately from the main metadata cache
        # so users who don't enable the feature never pay the extra API call.
        # cache_key = "{media_type}_{tmdb_id}", status =
        # "BluRay"|"Streaming"|"Cinema"|"Production". TTL: 7 days.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS release_status_cache (
                cache_key TEXT PRIMARY KEY,
                status    TEXT NOT NULL,
                cached_at BIGINT NOT NULL
            )
        """)
        # Burned-in-text detection results, keyed by source asset + detection
        # params. TMDB image paths are content-addressed (immutable), so the
        # answer never goes stale; cached_at exists only for housekeeping.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS text_detection_cache (
                cache_key TEXT PRIMARY KEY,
                has_text  INTEGER NOT NULL,
                cached_at BIGINT NOT NULL
            )
        """)
    conn.commit()


def init_db() -> None:
    """Create the connection pool and run idempotent schema bootstrap."""
    global _pool

    # Filesystem cache dirs — same as SQLite backend (Phase 3 removes these).
    os.makedirs(TMDB_POSTER_CACHE_DIR, exist_ok=True)
    os.makedirs(TMDB_LOGO_CACHE_DIR, exist_ok=True)

    _pool = ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=DB_POOL_MIN_SIZE,
        max_size=DB_POOL_MAX_SIZE,
        kwargs={"autocommit": False},
        open=True,
        timeout=10.0,
    )

    # Serialise schema bootstrap across replicas with a transaction-scoped
    # advisory lock. pg_advisory_xact_lock blocks until the lock is acquired
    # and releases automatically at commit / rollback.
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
        _bootstrap_schema(conn)

    logger.info("Postgres backend initialised (pool %d-%d)", DB_POOL_MIN_SIZE, DB_POOL_MAX_SIZE)


# ---------------------------------------------------------------------------
# Final composite poster cache
# ---------------------------------------------------------------------------

def _peek_final_poster(cache_key: str) -> bool:
    """Row-only TTL check. True if a fresh row exists; deletes the row
    on expiry. Caller handles blob cleanup."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cached_at FROM final_poster_cache WHERE cache_key = %s",
                (cache_key,),
            )
            row = cur.fetchone()
            if not row:
                return False
            (cached_at,) = row
            age_secs = time.time() - cached_at
            if age_secs > COMPOSITE_CACHE_TTL:
                logger.info(f"Final poster cache expired for {cache_key} ({age_secs/86400:.1f}d old)")
                cur.execute(
                    "DELETE FROM final_poster_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                conn.commit()
                return False
            return True


async def is_cached_final_poster_fresh(cache_key: str) -> bool:
    """Lightweight freshness probe — checks the metadata row + TTL only,
    never touches the blobstore. Lets /poster emit a 302 to the CDN
    without pulling the bytes through the app pod."""
    try:
        fresh = _peek_final_poster(cache_key)
        if not fresh:
            await blobstore.delete(blobstore.BUCKET_COMPOSITES, cache_key)
        return fresh
    except Exception as exc:
        logger.error(f"Final poster freshness probe error: {exc}")
        return False


async def get_cached_final_poster(cache_key: str) -> bytes | None:
    """Full read: metadata TTL check + blob fetch. Used as the
    inline-serve fallback when no CDN URL is configured."""
    try:
        if not _peek_final_poster(cache_key):
            await blobstore.delete(blobstore.BUCKET_COMPOSITES, cache_key)
            return None
        return await blobstore.get(
            blobstore.BUCKET_COMPOSITES, cache_key, max_age_seconds=COMPOSITE_CACHE_TTL,
        )
    except Exception as exc:
        logger.error(f"Final poster cache read error: {exc}")
        return None


def get_cached_final_poster_url(cache_key: str) -> str | None:
    """Public CDN URL for the composite, or None when no URL is configured."""
    return blobstore.url_for(blobstore.BUCKET_COMPOSITES, cache_key)


async def set_cached_final_poster(cache_key: str, jpeg_bytes: bytes) -> None:
    try:
        # Write bytes first so the metadata row is never present without
        # a backing blob.
        await blobstore.put(
            blobstore.BUCKET_COMPOSITES, cache_key, jpeg_bytes,
            content_type="image/jpeg",
        )
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO final_poster_cache (cache_key, cached_at)
                    VALUES (%s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        cached_at  = EXCLUDED.cached_at
                    """,
                    (cache_key, int(time.time())),
                )
                evict_keys: list[str] = []
                if COMPOSITE_MAX_ENTRIES > 0:
                    cur.execute("SELECT COUNT(*) FROM final_poster_cache")
                    (count,) = cur.fetchone()
                    overflow = count - COMPOSITE_MAX_ENTRIES
                    if overflow > 0:
                        cur.execute(
                            "SELECT cache_key FROM final_poster_cache "
                            "ORDER BY cached_at ASC LIMIT %s",
                            (overflow,),
                        )
                        evict_keys = [r[0] for r in cur.fetchall()]
                        cur.execute(
                            """
                            DELETE FROM final_poster_cache WHERE cache_key = ANY(%s)
                            """,
                            (evict_keys,),
                        )
                        logger.info(f"Composite cache cap: evicted {overflow} oldest entries")
            conn.commit()
        # Best-effort blob cleanup for evicted keys, outside the pool ctx.
        for k in evict_keys:
            try:
                await blobstore.delete(blobstore.BUCKET_COMPOSITES, k)
            except Exception:
                pass
    except Exception as exc:
        logger.error(f"Final poster cache write error: {exc}")


async def prune_caches() -> None:
    now = int(time.time())
    expired_composite_keys: list[str] = []
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Phase 10: capture composite keys before deletion so we can
                # drop the corresponding blobs after the txn commits.
                cur.execute(
                    "SELECT cache_key FROM final_poster_cache WHERE cached_at < %s",
                    (now - COMPOSITE_CACHE_TTL,),
                )
                expired_composite_keys = [r[0] for r in cur.fetchall()]
                if expired_composite_keys:
                    cur.execute(
                        "DELETE FROM final_poster_cache WHERE cache_key = ANY(%s)",
                        (expired_composite_keys,),
                    )
                    logger.info(f"Pruned {cur.rowcount} expired composite cache entries")

                rating_cutoff   = now - OLD_CACHE_DURATION           * 86400
                quality_cutoff  = now - QUALITY_OLD_CACHE_DURATION   * 86400
                metadata_cutoff = now - TMDB_METADATA_CACHE_DURATION * 86400

                cur.execute(
                    "DELETE FROM rating_cache WHERE cached_at < %s", (rating_cutoff,)
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired rating cache entries")

                cur.execute(
                    "DELETE FROM quality_cache WHERE cached_at < %s", (quality_cutoff,)
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired quality cache entries")

                cur.execute(
                    "DELETE FROM tmdb_metadata_cache WHERE cached_at < %s",
                    (metadata_cutoff,),
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired TMDB metadata cache entries")

                digital_cutoff = now - DIGITAL_RELEASE_MAX_AGE_DAYS * 86400
                cur.execute(
                    "DELETE FROM digital_release_cache WHERE posted_at < %s",
                    (digital_cutoff,),
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired digital release cache entries")

                release_status_cutoff = now - _RELEASE_STATUS_TTL_DAYS * 86400
                cur.execute(
                    "DELETE FROM release_status_cache WHERE cached_at < %s",
                    (release_status_cutoff,),
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired release status cache entries")

                detection_cutoff = now - 180 * 86400
                cur.execute(
                    "DELETE FROM text_detection_cache WHERE cached_at < %s",
                    (detection_cutoff,),
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} old text-detection cache entries")
            conn.commit()
        # Postgres autovacuum handles space reclamation — no explicit VACUUM here.

        # Phase 10: best-effort blob deletion for evicted composite rows,
        # outside the connection-pool ctx so a slow S3 doesn't hold the
        # connection.
        for k in expired_composite_keys:
            try:
                await blobstore.delete(blobstore.BUCKET_COMPOSITES, k)
            except Exception as exc:
                logger.warning(f"Composite blob delete error for {k}: {exc}")
    except Exception as exc:
        logger.error(f"Cache prune error: {exc}")


# ---------------------------------------------------------------------------
# Rating cache
# ---------------------------------------------------------------------------

def get_cached_rating(imdb_id: str):
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ratings_json, genre, cached_at, release_date,
                           award_wins, award_noms, awards_fetched, festival_label,
                           age_rating, is_cult, is_true_story, is_metacritic,
                           rating_min_votes
                    FROM rating_cache
                    WHERE imdb_id = %s
                    """,
                    (imdb_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None

                (ratings_json, genre, cached_at, release_date,
                 wins_raw, noms_raw, awards_fetched_int, festival_label,
                 age_rating, is_cult_int, is_true_story_int, is_metacritic_int,
                 rating_min_votes) = row

                if rating_min_votes is not None and rating_min_votes != RATING_MIN_VOTES:
                    logger.info(
                        f"Rating cache policy changed for {imdb_id}: "
                        f"stored={rating_min_votes!r}, current={RATING_MIN_VOTES}; refreshing"
                    )
                    cur.execute(
                        "DELETE FROM rating_cache WHERE imdb_id = %s",
                        (imdb_id,),
                    )
                    conn.commit()
                    return None

                age_days = (time.time() - cached_at) / 86400

                if age_days > _rating_ttl(release_date):
                    logger.info(f"Rating cache expired for {imdb_id} ({age_days:.1f}d old)")
                    cur.execute(
                        "DELETE FROM rating_cache WHERE imdb_id = %s",
                        (imdb_id,),
                    )
                    conn.commit()
                    return None

                if rating_min_votes is None:
                    # Rows created before policy tracking are still valid until
                    # their normal TTL expires. Backfill in place instead of
                    # consuming one MDBList request per legacy cache entry after
                    # an upgrade.
                    cur.execute(
                        "UPDATE rating_cache SET rating_min_votes = %s "
                        "WHERE imdb_id = %s AND rating_min_votes IS NULL",
                        (RATING_MIN_VOTES, imdb_id),
                    )
                    conn.commit()
                    logger.debug(f"Backfilled rating cache policy for {imdb_id}")

                ratings_dict = json.loads(ratings_json or "{}")
                wins = [w for w in (wins_raw or "").split("|") if w]
                noms = [n for n in (noms_raw or "").split("|") if n]
                awards_fetched = bool(awards_fetched_int)

                return (ratings_dict, genre, release_date, wins, noms,
                        awards_fetched, festival_label, age_rating,
                        bool(is_cult_int), bool(is_true_story_int), bool(is_metacritic_int))
    except Exception as exc:
        logger.error(f"Cache read error: {exc}")
        return None


def set_cached_rating(
    imdb_id: str,
    ratings_dict: dict,
    genre: str,
    rel: str | None,
    award_wins: list[str],
    award_noms: list[str],
    awards_fetched: bool = False,
    festival_label: str | None = None,
    age_rating: int | None = None,
    is_cult: bool = False,
    is_true_story: bool = False,
    is_metacritic: bool = False,
) -> None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rating_cache
                        (imdb_id, ratings_json, genre, cached_at, release_date,
                         award_wins, award_noms, awards_fetched, festival_label,
                         age_rating, is_cult, is_true_story, is_metacritic,
                         rating_min_votes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (imdb_id) DO UPDATE SET
                        ratings_json     = EXCLUDED.ratings_json,
                        genre            = EXCLUDED.genre,
                        cached_at        = EXCLUDED.cached_at,
                        release_date     = EXCLUDED.release_date,
                        award_wins       = EXCLUDED.award_wins,
                        award_noms       = EXCLUDED.award_noms,
                        awards_fetched   = EXCLUDED.awards_fetched,
                        festival_label   = EXCLUDED.festival_label,
                        age_rating       = EXCLUDED.age_rating,
                        is_cult          = EXCLUDED.is_cult,
                        is_true_story    = EXCLUDED.is_true_story,
                        is_metacritic    = EXCLUDED.is_metacritic,
                        rating_min_votes = EXCLUDED.rating_min_votes
                    """,
                    (
                        imdb_id,
                        json.dumps(ratings_dict),
                        genre,
                        int(time.time()),
                        rel,
                        "|".join(award_wins or []),
                        "|".join(award_noms or []),
                        int(awards_fetched),
                        festival_label,
                        age_rating,
                        int(is_cult),
                        int(is_true_story),
                        int(is_metacritic),
                        RATING_MIN_VOTES,
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Cache write error: {exc}")


# ---------------------------------------------------------------------------
# Quality cache
# ---------------------------------------------------------------------------

def get_cached_quality(imdb_id: str, release_date: str | None = None) -> list[str] | None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tokens, cached_at, release_date FROM quality_cache WHERE imdb_id = %s",
                    (imdb_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None

                tokens_raw, cached_at, stored_release = row
                ttl_release = release_date or stored_release
                age_days    = (time.time() - cached_at) / 86400
                if age_days > _quality_ttl(ttl_release):
                    logger.info(f"Quality cache expired for {imdb_id} ({age_days:.1f}d old)")
                    cur.execute("DELETE FROM quality_cache WHERE imdb_id = %s", (imdb_id,))
                    conn.commit()
                    return None

                return [t for t in (tokens_raw or "").split("|") if t]
    except Exception as exc:
        logger.error(f"Quality cache read error: {exc}")
        return None


def set_cached_quality(
    imdb_id: str,
    tokens: list[str],
    release_date: str | None = None,
) -> None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO quality_cache (imdb_id, tokens, cached_at, release_date)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (imdb_id) DO UPDATE SET
                        tokens       = EXCLUDED.tokens,
                        cached_at    = EXCLUDED.cached_at,
                        release_date = EXCLUDED.release_date
                    """,
                    (imdb_id, "|".join(tokens), int(time.time()), release_date),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Quality cache write error: {exc}")


# ---------------------------------------------------------------------------
# Trending snapshot cache
# ---------------------------------------------------------------------------

def get_cached_trending_snapshot(media_type: str) -> dict[str, int] | None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rankings_json, cached_at FROM trending_cache WHERE media_type = %s",
                    (media_type,),
                )
                row = cur.fetchone()
                if not row:
                    return None

                rankings_json, cached_at = row
                age_days = (time.time() - cached_at) / 86400

                if age_days > TRENDING_CACHE_DURATION:
                    return None

                return json.loads(rankings_json)
    except Exception as exc:
        logger.error(f"Trending snapshot cache read error: {exc}")
        return None


def set_cached_trending_snapshot(
    media_type: str,
    rankings: dict[str, int],
) -> None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trending_cache (media_type, rankings_json, cached_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (media_type) DO UPDATE SET
                        rankings_json = EXCLUDED.rankings_json,
                        cached_at     = EXCLUDED.cached_at
                    """,
                    (media_type, json.dumps(rankings), int(time.time())),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Trending snapshot cache write error: {exc}")


# ---------------------------------------------------------------------------
# TMDB metadata cache
# ---------------------------------------------------------------------------

def get_cached_tmdb_metadata(cache_key: str) -> dict | None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT title, release_year, genre_ids, is_textless, poster_path,
                           logos_json, cached_at,
                           credits_json, production_cos_json,
                           runtime, number_of_seasons, number_of_episodes,
                           original_language, original_title, backdrop_path, tmdb_status, vote_count,
                           text_backdrop_path, original_poster_path,
                           poster_langs_json
                    FROM tmdb_metadata_cache
                    WHERE cache_key = %s
                    """,
                    (cache_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None

                (
                    title, release_year, genre_ids_raw, is_textless, poster_path,
                    logos_json, cached_at,
                    credits_json, production_cos_json,
                    runtime, number_of_seasons, number_of_episodes,
                    original_language, original_title, backdrop_path, tmdb_status, vote_count,
                    text_backdrop_path, original_poster_path,
                    poster_langs_json,
                ) = row

                age_days = (time.time() - cached_at) / 86400
                if age_days > TMDB_METADATA_CACHE_DURATION:
                    logger.info(f"TMDB metadata cache expired for {cache_key} ({age_days:.1f}d old)")
                    cur.execute(
                        "DELETE FROM tmdb_metadata_cache WHERE cache_key = %s",
                        (cache_key,),
                    )
                    conn.commit()
                    return None

                # Rows created before vote_count or original_title was added were migrated
                # with NULL. Refresh once so detection has complete title aliases.
                if vote_count is None or original_title is None:
                    logger.info(
                        f"TMDB metadata cache missing vote_count or original_title for {cache_key}; refreshing"
                    )
                    cur.execute(
                        "DELETE FROM tmdb_metadata_cache WHERE cache_key = %s",
                        (cache_key,),
                    )
                    conn.commit()
                    return None

                return {
                    "title":                title,
                    "release_year":         release_year,
                    "genre_ids":            json.loads(genre_ids_raw or "[]"),
                    "is_textless":          bool(is_textless),
                    "poster_path":          poster_path,
                    "logos":                json.loads(logos_json or "[]"),
                    "credits":              json.loads(credits_json or "{}"),
                    "production_companies": json.loads(production_cos_json or "[]"),
                    "runtime":              runtime,
                    "number_of_seasons":    number_of_seasons,
                    "number_of_episodes":   number_of_episodes,
                    "original_language":    original_language,
                    "original_title":       original_title,
                    "backdrop_path":        backdrop_path,
                    "tmdb_status":          tmdb_status,
                    "vote_count":           vote_count,
                    "text_backdrop_path":   text_backdrop_path,
                    "original_poster_path": original_poster_path,
                    "poster_langs":         json.loads(poster_langs_json or "{}"),
                }
    except Exception as exc:
        logger.error(f"TMDB metadata cache read error: {exc}")
        return None


def set_cached_tmdb_metadata(
    cache_key: str,
    title: str,
    release_year: str | None,
    genre_ids: list[int],
    is_textless: bool,
    poster_path: str,
    logos: list[dict],
    *,
    credits: dict | None = None,
    production_companies: list[dict] | None = None,
    original_language: str | None = None,
    original_title: str | None = None,
    runtime: int | None = None,
    number_of_seasons: int | None = None,
    number_of_episodes: int | None = None,
    backdrop_path: str | None = None,
    tmdb_status: str | None = None,
    vote_count: int | None = None,
    text_backdrop_path: str | None = None,
    original_poster_path: str | None = None,
    poster_langs: dict | None = None,
) -> None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tmdb_metadata_cache
                        (cache_key, title, release_year, genre_ids, is_textless,
                         poster_path, logos_json, cached_at,
                         credits_json, production_cos_json,
                         runtime, number_of_seasons, number_of_episodes,
                         original_language, original_title, backdrop_path, tmdb_status, vote_count,
                         text_backdrop_path, original_poster_path,
                         poster_langs_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        title                = EXCLUDED.title,
                        release_year         = EXCLUDED.release_year,
                        genre_ids            = EXCLUDED.genre_ids,
                        is_textless          = EXCLUDED.is_textless,
                        poster_path          = EXCLUDED.poster_path,
                        logos_json           = EXCLUDED.logos_json,
                        cached_at            = EXCLUDED.cached_at,
                        credits_json         = EXCLUDED.credits_json,
                        production_cos_json  = EXCLUDED.production_cos_json,
                        runtime              = EXCLUDED.runtime,
                        number_of_seasons    = EXCLUDED.number_of_seasons,
                        number_of_episodes   = EXCLUDED.number_of_episodes,
                        original_language    = EXCLUDED.original_language,
                        original_title       = EXCLUDED.original_title,
                        backdrop_path        = EXCLUDED.backdrop_path,
                        tmdb_status          = EXCLUDED.tmdb_status,
                        vote_count           = EXCLUDED.vote_count,
                        text_backdrop_path   = EXCLUDED.text_backdrop_path,
                        original_poster_path = EXCLUDED.original_poster_path,
                        poster_langs_json    = EXCLUDED.poster_langs_json
                    """,
                    (
                        cache_key,
                        title,
                        release_year,
                        json.dumps(genre_ids),
                        int(is_textless),
                        poster_path,
                        json.dumps(logos),
                        int(time.time()),
                        json.dumps(credits or {}),
                        json.dumps(production_companies or []),
                        runtime,
                        number_of_seasons,
                        number_of_episodes,
                        original_language,
                        original_title,
                        backdrop_path,
                        tmdb_status,
                        vote_count,
                        text_backdrop_path,
                        original_poster_path,
                        json.dumps(poster_langs or {}),
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"TMDB metadata cache write error: {exc}")


def delete_cached_tmdb_metadata(cache_key: str) -> None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM tmdb_metadata_cache WHERE cache_key = %s",
                    (cache_key,),
                )
            conn.commit()
        logger.info(f"TMDB metadata cache invalidated for {cache_key}")
    except Exception as exc:
        logger.error(f"TMDB metadata cache delete error: {exc}")


# ---------------------------------------------------------------------------
# Digital release cache
# ---------------------------------------------------------------------------

def is_digital_release(imdb_id: str) -> bool:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM digital_release_cache WHERE imdb_id = %s",
                    (imdb_id,),
                )
                return cur.fetchone() is not None
    except Exception as exc:
        logger.error(f"Digital release cache lookup error: {exc}")
        return False


def count_digital_releases() -> int:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM digital_release_cache")
                (count,) = cur.fetchone()
                return count
    except Exception as exc:
        logger.error(f"Digital release cache count error: {exc}")
        return 0


def add_digital_releases(entries: list[tuple[str, int]]) -> int:
    if not entries:
        return 0
    inserted = 0
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                for imdb_id, posted_at in entries:
                    cur.execute(
                        """
                        INSERT INTO digital_release_cache (imdb_id, posted_at)
                        VALUES (%s, %s)
                        ON CONFLICT (imdb_id) DO NOTHING
                        """,
                        (imdb_id, posted_at),
                    )
                    if cur.rowcount:
                        inserted += cur.rowcount
            conn.commit()
    except Exception as exc:
        logger.error(f"Digital release cache write error: {exc}")
    return inserted


# ---------------------------------------------------------------------------
# Release status cache
# ---------------------------------------------------------------------------
# Cached separately from main metadata so the extra TMDB /release_dates call
# only happens for users who have enabled the "release_status" sash slot.
# TTL: 7 days — status changes slowly (Cinema → Streaming → BluRay is one-way).

def get_cached_release_status(cache_key: str) -> str | None:
    """Return the cached release status string, or None if absent / expired."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, cached_at FROM release_status_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                status, cached_at = row
                age_days = (time.time() - cached_at) / 86400
                if age_days > _RELEASE_STATUS_TTL_DAYS:
                    logger.info(f"Release status cache expired for {cache_key} ({age_days:.1f}d old)")
                    return None
                return status
    except Exception as exc:
        logger.error(f"Release status cache read error: {exc}")
        return None


def set_cached_release_status(cache_key: str, status: str) -> None:
    """Upsert a release status entry."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO release_status_cache (cache_key, status, cached_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        status    = EXCLUDED.status,
                        cached_at = EXCLUDED.cached_at
                    """,
                    (cache_key, status, int(time.time())),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Release status cache write error: {exc}")


def get_cached_text_detection(cache_key: str) -> bool | None:
    """Return the cached burned-in-text result (True/False), or None if absent.

    Results never expire — they're keyed by an immutable TMDB image path plus the
    detection params, so the answer can't change for a given key.
    """
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT has_text FROM text_detection_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                row = cur.fetchone()
                return None if row is None else bool(row[0])
    except Exception as exc:
        logger.error(f"Text-detection cache read error: {exc}")
        return None


def set_cached_text_detection(cache_key: str, has_text: bool) -> None:
    """Upsert a burned-in-text detection result."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO text_detection_cache (cache_key, has_text, cached_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        has_text  = EXCLUDED.has_text,
                        cached_at = EXCLUDED.cached_at
                    """,
                    (cache_key, int(has_text), int(time.time())),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Text-detection cache write error: {exc}")


# ---------------------------------------------------------------------------
# Cache stats
# ---------------------------------------------------------------------------

def get_cache_stats() -> dict:
    """
    Return row counts for every cache table.  Used by the /stats endpoint so
    operators can see cache health at a glance.  Never raises.

    composite_bytes / db_file_bytes are not meaningful for the Postgres
    backend (composite bytes live in the blobstore, and the DB is a shared
    server with no single file size to report), so both are reported as None
    to keep the response shape identical to the SQLite backend.
    """
    stats: dict = {}
    try:
        with _get_pool().connection() as conn:
            for table in (
                "rating_cache", "quality_cache", "trending_cache",
                "tmdb_metadata_cache", "final_poster_cache",
                "digital_release_cache", "release_status_cache",
                "text_detection_cache",
            ):
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        (n,) = cur.fetchone()
                    stats[table] = n
                except Exception:
                    # A failed COUNT aborts the transaction; roll back so the
                    # next table's query runs on a clean connection.
                    conn.rollback()
                    stats[table] = None

        # Composite bytes live in the blobstore, not Postgres — the relational
        # backend only holds metadata rows. And there is no single DB file to
        # stat on a shared server. Reported as None to match the SQLite shape.
        stats["composite_bytes"] = None
        stats["db_file_bytes"] = None
    except Exception as exc:
        logger.error(f"Cache stats error: {exc}")
    return stats


def get_cached_imdb_to_tmdb(imdb_id: str, media_type: str) -> str | None:
    """Phase 11: look up the cached tmdb_id for an imdb_id + media_type."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tmdb_id FROM imdb_to_tmdb_cache "
                    "WHERE imdb_id = %s AND media_type = %s",
                    (imdb_id, media_type),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as exc:
        logger.error(f"imdb_to_tmdb cache read error: {exc}")
        return None


def set_cached_imdb_to_tmdb(imdb_id: str, media_type: str, tmdb_id: str) -> None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO imdb_to_tmdb_cache (imdb_id, media_type, tmdb_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (imdb_id, media_type) DO UPDATE SET tmdb_id = EXCLUDED.tmdb_id
                    """,
                    (imdb_id, media_type, tmdb_id),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"imdb_to_tmdb cache write error: {exc}")


def ping() -> bool:
    """Cheap connectivity check for /ready probes (Phase 4)."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False


def close() -> None:
    """Close the pool — called from lifespan shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
