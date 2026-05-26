"""Phase 11: named preset bundles for the anonymous public tier.

The public preset endpoint ``/p/{preset}/{type}/{imdb_id}.jpg`` resolves
the preset name to a fixed ``raw_params`` dict, then runs the same
render pipeline as ``/poster`` but with a few simplifications that make
it CDN-cacheable and quota-safe:

  * No per-user TMDB / MDBlist keys — the operator's server keys are used.
  * No access_key gating — the endpoint is anonymous (the operator
    decides whether to expose it by setting ``PRESET_ENABLED``).
  * No active quality-badge fetch — badges only render from cached
    AIOStreams data (``badge_display_mode=1``) so an anonymous poster
    load can't fan out into per-title stream lookups.

Presets are intentionally small and grep-able. Each value is a string
because ``build_request_config`` parses raw query-param strings; that
keeps the public preset path bit-for-bit equivalent to a /poster call
with the same params.

Adding a preset: append to ``PRESETS`` below, add a one-line
description, and the new key is immediately reachable as
``/p/<name>/...``. Keep names URL-safe lowercase + dashes.

Cherry-pick guide:
  * Upstream has no equivalent — this is an ElfHosted-only module.
  * The preset dicts are pure data; nothing about them couples to the
    rest of the codebase, so renaming or retuning a preset is a
    one-file change.
"""
from __future__ import annotations


# Common base applied to every preset. Public-tier defaults:
#   * Mode 4 quality indicator (cheap vertical tier-bar pill — much
#     lighter render cost than mode 1 age-rating numeral or mode 2
#     full badge row). Cached badges only; never AIOStreams fan-out on
#     anonymous hits.
#   * Sash on by default (the curated discovery-override dataset is
#     the moat — every public hit gets a chance to show a festival or
#     director sash).
#
# Presets pick a small number of axes that meaningfully differ visually
# while keeping render cost low. The endpoint hashes raw_params into a
# short params_hash, so fewer distinct presets = fewer composite cache
# entries per (imdb_id, type) = better cache hit rate at the edge.
_PUBLIC_BASE: dict[str, str] = {
    "badge_display_mode": "4",
    "show_award_sash":    "true",
}


def _preset(extra: dict[str, str]) -> dict[str, str]:
    """Layer preset-specific overrides on top of the public-tier base."""
    return {**_PUBLIC_BASE, **extra}


# Six starter presets. Names are part of the URL contract — renaming a
# preset breaks any external link that already uses it, so rename only
# with a deprecation alias.
PRESETS: dict[str, dict[str, str]] = {
    # Default rendering. Standard look: weighted score bar + genre/year
    # caption + a mode-4 tier accent bar in the corner. The natural
    # choice when no opinion about visual tone is needed.
    "default": _preset({}),

    # Sash-forward. The award/discovery sash is the only visible
    # overlay: score hidden, no quality bar. Muted sash sits *in* the
    # art rather than above it for a less-shouty prestige look.
    "awards": _preset({
        "rating_display_mode": "0",
        "badge_display_mode":  "0",
        "muted":               "true",
    }),

    # Minimalist mode — small genre tag bottom-right, no sash, no
    # quality bar, no logo overlay (textless). Lets the poster art
    # lead; cheapest preset to render of the set.
    "minimalist": _preset({
        "rating_display_mode": "3",
        "badge_display_mode":  "0",
        "show_award_sash":     "false",
        "textless":            "true",
    }),

    # Letterboxd-flavoured: numeric score with genre tag, no sash, no
    # quality bar. Mirrors the audience-score-only aesthetic of
    # letterboxd embeds.
    "letterboxd": _preset({
        "rating_display_mode":   "2",
        "badge_display_mode":    "0",
        "show_award_sash":       "false",
        "movie_weights":         "letterboxd:1.0",
        "tv_weights":            "trakt:0.7,tomatoes:0.3",
    }),

    # Cinephile: prestige-leaning sash priority (festival circuit and
    # director/cast slots before commercial-success ones), muted sash,
    # and the metal score palette (grey/bronze/silver/gold) matching
    # the tier-bar colours for a unified subdued look.
    "cinephile": _preset({
        "rating_display_mode": "1",
        "badge_display_mode":  "4",
        "sash_priority":       "wins,festival,gg_wins,director,cast,studio,pic_noms,gg_noms",
        "muted":               "true",
        "score_color_mode":    "2",
    }),

    # Quality-forward: keeps the score visible AND a mode-4 tier bar.
    # Cheaper than the old badge-row mode and still signals stream
    # availability when AIOStreams data is cached.
    "quality": _preset({
        "rating_display_mode": "1",
        "badge_display_mode":  "4",
    }),
}


def get_preset(name: str) -> dict[str, str] | None:
    """Return the raw_params dict for a preset name, or None if unknown."""
    return PRESETS.get(name)


def preset_names() -> list[str]:
    """List of registered preset names — used by /server-caps for discovery."""
    return sorted(PRESETS.keys())
