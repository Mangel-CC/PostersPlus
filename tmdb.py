#tmdb.py
import asyncio
import io
import logging
import os
import httpx
import numpy as np
import re

logger = logging.getLogger(__name__)
from PIL import Image, ImageDraw

from cache import (
    get_cached_trending_snapshot,
    set_cached_trending_snapshot,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
    get_cached_tmdb_metadata,
    set_cached_tmdb_metadata,
    get_cached_imdb_to_tmdb,
    set_cached_imdb_to_tmdb,
)

from config import (
    POSTER_WIDTH,
    POSTER_HEIGHT,
    LOGO_MAX_W_RATIO,
    LOGO_MAX_H_RATIO,
    LOGO_BOTTOM_RATIO,
)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def normalise_poster(image: Image.Image) -> Image.Image:
    target_w, target_h = POSTER_WIDTH, POSTER_HEIGHT
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    image = image.resize((new_w, new_h), Image.LANCZOS)
    left = round((new_w - target_w) / 2)
    top  = round((new_h - target_h) / 2)
    return image.crop((left, top, left + target_w, top + target_h))


def ensure_light_logo(logo: Image.Image, threshold: float = 0.2) -> Image.Image:
    """
    If the visible pixels of *logo* are too dark, force them all to white.
    Uses numpy for vectorised luminance calculation — avoids materialising
    a Python list of per-pixel tuples.
    """
    rgba = np.array(logo.convert("RGBA"), dtype=np.float32)   # H×W×4
    alpha = rgba[:, :, 3]
    visible_mask = alpha > 30                                  # boolean H×W

    if not visible_mask.any():
        return logo

    r = rgba[:, :, 0][visible_mask]
    g = rgba[:, :, 1][visible_mask]
    b = rgba[:, :, 2][visible_mask]
    avg_lum = (0.2126 * r + 0.7152 * g + 0.0722 * b).mean() / 255.0

    if avg_lum > threshold:
        return logo

    # Force visible pixels to white, preserve alpha channel
    out = rgba.copy()
    out[:, :, 0][visible_mask] = 255
    out[:, :, 1][visible_mask] = 255
    out[:, :, 2][visible_mask] = 255
    return Image.fromarray(out.astype(np.uint8), "RGBA")


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def fetch_poster_metadata(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
) -> tuple[list[int], bool, list[dict], str | None, str, str, str | None, dict]:
    """
    Fetch (or return cached) TMDB metadata, including credits,
    production_companies, and original_language for discovery sash logic.

    Returns:
        (genre_ids, is_textless, logos, release_year, title, poster_path,
         backdrop_path, tmdb_data)

    backdrop_path is a textless landscape backdrop ready to be centre-cropped
    to portrait when no textless poster exists (handled by main.py). None when
    TMDB has no language-neutral backdrop for the title.
    """
    endpoint = "tv" if media_type in ("tv", "series") else "movie"
    metadata_cache_key = f"{endpoint}_{tmdb_id}"

    meta = get_cached_tmdb_metadata(metadata_cache_key)

    if meta:
        logger.info(f"TMDB metadata cache hit for {tmdb_id}")
        tmdb_data = {
            "credits":               meta.get("credits", {}),
            "production_companies":  meta.get("production_companies", []),
            "original_language":     meta.get("original_language"),
            "runtime":               meta.get("runtime"),
            "number_of_seasons":     meta.get("number_of_seasons"),
            "number_of_episodes":    meta.get("number_of_episodes"),
        }
        return (
            meta["genre_ids"],
            meta["is_textless"],
            meta["logos"],
            meta["release_year"],
            meta["title"],
            meta["poster_path"],
            meta.get("backdrop_path"),
            tmdb_data,
        )

    logger.info(f"External API Call: Requested meta from TMDB for {tmdb_id}")
    import upstream
    resp = await upstream.request(
        client, "GET",
        f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
        service=upstream.SVC_TMDB,
        params={
            "api_key": tmdb_key,
            "append_to_response": "images,credits",
            # null = TMDB's signal for language-neutral entries (textless),
            # which is what we want for both posters and backdrops.
            "include_image_language": "en,null",
        },
    )
    data = resp.json()

    title = (
        data.get("title")
        or data.get("name")
        or data.get("original_title")
        or data.get("original_name")
        or "Unknown Title"
    )

    raw_date = data.get("release_date") or data.get("first_air_date") or ""
    release_year: str | None = raw_date[:4] if len(raw_date) >= 4 else None

    images    = data.get("images", {})
    posters   = images.get("posters", [])
    logos     = images.get("logos", [])
    backdrops = images.get("backdrops", [])

    # iso_639_1 is None (JSON null) for most textless entries; older TMDB
    # records occasionally use "" for the same thing — accept both.
    textless = [p for p in posters if p.get("iso_639_1") in (None, "")]

    if textless:
        best = max(textless, key=lambda x: x.get("vote_average", 0))
        poster_path = best["file_path"]
        is_textless = True
    else:
        poster_path = data.get("poster_path")
        is_textless = False

    if not poster_path:
        logger.warning(f"No poster image on TMDB for tmdb_id={tmdb_id} — backdrop or fallback canvas will be served")
        is_textless = False  # no art, no point fetching logos
        # poster_path stays None; main.py picks backdrop_path or generates a canvas

    # Best textless backdrop. Only consider null/unspecified-language entries —
    # backdrops with an explicit language tag frequently have title text burned
    # in. None if no neutral backdrop exists, which suppresses the fallback.
    backdrop_candidates = [b for b in backdrops if b.get("iso_639_1") in (None, "")]
    if backdrop_candidates:
        best_backdrop = max(backdrop_candidates, key=lambda x: x.get("vote_average", 0))
        backdrop_path: str | None = best_backdrop["file_path"]
    else:
        backdrop_path = None

    genre_ids            = [g["id"] for g in data.get("genres", [])]
    credits              = data.get("credits", {})
    production_companies = data.get("production_companies", [])
    original_language    = data.get("original_language")
    runtime              = data.get("runtime")
    number_of_seasons    = data.get("number_of_seasons")
    number_of_episodes   = data.get("number_of_episodes")

    set_cached_tmdb_metadata(
        metadata_cache_key,
        title,
        release_year,
        genre_ids,
        is_textless,
        poster_path,
        logos,
        credits=credits,
        production_companies=production_companies,
        original_language=original_language,
        runtime=runtime,
        number_of_seasons=number_of_seasons,
        number_of_episodes=number_of_episodes,
        backdrop_path=backdrop_path,
    )

    tmdb_data = {
        "credits":              credits,
        "production_companies": production_companies,
        "original_language":    original_language,
        "runtime":              runtime,
        "number_of_seasons":    number_of_seasons,
        "number_of_episodes":   number_of_episodes,
    }

    return genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data


