"""Badge LAT en las imagenes de capitulos (2026-11-16, Nuvio via AIOMetadata).

/still?src=<imagen del capitulo>&tmdb=<serie>&s=<temporada>&e=<episodio> pregunta a bitmagnet-web
(/internal/latep) si ese episodio tiene audio latino ("audio") o solo subtitulos latinos ("subs") y
dibuja en la esquina superior izquierda el mismo badge del poster (LAT con AUDIO/SUBS debajo, diseno A:
sin caja, con sombra suave para que se lea en fondos claros). Sin latino: redirige a la imagen original."""
import io
import os
import time
from collections import OrderedDict
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageFilter

import pill_layout as pl

LAT_EP_URL = os.environ.get("LAT_EP_URL", "http://bitmagnet-web:8097/internal/latep")
ALLOWED_HOSTS = {"image.tmdb.org", "artworks.thetvdb.com", "thetvdb.com", "www.thetvdb.com", "images.metahub.space",
                 "episodes.metahub.space", "img.anili.st", "media.kitsu.app", "media.kitsu.io", "s4.anilist.co", "cdn.myanimelist.net"}
_lat_cache: dict[tuple[str, int, int], tuple[float, str | None, bool]] = {}
_img_cache: "OrderedDict[tuple[str, str], bytes]" = OrderedDict()
_IMG_MAX = 600


def allowed(src: str) -> bool:
    try:
        u = urlparse(src)
        return u.scheme in ("http", "https") and (u.hostname or "") in ALLOWED_HOSTS
    except Exception:
        return False


async def fetch_lat_ep(client: httpx.AsyncClient, tmdb: str, s: int, e: int) -> tuple[str | None, bool]:
    """(estado, verificado): estado "audio"|"subs"|None; verificado = hay un torrent verificado de ese episodio."""
    key = (str(tmdb), int(s), int(e))
    now = time.monotonic()
    hit = _lat_cache.get(key)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    try:
        r = await client.get(LAT_EP_URL, params={"tmdb": str(tmdb), "s": int(s), "e": int(e)}, timeout=20.0)
        r.raise_for_status()
        data = r.json()
        val = data.get("lat")
        val = val if val in ("audio", "subs") else None
        ver = bool(data.get("verified")) and val is not None
        _lat_cache[key] = (now + 3600, val, ver)
    except Exception:
        _lat_cache[key] = (now + 60, None, False)
        return None, False
    if len(_lat_cache) > 20000:
        for k in list(_lat_cache)[:5000]:
            _lat_cache.pop(k, None)
    return val, ver


def draw(raw: bytes, lat: str, ver: bool = False) -> bytes:
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    w, h = im.size
    size = max(14, int(h * 0.11))
    badge, _ = pl._lat_stack(lat, size, ver)
    # Nuvio recorta los costados de la tarjeta (~5-6 % por lado) y redondea esquinas: margen generoso
    # (en la primera version quedaba "AT AUDIO", 2026-11-16)
    px, py = int(w * 0.085), int(h * 0.09)
    glow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rectangle([0, 0, px + badge.width + int(h * 0.06), py + badge.height + int(h * 0.06)], fill=(0, 0, 0, 140))
    glow = glow.filter(ImageFilter.GaussianBlur(max(2, int(h * 0.06))))
    im = Image.alpha_composite(im, glow)
    im.alpha_composite(badge, (px, py))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


async def render(client: httpx.AsyncClient, src: str, lat: str, ver: bool = False) -> bytes:
    key = (src, lat, ver)
    if key in _img_cache:
        _img_cache.move_to_end(key)
        return _img_cache[key]
    r = await client.get(src, timeout=12.0, follow_redirects=True)
    r.raise_for_status()
    data = draw(r.content, lat, ver)
    _img_cache[key] = data
    while len(_img_cache) > _IMG_MAX:
        _img_cache.popitem(last=False)
    return data
