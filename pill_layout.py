"""Layout "pill" (layout=pill): pastilla arriba a la izquierda con el sash, badges de calidad
arriba a la derecha, desenfoque oscuro abajo con el logo encima y "Genero • ★ nota" debajo.

Todo se dimensiona respecto al ancho del poster (referencia 500x750). La pastilla nunca invade
los badges: primero se achica la fuente y, si aun no cabe, se corta el texto con "…".
"""
from __future__ import annotations

import colorsys
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from tmdb import LOGO_ABS_MAX_H, composite_logo
import config as _cfg

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_BOLD = os.path.join(_FONTS_DIR, "Inter-Bold.ttf")        # texto inferior (tiene ★)
_PILL_FONT = os.path.join(_FONTS_DIR, "Arimo-Bold.ttf")  # pastilla y badges (estilo Arial)
_REGULAR = os.path.join(_FONTS_DIR, "Arimo-Regular.ttf")  # logo de Dolby Vision

SS = 3  # supersample de pastilla y badges (bordes redondeados limpios)

# Geometria en fraccion del ancho (medida sobre el poster de referencia de 500px).
MARGIN       = 0.056   # margen superior/lateral de pastilla y badges
PILL_H       = 0.114
PILL_PAD_X   = 0.058
PILL_FONT    = 0.066
PILL_MIN_FONT = 0.050
PILL_2L_FONT = 0.054   # fuente de la pastilla en dos lineas
PILL_MIN_PAD = 0.60    # relleno minimo (fraccion del normal) al achicar la pastilla
BADGE_H      = 0.078
BADGE_PAD_X  = 0.020
BADGE_FONT   = 0.060
BADGE_GAP    = 0.016
ITEM_GAP     = 0.030   # separacion minima pastilla <-> badges
TOP_RANK     = 25      # puestos del top de hoy de TMDB que se muestran en la pastilla
CENTER_SCALE = 1.18
TAB_H        = 0.136   # pestaña del catalogo Top (fraccion del ancho)
TAB_PAD_X    = 0.072
TAB_FONT     = 0.080    # badges centrados (sin pastilla) un poco mas grandes
LABEL_FONT   = 0.076
LABEL_CY     = 0.896   # centro vertical de "Genero • ★ nota" (fraccion del alto)
LABEL_COLOR  = (150, 154, 163, 255)

BLUR_START   = 0.58    # donde empieza el desenfoque (fraccion del alto)
BLUR_FULL    = 0.80    # donde ya es desenfoque completo
LOGO_CY      = 0.715   # centro vertical del logo
LOGO_MAX_W   = 0.78
LOGO_MAX_H   = 0.20

DARK_PILL = (27, 27, 28)

_RES_TOKENS = ("4K", "1080P")


def _font(size: int, path: str = _BOLD) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, max(1, size))
    except OSError:
        return ImageFont.load_default()


def _accent_rgb(image: Image.Image) -> tuple[int, int, int]:
    """Color de la pastilla: el tono que domina la franja superior del poster (donde va la
    pastilla), tal cual. Si esa franja es oscura y apagada, gris casi negro."""
    w, h = image.size
    band = image.convert("RGB").crop((0, 0, w, int(h * 0.22))).resize((60, 18), Image.BILINEAR)
    px = np.asarray(band, dtype=np.float32).reshape(-1, 3)
    # k-means pequeño (k=4) para quedarnos con el color mas abundante, no con un promedio turbio.
    rng = np.random.default_rng(0)
    cent = px[rng.choice(len(px), 4, replace=False)]
    for _ in range(8):
        lab = np.argmin(((px[:, None, :] - cent[None]) ** 2).sum(-1), axis=1)
        for k in range(4):
            if (lab == k).any():
                cent[k] = px[lab == k].mean(axis=0)
    counts = np.bincount(lab, minlength=4)
    r, g, b = cent[counts.argmax()]
    hh, ss, vv = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if vv < 0.30 and ss < 0.45:
        return DARK_PILL
    return int(r), int(g), int(b)


def _text_on(rgb: tuple[int, int, int]) -> tuple[int, int, int, int]:
    lum = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255
    return (255, 255, 255, 255) if lum < 0.50 else (22, 22, 24, 255)