async def resolve_imdb_to_tmdb(
    client: httpx.AsyncClient,
    imdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
) -> str | None:
    """Phase 11: resolve an imdb_id (tt...) to a TMDB id for the given media
    type. Cached forever — TMDB ids are stable. Returns None on miss.

    Public preset URLs only carry the imdb_id (the universal Stremio
    identifier); this resolver runs once per (imdb_id, media_type) and
    every subsequent /p hit reads from cache.
    """
    cached = get_cached_imdb_to_tmdb(imdb_id, media_type)
    if cached is not None:
        return cached

    endpoint = "tv_results" if media_type in ("tv", "series") else "movie_results"
    url = f"https://api.themoviedb.org/3/find/{imdb_id}"
    try:
        resp = await client.get(url, params={"api_key": tmdb_key, "external_source": "imdb_id"})
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning(f"imdb_to_tmdb resolve failed for {imdb_id}: {exc}")
        return None

    results = data.get(endpoint) or []
    if not results:
        logger.info(f"imdb_to_tmdb: no {media_type} match for {imdb_id}")
        return None

    tmdb_id = str(results[0].get("id") or "")
    if not tmdb_id:
        return None

    set_cached_imdb_to_tmdb(imdb_id, media_type, tmdb_id)
    return tmdb_id


async def fetch_poster_image(
    client: httpx.AsyncClient,
    tmdb_id: str,
    media_type: str,
    poster_path: str,
) -> Image.Image:
    """
    Fetch and cache the base poster image.

    Disk cache format is JPEG (q=92 RGB) rather than PNG:
      - ~4-5x faster decode on cache hit
      - ~5x smaller on disk
      - Imperceptible quality difference for photographic poster art
    The image is returned as RGBA so the compositing pipeline can use
    alpha_composite throughout without mode-checking.
    """
    poster_cache_key = f"{media_type}_{tmdb_id}_{poster_path.strip('/')}"
    cached_bytes = get_cached_tmdb_poster(poster_cache_key)

    if cached_bytes:
        logger.info(f"TMDB poster cache hit for {tmdb_id}")
        # Stored as JPEG RGB — convert to RGBA for the compositing pipeline
        image = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        if image.size != (POSTER_WIDTH, POSTER_HEIGHT):
            image = normalise_poster(image)
        return image

    logger.info(f"External API Call: Requested poster from TMDB for {tmdb_id}")
    img_resp = await client.get(f"https://image.tmdb.org/t/p/w500{poster_path}")
    img_resp.raise_for_status()
    image = Image.open(io.BytesIO(img_resp.content)).convert("RGBA")
    image = normalise_poster(image)

    # Save as JPEG RGB (no alpha needed for base poster; restoring alpha on load is free)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(poster_cache_key, buf.getvalue())

    return image


