"""Calidad del logo de una ficha de TMDB, usada para decidir cuanto tiempo se cachea.

Un logo es "usable" si hay al menos uno en español, en inglés o sin idioma (neutral). Si TMDB
solo trae logos en otros idiomas (p.ej. solo el japonés de un anime recien estrenado) o ninguno,
PostersPlus acaba mostrando un logo ilegible o el de Metahub: esa ficha y los posters armados
con ella se cachean poco tiempo para recoger el logo correcto en cuanto alguien lo suba a TMDB.
"""

USABLE_LOGO_LANGS = (None, "", "en", "es")


def logos_usable(logos) -> bool:
    return any((lg or {}).get("iso_639_1") in USABLE_LOGO_LANGS for lg in (logos or []))
