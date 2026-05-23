#awards.py
import os
import re
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------

class _FetchFailed:
    """Singleton sentinel returned when a fetch attempt fails."""
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    def __repr__(self):
        return "FETCH_FAILED"

FETCH_FAILED = _FetchFailed()


# ---------------------------------------------------------------------------
# Emmy winners — hardcoded TMDB IDs
# Drama, Comedy and Limited Series winners only.
# ---------------------------------------------------------------------------

EMMY_WINNER_TMDB_IDS: set[int] = {
    # Comedy
    247767,  # The Studio
    124101,  # Hacks
    136315,  # The Bear
    97546,   # Ted Lasso
    61662,   # Schitt's Creek
    67070,   # Fleabag
    70796,   # The Marvelous Mrs Maisel
    2947,    # Veep
    1421,    # Modern Family
    4608,    # 30 Rock
    2316,    # The Office
    2140,    # Everybody Loves Raymond
    4589,    # Arrested Development
    1668,    # Friends
    105,     # Sex and the City
    4454,    # Will & Grace
    1480,    # Ally McBeal
    3452,    # Frasier
    1400,    # Seinfeld
    3219,    # Murphy Brown
    141,     # Cheers
    4500,    # The Wonder Years
    1678,    # The Golden Girls
    1759,    # The Cosby Show
    3253,    # Barney Miller
    2251,    # Taxi
    1922,    # All in the Family
    2962,    # The Mary Tyler Moore Show
    918,     # M*A*S*H
    582,     # My World and Welcome to It
    # Drama
    250307,  # The Pitt
    126308,  # Shogun
    76331,   # Succession
    65494,   # The Crown
    1399,    # Game of Thrones
    69478,   # The Handmaid's Tale
    1396,    # Breaking Bad
    1407,    # Homeland
    1104,    # Mad Men
    1398,    # The Sopranos
    1973,    # 24
    4607,    # Lost
    688,     # The West Wing
    3050,    # The Practice
    549,     # Law & Order
    4588,    # ER
    194,     # NYPD Blue
    206,     # Picket Fences
    4396,    # Northern Exposure
    732,     # L.A. Law
    1448,    # thirtysomething
    4223,    # Cagney & Lacey
    3828,    # Hill Street Blues
    480,     # Lou Grant
    954,     # The Rockford Files
    492,     # Upstairs Downstairs
    9855,    # Police Story
    5021,    # The Waltons
    1103,    # Elizabeth R
    3213,    # Marcus Welby M.D.
    # Limited Series
    249042,  # Adolescence
    154385,  # Beef
    111803,  # The White Lotus
    87739,   # The Queen's Gambit
    79788,   # Watchmen
    87108,   # Chernobyl
    64513,   # American Crime Story
    66292,   # Big Little Lies
    61585,   # Olive Kitteridge
    60622,   # Fargo
    33907,   # Downton Abbey
    16997,   # The Pacific
    13561,   # Little Dorrit
    15114,   # John Adams
    20056,   # Broken Trail
    13291,   # Elizabeth I
    13688,   # The Lost Prince
    11245,   # Angels in America
    2432,    # Taken
    4613,    # Band of Brothers
    21276,   # Anne Frank: The Whole Story
    20658,   # Arabian Nights
    814,     # Hornblower
    3556,    # From the Earth to the Moon
    11121,   # The Odyssey
    13675,   # Gulliver's Travels
}


# ---------------------------------------------------------------------------
# Award parsing from MDblist keywords
# ---------------------------------------------------------------------------

def parse_mdblist_awards(
    keywords: list[dict],
    tmdb_id: int | str | None = None,
) -> tuple[list[str], list[str]]:
    """
    Derive award wins and nominations from MDblist keyword objects.

    Best Picture wins/noms come from keywords:
        best-picture-winner   → win
        best-picture-nominated → nom

    Emmy wins come from the hardcoded EMMY_WINNER_TMDB_IDS set.
    Emmy noms come from the keyword:
        emmy-award-nominated  → nom (only added if no Emmy win already)

    Returns (wins, noms) where each is a list of human-readable strings.
    """
    keyword_names: set[str] = {
        (kw.get("name") or "").lower().strip()
        for kw in keywords
    }

    wins: list[str] = []
    noms: list[str] = []

    # --- Best Picture ---
    if "best-picture-winner" in keyword_names:
        wins.append("Best Picture")
    elif "best-picture-nominated" in keyword_names:
        noms.append("Best Picture")

    # --- Emmy ---
    numeric_tmdb_id: int | None = None
    if tmdb_id is not None:
        try:
            numeric_tmdb_id = int(tmdb_id)
        except (ValueError, TypeError):
            pass

    if numeric_tmdb_id is not None and numeric_tmdb_id in EMMY_WINNER_TMDB_IDS:
        wins.append("Emmy Winner")
    elif "emmy-award-nominated" in keyword_names:
        noms.append("Emmy Nominee")

    return wins, noms