async def fetch_backdrop_image(
    client: httpx.AsyncClient,
    tmdb_id: str,
    backdrop_path: str,
) -> Image.Image:
    """
    Fetch, centre-crop, and cache a TMDB backdrop as a portrait poster.

    Backdrops are 16:9 landscape; we take the full height and cut a centred
    2:3 strip, giving a clean textless portrait without any AI inpainting.
    Cached under the same JPEG scheme as regular posters so subsequent hits
    skip the upstream call. Used by main.py when fetch_poster_metadata
    returns poster_path=None (no textless or default poster on TMDB).
    """
    cache_key = f"backdrop_{tmdb_id}_{backdrop_path.strip('/')}"
    cached_bytes = get_cached_tmdb_poster(cache_key)

    if cached_bytes:
        logger.info(f"TMDB backdrop cache hit for {tmdb_id}")
        image = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        if image.size != (POSTER_WIDTH, POSTER_HEIGHT):
            image = normalise_poster(image)
        return image

    # w1280 gives enough resolution to crop to a quality portrait
    logger.info(f"External API Call: Requested backdrop from TMDB for {tmdb_id}")
    img_resp = await client.get(f"https://image.tmdb.org/t/p/w1280{backdrop_path}")
    img_resp.raise_for_status()
    image = Image.open(io.BytesIO(img_resp.content)).convert("RGBA")

    # Centre-crop 16:9 → 2:3: keep full height, take a centred vertical strip
    w, h = image.size
    crop_w = int(h * 2 / 3)
    if crop_w < w:
        left = (w - crop_w) // 2
        image = image.crop((left, 0, left + crop_w, h))

    image = normalise_poster(image)

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())

    return image


async def fetch_logo(
    client: httpx.AsyncClient,
    logos: list[dict],
    logo_language: str = "en",
) -> Image.Image | None:

    preferred = [
        lg for lg in logos
        if lg["file_path"].endswith(".png")
        and lg.get("iso_639_1") == logo_language
    ]

    english = [
        lg for lg in logos
        if lg["file_path"].endswith(".png")
        and lg.get("iso_639_1") == "en"
    ]

    neutral = [
        lg for lg in logos
        if lg["file_path"].endswith(".png")
        and lg.get("iso_639_1") is None
    ]

    candidates = preferred or neutral or english

    candidates = sorted(
        candidates,
        key=lambda x: x.get("vote_average", 0),
        reverse=True,
    )

    if not candidates:
        return None

    logo_path = candidates[0]["file_path"]

    logo_cache_key = logo_path.strip('/').replace('/', '_')
    cached_bytes = get_cached_tmdb_logo(logo_cache_key)

    if cached_bytes:
        logger.info("TMDB logo cache hit")
        logo = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        return logo

    resp = await client.get(f"https://image.tmdb.org/t/p/w500{logo_path}")
    logger.info(f"External API Call: Requested logo from TMDB")
    resp.raise_for_status()

    logo = Image.open(io.BytesIO(resp.content)).convert("RGBA")

    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)

    logo = ensure_light_logo(logo)

    buf = io.BytesIO()
    logo.save(buf, format="PNG")
    set_cached_tmdb_logo(logo_cache_key, buf.getvalue())

    return logo


async def fetch_trending_rank(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
) -> int | None:

    endpoint = "tv" if media_type in ("tv", "series") else "movie"

    snapshot = get_cached_trending_snapshot(endpoint)

    if snapshot is None:
        logger.info("External API Call: Refreshing TMDB trending snapshot (pages 1+2 concurrent)")

        async def _fetch_page(page: int) -> list[dict]:
            resp = await client.get(
                f"https://api.themoviedb.org/3/trending/{endpoint}/day",
                params={"api_key": tmdb_key, "page": page},
            )
            resp.raise_for_status()
            return resp.json().get("results", [])

        try:
            page1_results, page2_results = await asyncio.gather(
                _fetch_page(1),
                _fetch_page(2),
            )
        except Exception as exc:
            logger.error(f"TMDB trending fetch error: {exc}")
            return None

        rankings: dict[str, int] = {}
        for i, item in enumerate(page1_results, start=1):
            rankings[str(item["id"])] = i
        for i, item in enumerate(page2_results, start=len(page1_results) + 1):
            rankings[str(item["id"])] = i

        set_cached_trending_snapshot(endpoint, rankings)
        snapshot = rankings

    rank = snapshot.get(str(tmdb_id))

    if rank:
        logger.info(f"Trending rank for {tmdb_id}: #{rank}")

    return rank


# ---------------------------------------------------------------------------
# Logo rendering (onto poster)
# ---------------------------------------------------------------------------

def composite_logo(
    image: Image.Image,
    logo: Image.Image,
    *,
    max_w_ratio: float = LOGO_MAX_W_RATIO,
    max_h_ratio: float = LOGO_MAX_H_RATIO,
    bottom_ratio: float = LOGO_BOTTOM_RATIO,
) -> None:
    width, height = image.size

    max_w = int(width  * max_w_ratio)
    max_h = int(height * max_h_ratio)

    logo.thumbnail((max_w, max_h), Image.LANCZOS)

    alpha_bbox = logo.getchannel("A").getbbox()
    if alpha_bbox:
        logo = logo.crop(alpha_bbox)

    logo_x = round((width - logo.width) / 2)
    logo_y = height - int(height * bottom_ratio) - logo.height

    image.paste(logo, (logo_x, logo_y), logo)