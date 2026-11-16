"""Badge "LAT" (layout=pill): pregunta al indexador propio (bitmagnet-web, /internal/lat) si el titulo
tiene un torrent verificado con audio en espanol latino ("audio") o solo subtitulos latinos ("subs").

Se consulta por la red interna de Docker; cache en memoria 1 h (el indexador guarda 6-24 h). Si el
indexador no responde, no se dibuja nada y tampoco se cambia la clave del composite."""
import os
import time

import httpx

LAT_STATUS_URL = os.environ.get("LAT_STATUS_URL", "http://bitmagnet-web:8097/internal/lat")
_TTL_OK, _TTL_ERR = 3600.0, 600.0
_cache: dict[tuple[str, str], tuple[float, str | None, list[str] | None, bool]] = {}
_dub: dict[tuple[str, str], tuple | None] = {}   # doblaje en curso: (temporada, episodio, nuevo)
_ver: dict[tuple[str, str], bool] = {}            # escudo: pelicula verificada / serie con todo lo emitido verificado
_QUALITY_OK = {"4K", "1080P", "DV", "HDR10+", "HDR10", "ATMOS", "TRUEHD", "DDP", "REMUX", "DTS-HD", "DTSX", "DTS", "5.1", "7.1"}


async def fetch_lat(client: httpx.AsyncClient, media_type: str, tmdb_id: str) -> tuple[str | None, list[str] | None, bool]:
    """Devuelve (estado, calidad, ok): estado = "audio" | "subs" | None; calidad = tokens del mejor
    torrent con audio latino (ej. ["4K", "DV", "ATMOS"]) o None; ok = False si el indexador fallo."""
    if not LAT_STATUS_URL or not tmdb_id:
        return None, None, False
    kind = "movie" if media_type == "movie" else "tv"
    key = (kind, str(tmdb_id))
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1], hit[2], hit[3]
    try:
        r = await client.get(LAT_STATUS_URL, params={"type": kind, "tmdb": str(tmdb_id)}, timeout=4.0)
        r.raise_for_status()
        data = r.json()
        val = data.get("lat") if data.get("lat") in ("audio", "subs") else None
        q = [t for t in (data.get("quality") or []) if t in _QUALITY_OK] or None
        d = data.get("dub") or None
        _dub[key] = (int(d["s"]), int(d["e"]), bool(d.get("nuevo"))) if d and val == "audio" else None
        _ver[key] = bool(data.get("ver")) and val is not None
        _cache[key] = (now + _TTL_OK, val, q, True)
        return val, q, True
    except Exception:
        _cache[key] = (now + _TTL_ERR, None, None, False)
        return None, None, False



def lat_dub(media_type: str, tmdb_id: str) -> tuple | None:
    """Doblaje latino en curso del ultimo fetch_lat de ese titulo: (temporada, episodio, nuevo) o None."""
    return _dub.get(("movie" if media_type == "movie" else "tv", str(tmdb_id)))


def lat_ver(media_type: str, tmdb_id: str) -> bool:
    """True si el titulo esta verificado (escudo junto al LAT)."""
    return _ver.get(("movie" if media_type == "movie" else "tv", str(tmdb_id)), False)