def _bottom_fade(image: Image.Image) -> Image.Image:
    """Desenfoque progresivo + oscurecido teñido con el color de la parte baja del poster."""
    w, h = image.size
    base = image.convert("RGBA")
    blurred = base.filter(ImageFilter.GaussianBlur(radius=max(4, int(h * 0.022))))

    y0, y1 = int(h * BLUR_START), int(h * BLUR_FULL)
    ramp = np.zeros(h, dtype=np.float32)
    ramp[y0:y1] = np.linspace(0, 1, max(1, y1 - y0), dtype=np.float32) ** 1.4
    ramp[y1:] = 1.0
    blur_mask = Image.fromarray((np.broadcast_to(ramp[:, None], (h, w)) * 255).astype(np.uint8), "L")
    out = Image.composite(blurred, base, blur_mask)

    strip = np.asarray(blurred.convert("RGB").crop((0, int(h * 0.82), w, h)), dtype=np.float32)
    tint = tuple(int(c * 0.20) for c in strip.reshape(-1, 3).mean(axis=0))
    dark = np.zeros(h, dtype=np.float32)
    d0 = int(h * (BLUR_START - 0.04))
    dark[d0:] = np.linspace(0, 1, h - d0, dtype=np.float32) ** 1.2 * 0.93
    dark_mask = Image.fromarray((np.broadcast_to(dark[:, None], (h, w)) * 255).astype(np.uint8), "L")
    out.paste(Image.new("RGBA", (w, h), (*tint, 255)), (0, 0), dark_mask)
    return out


def _rounded(size: tuple[int, int], radius: int, fill) -> Image.Image:
    w, h = size
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), radius=radius * SS, fill=fill)
    return big.resize((w, h), Image.LANCZOS)


def _draw_centered_text(layer: Image.Image, box: tuple[int, int, int, int], text: str,
                        font: ImageFont.FreeTypeFont, fill) -> None:
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = box
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    tx = x0 + (x1 - x0 - (r - l)) / 2 - l
    ty = y0 + (y1 - y0 - (b - t)) / 2 - t
    d.text((tx, ty), text, font=font, fill=fill)


_DOLBY_AUDIO = (("ATMOS", "ATMOS"), ("TRUEHD", "TRUEHD"), ("DDP", "DIGITAL"))


def _res_token(tokens: list[str]) -> str | None:
    return next((t for t in _RES_TOKENS if t in tokens), None)


def _dolby_line(tokens: list[str]) -> str | None:
    """Segunda linea del logo de Dolby: audio Dolby y/o VISION (ej. "ATMOS · VISION")."""
    parts = [word for tok, word in _DOLBY_AUDIO if tok in tokens][:1]
    if "DV" in tokens:
        parts.append("VISION")
    return " · ".join(parts) if parts else None


