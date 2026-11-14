"""Badge "LAT" (layout=pill): pregunta al indexador propio (bitmagnet-web, /internal/lat) si el titulo
tiene un torrent verificado con audio en espanol latino ("audio") o solo subtitulos latinos ("subs").

Se consulta por la red interna de Docker; cache en memoria 1 h (el indexador guarda 6-24 h). Si el
indexador no responde, no se dibuja nada y tampoco se cambia la clave del composite."""
import os
import time

import httpx

LAT_STATUS_URL = os.environ.get("LAT_STATUS_URL", "http://bitmagnet-web:8097/internal/lat")
_TTL_OK, _TTL_ERR = 3600.0, 600.0
_cache: dict[tuple[str, str], tuple[float, str | None, bool]] = {}


async def fetch_lat(client: httpx.AsyncClient, media_type: str, tmdb_id: str) -> tuple[str | None, bool]:
    """Devuelve (estado, ok): estado = "audio" | "subs" | None; ok = False si el indexador fallo."""
    if not LAT_STATUS_URL or not tmdb_id:
        return None, False
    kind = "movie" if media_type == "movie" else "tv"
    key = (kind, str(tmdb_id))
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1], hit[2]
    try:
        r = await client.get(LAT_STATUS_URL, params={"type": kind, "tmdb": str(tmdb_id)}, timeout=2.0)
        r.raise_for_status()
        val = r.json().get("lat")
        val = val if val in ("audio", "subs") else None
        _cache[key] = (now + _TTL_OK, val, True)
        return val, True
    except Exception:
        _cache[key] = (now + _TTL_ERR, None, False)
        return None, False


def lat_text(status: str | None) -> str | None:
    return {"audio": "LAT", "subs": "SUB LAT"}.get(status or "")
