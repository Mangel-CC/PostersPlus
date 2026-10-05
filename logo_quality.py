"""Calidad del logo de una ficha de TMDB, usada para decidir cuanto tiempo se cachea.

Un logo es "usable" si hay al menos uno en español, en inglés o sin idioma (neutral). Si TMDB
solo trae logos en otros idiomas (p.ej. solo el japonés de un anime recien estrenado) o ninguno,
PostersPlus acaba mostrando un logo ilegible o el de Metahub: esa ficha y los posters armados
con ella se cachean poco tiempo para recoger el logo correcto en cuanto alguien lo suba a TMDB.
"""

USABLE_LOGO_LANGS = (None, "", "en", "es")

# Estados de TMDB de algo que todavia no sale: el sash "En Produccion" / "Proximamente" depende de
# ellos y deja de ser cierto el dia del estreno, asi que ficha, estado y posters armados con ellos
# se cachean poco (ver BAD_LOGO_CACHE_MINUTES en config.py).
PRE_RELEASE_TMDB_STATUSES = ("In Production", "Planned", "Pilot", "Post Production", "Rumored")


def is_pre_release(tmdb_status) -> bool:
    return tmdb_status in PRE_RELEASE_TMDB_STATUSES


def logos_usable(logos) -> bool:
    return any((lg or {}).get("iso_639_1") in USABLE_LOGO_LANGS for lg in (logos or []))