def _dolby_logo(sub: str, height: int) -> Image.Image:
    """Logo blanco sin fondo: doble D + "Dolby" arriba y *sub* espaciado abajo."""
    H = height * SS
    top_h = int(H * 0.56)                       # alto de la linea "DD Dolby"
    f_dolby = _font(int(top_h * 1.05), _REGULAR)
    f_sub = _font(int(H * 0.30), _REGULAR)
    track = int(H * 0.085)                      # espaciado entre letras de la segunda linea
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    sub_w = sum(tmp.textlength(c, font=f_sub) for c in sub) + track * (len(sub) - 1)
    l, t, r, b = tmp.textbbox((0, 0), "Dolby", font=f_dolby)
    sym_h = int(top_h * 0.86)
    sym_w = int(sym_h * 1.32)
    gap = int(top_h * 0.22)
    top_w = sym_w + gap + (r - l)
    W = int(max(top_w, sub_w)) + 2
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 235)
    # Doble D: dos "D" con las curvas hacia el centro.
    x0 = (W - top_w) // 2
    y0 = (top_h - sym_h) // 2
    half = sym_w // 2 - max(1, sym_w // 22)
    d.rectangle((x0, y0, x0 + half // 2, y0 + sym_h), fill=white)
    d.pieslice((x0, y0, x0 + half, y0 + sym_h), -90, 90, fill=white)
    x1 = x0 + sym_w - half
    d.rectangle((x1 + half // 2, y0, x1 + half, y0 + sym_h), fill=white)
    d.pieslice((x1, y0, x1 + half, y0 + sym_h), 90, 270, fill=white)
    d.text((x0 + sym_w + gap - l, (top_h - (b - t)) // 2 - t), "Dolby", font=f_dolby, fill=white)
    sl, st, sr, sb = tmp.textbbox((0, 0), sub, font=f_sub)
    x = (W - sub_w) / 2
    y = top_h + (H - top_h - (sb - st)) // 2 - st
    for c in sub:
        d.text((x, y), c, font=f_sub, fill=white)
        x += tmp.textlength(c, font=f_sub) + track
    logo = img.resize((max(1, W // SS), height), Image.LANCZOS)
    # Sombra suave para que el logo blanco se lea sobre fondos claros.
    pad = max(2, height // 6)
    out = Image.new("RGBA", (logo.width + 2 * pad, logo.height + 2 * pad), (0, 0, 0, 0))
    shadow = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    shadow.putalpha(logo.getchannel("A").point(lambda a: int(a * 0.55)))
    out.alpha_composite(shadow, (pad, pad + 1))
    out = out.filter(ImageFilter.GaussianBlur(max(1, height // 12)))
    out.alpha_composite(logo, (pad, pad))
    return out


def _text_badge(text: str, bh: int, font_px: int) -> Image.Image:
    """Badge claro con el texto calado (transparente): a traves de las letras se ve el poster,
    como en btttr. "1080p" lleva la "p" mas chica."""
    font = _font(font_px * SS, _PILL_FONT)
    small = _font(int(font_px * 0.78) * SS, _PILL_FONT)
    main, suffix = (text[:-1], text[-1]) if text.endswith("p") and text[:-1].isdigit() else (text, "")
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    ml, mt, mr, mb = d.textbbox((0, 0), main, font=font, anchor="ls")
    sw = d.textlength(suffix, font=small) if suffix else 0
    tw = (mr - ml) + sw
    H = bh * SS
    W = int(tw + 2 * H * 0.25)
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, W - 1, H - 1), radius=int(H * 0.17), fill=245)
    x = (W - tw) / 2 - ml
    base_y = (H + (mb - mt)) / 2 - mb      # baseline que centra el texto principal
    md.text((x, base_y), main, font=font, fill=0, anchor="ls")
    if suffix:
        md.text((x + (mr - ml) + ml, base_y), suffix, font=small, fill=0, anchor="ls")
    letters = Image.new("L", (W, H), 0)
    ld = ImageDraw.Draw(letters)
    ld.text((x, base_y), main, font=font, fill=255, anchor="ls")
    if suffix:
        ld.text((x + (mr - ml) + ml, base_y), suffix, font=small, fill=255, anchor="ls")
    badge = Image.new("RGBA", (W, H), (236, 237, 241, 0))
    badge.putalpha(mask)
    # Dentro de las letras se ve el poster oscurecido (no solo transparente), para que se lean
    # tambien sobre fondos claros.
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shade.putalpha(letters.point(lambda a: int(a * 0.55)))
    badge.alpha_composite(shade)
    return badge.resize((max(1, W // SS), bh), Image.LANCZOS)


def _dolby_item(tokens: list[str], w: int, scale: float) -> Image.Image | None:
    line = _dolby_line(tokens)
    return _dolby_logo(line, int(w * BADGE_H * scale * 0.80)) if line else None


def _res_item(tokens: list[str], w: int, scale: float) -> Image.Image | None:
    res = _res_token(tokens)
    if not res:
        return None
    return _text_badge(_cfg.QUALITY_LABELS.get(res, res), int(w * BADGE_H * scale), int(w * BADGE_FONT * scale))


def _draw_badges(image: Image.Image, tokens: list[str], center_y: int, sash_text: str | None) -> int:
    """Badges arriba. Sin pastilla: logo Dolby + resolucion centrados y algo mas grandes.
    Con pastilla: en la misma fila, pegados a la derecha; si el texto de la pastilla no entra
    ni con la fuente minima, el logo Dolby se achica un poco.
    Devuelve la x del borde izquierdo que la pastilla no debe pasar."""
    w, _ = image.size
    free = w - int(w * MARGIN) + int(w * ITEM_GAP)
    if not sash_text:
        items = [i for i in (_dolby_item(tokens, w, CENTER_SCALE), _res_item(tokens, w, CENTER_SCALE)) if i]
        gap = int(w * BADGE_GAP * 1.6)
        x = (w - (sum(i.width for i in items) + gap * (len(items) - 1))) // 2
        for it in items:
            image.alpha_composite(it, (x, center_y - it.height // 2))
            x += it.width + gap
        return free
    right = w - int(w * MARGIN)
    gap = int(w * BADGE_GAP)
    res = _res_item(tokens, w, 1.0)
    dolby = None
    for scale in (1.0, 0.8):
        dolby = _dolby_item(tokens, w, scale)
        row = [i for i in (dolby, res) if i]
        row_w = sum(i.width for i in row) + gap * (len(row) - 1)
        if _pill_fits_one_line(sash_text, right - row_w, w):
            break
    row = [i for i in (dolby, res) if i]
    if not row:
        return free
    row_w = sum(i.width for i in row) + gap * (len(row) - 1)
    x = right - row_w
    for it in row:
        image.alpha_composite(it, (x, center_y - it.height // 2))
        x += it.width + gap
    return right - row_w


def _max_text_w(right_limit: int, w: int, pad: int) -> int:
    return right_limit - int(w * ITEM_GAP) - int(w * MARGIN) - 2 * pad


def _pill_fits_one_line(text: str, right_limit: int, w: int) -> bool:
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pad = int(w * PILL_PAD_X * PILL_MIN_PAD)
    return d.textlength(text, font=_font(int(w * PILL_MIN_FONT), _PILL_FONT)) <= _max_text_w(right_limit, w, pad)


def _split_two(text: str) -> list[str]:
    """Parte el texto en dos lineas lo mas parejas posible (por palabras)."""
    words = text.split()
    if len(words) < 2:
        return [text]
    best = min(range(1, len(words)),
               key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    return [" ".join(words[:best]), " ".join(words[best:])]


def _fit_pill_text(text: str, right_limit: int, w: int) -> tuple[list[str], ImageFont.FreeTypeFont, int]:
    """Texto completo siempre que se pueda: 1) fuente normal; 2) menos relleno y fuente mas chica;
    3) dos lineas; 4) solo como ultimo recurso, cortar con "…". Devuelve (lineas, fuente, relleno)."""
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    full_pad = int(w * PILL_PAD_X)
    min_pad = int(w * PILL_PAD_X * PILL_MIN_PAD)
    for size in range(int(w * PILL_FONT), int(w * PILL_MIN_FONT) - 1, -1):
        f = _font(size, _PILL_FONT)
        tw = d.textlength(text, font=f)
        # El relleno se va reduciendo junto con la fuente.
        t = (int(w * PILL_FONT) - size) / max(1, int(w * PILL_FONT) - int(w * PILL_MIN_FONT))
        pad = int(full_pad - (full_pad - min_pad) * t)
        if tw <= _max_text_w(right_limit, w, pad):
            return [text], f, pad
    lines = _split_two(text)
    if len(lines) == 2:
        for size in range(int(w * PILL_2L_FONT), int(w * PILL_MIN_FONT) - 1, -1):
            f = _font(size, _PILL_FONT)
            if max(d.textlength(x, font=f) for x in lines) <= _max_text_w(right_limit, w, min_pad):
                return lines, f, min_pad
    f = _font(int(w * PILL_MIN_FONT), _PILL_FONT)
    cut = text
    while cut and d.textlength(cut.rstrip() + "…", font=f) > _max_text_w(right_limit, w, min_pad):
        cut = cut[:-1]
    return ([cut.rstrip() + "…"] if cut else []), f, min_pad


def _draw_pill(image: Image.Image, text: str, right_limit: int) -> None:
    w, _ = image.size
    x0 = y0 = int(w * MARGIN)
    lines, font, pad = _fit_pill_text(text, right_limit, w)
    if not lines:
        return
    d = ImageDraw.Draw(image)
    tw = int(max(d.textlength(x, font=font) for x in lines))
    pw = tw + 2 * pad
    ph = int(w * PILL_H)
    if len(lines) == 2:
        line_h = int(font.size * 1.12)
        ph = max(ph, 2 * line_h + int(w * 0.03))
    rgb = _accent_rgb(image)
    pill = _rounded((pw, ph), int(w * PILL_H * 0.16), (*rgb, 255))
    if len(lines) == 1:
        _draw_centered_text(pill, (0, 0, pw, ph), lines[0], font, _text_on(rgb))
    else:
        half = ph // 2
        _draw_centered_text(pill, (0, 0, pw, half + int(w * 0.004)), lines[0], font, _text_on(rgb))
        _draw_centered_text(pill, (0, half - int(w * 0.004), pw, ph), lines[1], font, _text_on(rgb))
    image.alpha_composite(pill, (x0, y0))


def _tab_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """La pestaña va sobre el mismo fondo del que sale su color: se separa un poco del fondo
    (mas saturada y algo mas oscura si es clara, mas clara si es oscura) para que se distinga."""
    if rgb == DARK_PILL:
        return rgb
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    if v > 0.5:
        s, v = min(1.0, s * 1.25 + 0.05), v * 0.82
    else:
        v = min(1.0, v + 0.14)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _draw_rank_tab(image: Image.Image, text: str) -> None:
    """Catalogo Top: pestaña centrada pegada al borde superior (solo esquinas de abajo redondeadas)."""
    w, _ = image.size
    font = _font(int(w * TAB_FONT), _PILL_FONT)
    tw = ImageDraw.Draw(image).textlength(text, font=font)
    pw = int(tw + 2 * w * TAB_PAD_X)
    ph = int(w * TAB_H)
    r = int(ph * 0.22)
    rgb = _tab_rgb(_accent_rgb(image))
    big = Image.new("RGBA", (pw * SS, ph * SS), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle((0, -r * SS, pw * SS - 1, ph * SS - 1), radius=r * SS, fill=(*rgb, 255))
    tab = big.resize((pw, ph), Image.LANCZOS)
    _draw_centered_text(tab, (0, 0, pw, ph), text, font, _text_on(rgb))
    image.alpha_composite(tab, ((w - pw) // 2, 0))


def _draw_label(image: Image.Image, text: str) -> int:
    """Texto inferior centrado. Devuelve la y superior que ocupa (para no pisarlo con el logo)."""
    w, h = image.size
    d = ImageDraw.Draw(image)
    size = int(w * LABEL_FONT)
    font = _font(size)
    while d.textlength(text, font=font) > w * 0.86 and size > 10:
        size -= 1
        font = _font(size)
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    cy = int(h * LABEL_CY)
    d.text(((w - (r - l)) / 2 - l, cy - (b - t) / 2 - t), text, font=font, fill=LABEL_COLOR)
    return cy - (b - t) // 2


def _draw_title_text(image: Image.Image, title: str, bottom_limit: int) -> None:
    w, h = image.size
    d = ImageDraw.Draw(image)
    max_w = int(w * LOGO_MAX_W)
    size = int(h * 0.09)
    while size > 18:
        f = _font(size, _PILL_FONT)
        words, lines, cur = title.split(), [], []
        for word in words:
            cand = " ".join(cur + [word])
            if d.textlength(cand, font=f) <= max_w or not cur:
                cur.append(word)
            else:
                lines.append(" ".join(cur))
                cur = [word]
        if cur:
            lines.append(" ".join(cur))
        lh = int(size * 1.12)
        if len(lines) <= 2 and all(d.textlength(x, font=f) <= max_w for x in lines):
            break
        size -= 2
    cy = min(int(h * 0.77), bottom_limit - lh * len(lines) // 2)
    top = cy - lh * len(lines) // 2
    for i, line in enumerate(lines):
        l, t, r, b = d.textbbox((0, 0), line, font=f)
        x = (w - (r - l)) / 2 - l
        y = top + i * lh
        d.text((x + 2, y + 2), line, font=f, fill=(0, 0, 0, 170))
        d.text((x, y), line, font=f, fill=(255, 255, 255, 255))


def build_pill_poster(
    image: Image.Image,
    *,
    label: str,
    sash_text: str | None,
    tokens: list[str],
    show_badges: bool,
    logo: Image.Image | None,
    fallback_title: str | None,
    rank_text: str | None = None,
) -> Image.Image:
    w, h = image.size
    image = _bottom_fade(image)

    label_top = _draw_label(image, label)
    gap = int(h * 0.025)

    if logo is not None:
        # composite_logo centra el logo en logo_centre_y(); se elige bottom_ratio para que ese
        # centro quede en LOGO_CY y la altura maxima no llegue al texto inferior.
        max_h_ratio = min(LOGO_MAX_H, (label_top - gap - h * (LOGO_CY - LOGO_MAX_H / 2)) / h)
        g_max_h = min(int(h * _cfg.LOGO_MAX_H_RATIO), LOGO_ABS_MAX_H)
        bottom_ratio = (h - h * LOGO_CY - g_max_h / 2) / h
        composite_logo(image, logo, max_w_ratio=LOGO_MAX_W, max_h_ratio=max(0.08, max_h_ratio),
                       bottom_ratio=bottom_ratio)
    elif fallback_title:
        _draw_title_text(image, fallback_title, label_top - gap)

    if rank_text:
        # Catalogo Top: solo la pestaña con el puesto, sin badges ni otra pastilla (como btttr).
        _draw_rank_tab(image, rank_text)
        return image

    badge_cy = int(w * MARGIN) + int(w * PILL_H) // 2
    left_of_badges = w - int(w * MARGIN) + int(w * ITEM_GAP)
    if show_badges and tokens:
        left_of_badges = _draw_badges(image, tokens, badge_cy, sash_text)
    if sash_text:
        _draw_pill(image, sash_text, left_of_badges)
    return image
