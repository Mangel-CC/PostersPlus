# age_badge.py  — typographic age-rating + quality-tier colour (badge mode 1)
#
# Scoring (max 2 pts per category, 6 pts total):
#   Resolution:  4K=2,    1080P=1
#   Source:      REMUX=2, WEBDL=1
#   Visual:      DV=2,    HDR10+=1, HDR10=1
#
# Tiers → font colour:
#   0–1 pts  → Grey     (unknown / poor quality)
#   2–3 pts  → Bronze
#   4–5 pts  → Silver
#   6+ pts   → Gold

from __future__ import annotations
import os
from typing import Sequence
from PIL import Image, ImageDraw, ImageFilter, ImageFont


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_CATEGORIES: dict[str, dict[str, int]] = {
    "resolution": {"4K": 2, "1080P": 1},
    "source":     {"REMUX": 2, "WEBDL": 1},
    "visual":     {"DV": 2, "HDR10+": 2, "HDR10": 1},
}

_CATEGORY_CAP = 2


def _score_points(tokens: Sequence[str]) -> int:
    token_set = set(tokens)
    total = 0
    for category_tokens in _CATEGORIES.values():
        pts = sum(v for k, v in category_tokens.items() if k in token_set)
        total += min(pts, _CATEGORY_CAP)
    return total


# ---------------------------------------------------------------------------
# Tier colours  (RGBA)
#
# Each tier carries three layers that build the premium look:
#   glow      — wide, very soft halo drawn underneath (large blur radius)
#   shadow    — tight drop shadow for depth
#   primary   — the face colour of the numeral
#   highlight — a near-white tint composited at low opacity for an inner-light
#               effect (simulated by blending a white copy at reduced alpha)
# ---------------------------------------------------------------------------

_TIERS = {
    "grey": {
        "glow":      (100, 100, 104,  28),   # barely any halo — looks flat/unlit
        "shadow":    (12,  12,  14, 190),
        "primary":   (130, 130, 136, 175),   # noticeably darker and more faded than silver
        "highlight": (160, 160, 166,  18),   # near-invisible — no metallic sheen
    },
    "bronze": {
        "glow":      (200, 110,  40,  70),
        "shadow":    (45,  18,   0, 210),
        "primary":   (200, 110,  45, 225),
        "highlight": (255, 200, 150, 45),
    },
    "silver": {
        "glow":      (195, 205, 228,  65),
        "shadow":    (30,  34,  50, 215),
        "primary":   (218, 224, 240, 200),
        "highlight": (255, 255, 255, 55),
    },
    "gold": {
        "glow":      (255, 215,  70,  80),
        "shadow":    (60,  45,   0, 220),
        "primary":   (255, 205,  60, 200),
        "highlight": (255, 250, 200, 55),
    },
}


def _tier(pts: int) -> dict:
    if pts >= 6:
        return _TIERS["gold"]
    elif pts >= 4:
        return _TIERS["silver"]
    elif pts >= 2:
        return _TIERS["bronze"]
    else:
        return _TIERS["grey"]


# ---------------------------------------------------------------------------
# Font cache
# ---------------------------------------------------------------------------

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _font(name: str, size: int):
    key = (name, size)
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(name, size)
        except IOError:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_text_layer(
    size: tuple[int, int],
    xy: tuple[int, int],
    text: str,
    font,
    fill: tuple[int, int, int, int],
) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=font, fill=fill)
    return layer


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def draw_quality_age_badge(
    image: Image.Image,
    age_rating: int | None,
    quality_tokens: Sequence[str],
    *,
    anchor_x_ratio: float = 0.040,
    anchor_y_ratio: float = 0.036,
    badge_height: int = 54,
    always_silver: bool = False,
) -> None:
    """
    Render the age rating as a large typographic number in the top-left corner.
    Colour is determined by the quality tier derived from *quality_tokens*,
    unless *always_silver* is True, in which case the silver tier is always used
    regardless of quality (mode 3 — age rating only, no quality dependency).
    If no age rating is available the badge is skipped entirely.

    Visual layers (back → front):
      1. Wide ambient glow      — very soft, large-radius blur for luminance halo
      2. Tight drop shadow      — small offset + moderate blur for depth
      3. Primary numeral        — full-opacity face colour
      4. Highlight pass         — near-white overlay at low opacity, shifted
                                  slightly up-left, for an inner-light illusion
    """
    if not age_rating:
        return

    W, H   = image.size
    colors = _TIERS["silver"] if always_silver else _tier(_score_points(quality_tokens))

    age_text  = str(age_rating)
    font_size = max(16, int(badge_height * 1.0))
    font      = _font(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf"), font_size)

    ax = int(W * anchor_x_ratio)
    ay = int(H * anchor_y_ratio)

    # Measure text bounds once
    probe = ImageDraw.Draw(image)
    bb    = probe.textbbox((0, 0), age_text, font=font)
    tx    = ax - bb[0]
    ty    = ay - bb[1]

    # ── 1. Ambient glow ───────────────────────────────────────────────────
    # Large, very-soft blur centred on the glyph — creates a luminance halo
    # that belongs to the numeral rather than the surface beneath it.
    glow_blur = max(font_size // 4, 8)
    glow_layer = _make_text_layer(image.size, (tx, ty), age_text, font, colors["glow"])
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_blur))
    image.alpha_composite(glow_layer)

    # Second, slightly tighter pass at higher opacity for a warm core to the glow
    glow_core_color = (*colors["glow"][:3], min(255, colors["glow"][3] + 40))
    glow_core = _make_text_layer(image.size, (tx, ty), age_text, font, glow_core_color)
    glow_core = glow_core.filter(ImageFilter.GaussianBlur(glow_blur // 2))
    image.alpha_composite(glow_core)

    # ── 2. Drop shadow ────────────────────────────────────────────────────
    shadow_offset = max(1, font_size // 16)   # tighter than before for elegance
    shadow_blur   = max(2, font_size // 10)
    shadow_layer  = _make_text_layer(
        image.size,
        (tx + shadow_offset, ty + shadow_offset),
        age_text, font, colors["shadow"],
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(shadow_blur))
    image.alpha_composite(shadow_layer)

    # ── 3. Primary numeral ────────────────────────────────────────────────
    text_layer = _make_text_layer(image.size, (tx, ty), age_text, font, colors["primary"])
    image.alpha_composite(text_layer)

    # ── 4. Highlight / inner-light pass ───────────────────────────────────
    # A slightly up-left shifted copy in near-white at low opacity creates the
    # illusion of light catching the top-left edge of the numeral — no container
    # needed; the effect belongs entirely to the glyph itself.
    hl_offset = max(1, font_size // 22)
    hl_layer  = _make_text_layer(
        image.size,
        (tx - hl_offset, ty - hl_offset),
        age_text, font, colors["highlight"],
    )
    # Tiny blur so the highlight blends rather than creating a visible echo
    hl_layer = hl_layer.filter(ImageFilter.GaussianBlur(max(1, font_size // 30)))
    image.alpha_composite(hl_layer)