# ---------------------------------------------------------------------------
# Sash drawing  (unchanged from original)
# ---------------------------------------------------------------------------

def _text_center(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    cx: float,
    cy: float,
) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    bbox_width = bbox[2] - bbox[0]

    try:
        ascent, descent = font.getmetrics()
    except AttributeError:
        ascent, descent = 0, 0

    x = cx - bbox_width / 2 - bbox[0]
    optical_adjust = int(ascent * 0.22)
    y = cy - (ascent + descent) / 2 - descent + optical_adjust

    return x, y


def draw_award_sash(
    image: Image.Image,
    label: str,
    sash_type: str = "win",
) -> Image.Image:
    width, height = image.size

    SS          = 3
    sash_length = int(width * 1.15) #1.1
    sash_height = int(width * 0.12) #0.12

    sl, sh = sash_length * SS, sash_height * SS

    sash = Image.new("RGBA", (sl, sh), (0, 0, 0, 0))
    d    = ImageDraw.Draw(sash)

    if sash_type == "win":
        hi, lo        = (212, 175, 55, 255), (160, 130, 40, 255)
        border_colour = (212, 175, 55, 255)
    elif sash_type == "prestige":
        hi, lo        = (160, 100, 230, 255), (100, 55, 160, 255)
        border_colour = (190, 140, 255, 255)
    elif sash_type == "cast":
        hi, lo        = (46, 125, 50, 255), (27, 94, 32, 255)
        border_colour = (102, 187, 106, 255)
    elif sash_type == "info":
        hi, lo        = (60, 190, 180, 255), (30, 130, 120, 255)
        border_colour = (100, 220, 210, 255)
    elif sash_type == "trending":
        hi, lo        = (90, 170, 255, 255), (50, 110, 190, 255)
        border_colour = (160, 220, 255, 255)
    else:  # "nom"
        hi, lo        = (180, 180, 190, 255), (110, 110, 120, 255)
        border_colour = (192, 192, 200, 255)

    half = sh // 2
    for y in range(sh):
        t = y / half if y < half else (sh - y) / half
        colour = tuple(int(lo[i] * (1 - t) + hi[i] * t) for i in range(4))
        d.line([(0, y), (sl, y)], fill=colour)

    margin = int(sh * 0.12)
    d.rectangle([(0, margin), (sl, sh - margin)], fill=(8, 8, 8, 245))

    edge = max(2 * SS, sh // 18)
    d.rectangle([(0, 0), (sl, edge)], fill=border_colour)
    d.rectangle([(0, sh - edge), (sl, sh)], fill=border_colour)
    # Disabled because it's causing aliasing bugs // d.rectangle([(0, 0), (sl - 1, sh - 1)], outline=(0, 0, 0, 120), width=max(1, SS))

    base_size     = sash_height * 0.4
    adjusted_size = sash_height * 0.85 / (len(label) ** 0.35)
    font_size     = int(min(base_size, adjusted_size)) * SS

    try:
        font = ImageFont.truetype(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Ubuntu-Bold.ttf"), font_size)
    except IOError:
        font = ImageFont.load_default()

    band_cx = sl / 2
    band_cy = margin + (sh - 2 * margin) / 2

    text_layer = Image.new("RGBA", sash.size, (0, 0, 0, 0))
    td         = ImageDraw.Draw(text_layer)

    tx, ty = _text_center(td, label, font, band_cx, band_cy)
    td.text((tx + 2 * SS, ty + 2 * SS), label, font=font, fill=(0, 0, 0, 180))
    td.text((tx, ty),                   label, font=font, fill=(225, 225, 225, 225))

    sash = Image.alpha_composite(sash, text_layer)

    sash = sash.rotate(-45, expand=True, resample=Image.Resampling.BICUBIC)
    sash = sash.resize((sash.width // SS, sash.height // SS), Image.Resampling.LANCZOS)

    shadow   = Image.new("RGBA", sash.size, (0, 0, 0, 0))
    sd       = ImageDraw.Draw(shadow)
    sd.bitmap((0, 0), sash.split()[3], fill=(0, 0, 0, 110))
    shadow   = shadow.filter(ImageFilter.GaussianBlur(10))

    result   = image.copy()
    offset_x = int(sash.width  * 0.68)
    offset_y = int(sash.height * 0.32)

    result.paste(shadow, (width - offset_x + 6, -offset_y + 6), shadow)
    result.paste(sash,   (width - offset_x,     -offset_y),     sash)

    return result