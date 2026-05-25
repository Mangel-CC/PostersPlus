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
#   * Badges from cache only (no AIOStreams fan-out per anonymous hit).
#   * Sash on (the curated discovery overrides are the moat).
_PUBLIC_BASE: dict[str, str] = {
    "badge_display_mode": "1",
    "show_award_sash":    "true",
}


def _preset(extra: dict[str, str]) -> dict[str, str]:
    """Layer preset-specific overrides on top of the public-tier base."""
    return {**_PUBLIC_BASE, **extra}


# Six starter presets. Names are part of the URL contract — renaming a
# preset breaks any external link that already uses it, so rename only
# with a deprecation alias.
PRESETS: dict[str, dict[str, str]] = {
    # Default rendering — same look the configurator emits by default.
    # The natural choice for embedding posters where you don't have an
    # opinion about visual tone.
    "default": _preset({}),

    # Sash-forward. Hides the numeric score so the award/discovery sash
    # is the dominant element. Useful for catalogues skewed toward
    # prestige picks (festival winners, AFI lists, "best of" sets).
    "awards": _preset({
        "rating_display_mode": "0",
    }),

    # Minimalist mode — small genre text bottom-right, no score bar.
    # Pairs well with grid-heavy UIs that want the poster art to lead.
    "minimalist": _preset({
        "rating_display_mode": "3",
    }),

    # Letterboxd-flavoured: numeric score with the genre tag, no sash.
    # Mirrors the audience-score-only aesthetic of letterboxd embeds.
    "letterboxd": _preset({
        "rating_display_mode":   "2",
        "show_award_sash":       "false",
        "movie_weights":         "letterboxd:1.0",
        "tv_weights":            "trakt:0.7,tomatoes:0.3",
    }),

    # Cinephile: prestige-leaning sash priority. Surfaces festival
    # circuit and director/cast badges before commercial-success ones
    # like "trending" or "metacritic-must-see".
    "cinephile": _preset({
        "rating_display_mode": "1",
        "sash_priority":       "wins,festival,gg_wins,director,cast,studio,pic_noms,gg_noms",
    }),

    # Quality-forward: keeps the score visible AND the quality badges
    # corner-stacked when AIOStreams data is in cache. Good for users
    # who want stream-availability cues alongside the poster.
    "quality": _preset({
        "rating_display_mode": "1",
        "badge_display_mode":  "1",
    }),
}


def get_preset(name: str) -> dict[str, str] | None:
    """Return the raw_params dict for a preset name, or None if unknown."""
    return PRESETS.get(name)


def preset_names() -> list[str]:
    """List of registered preset names — used by /server-caps for discovery."""
    return sorted(PRESETS.keys())
