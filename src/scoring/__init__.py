"""Puntuación de anuncios: texto + precio + localización, ponderados."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from src.geo import pois as pois_mod
from src.models import Listing, Score
from src.scoring import localizacion, precio, texto

if TYPE_CHECKING:
    from src.store import Store

log = logging.getLogger(__name__)

PESOS_DEFECTO = {"texto": 0.30, "precio": 0.40, "localizacion": 0.30}


def config_hash(config: dict[str, Any]) -> str:
    """Huella del bloque `scoring:`.

    Los scores viven en la BD; si cambias los pesos, los guardados quedan
    obsoletos. Comparando el hash sabemos a cuáles hay que recalcular, sin
    tener que acordarnos de borrar nada a mano.
    """
    bloque = json.dumps(config.get("scoring") or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(bloque.encode()).hexdigest()[:12]


def puntua(
    listing: Listing,
    *,
    mediana: float | None,
    muestra: int,
    fuente_mediana: str,
    pois: dict[str, Any] | None,
    config: dict[str, Any],
) -> Score:
    cfg = config.get("scoring") or {}
    pesos = {**PESOS_DEFECTO, **(cfg.get("pesos") or {})}

    s_texto, d_texto = texto.puntua(listing.descripcion, cfg.get("texto") or {})
    s_precio, d_precio = precio.puntua(
        listing, mediana, muestra, cfg.get("precio") or {}, fuente_mediana
    )

    cfg_loc = cfg.get("localizacion") or {}
    dists = localizacion.distancias(listing, pois) if pois else None
    s_loc, d_loc = localizacion.puntua(dists, cfg_loc)

    subscores = {"texto": s_texto, "precio": s_precio, "localizacion": s_loc}

    # Renormalizar sobre lo disponible. La alternativa (puntuar 0 lo que no se
    # puede calcular) hundiría un anuncio por un fallo nuestro, no suyo. El
    # precio a pagar es que un 78 con tres dimensiones y un 78 con dos no son
    # el mismo 78: por eso el desglose viaja con la nota y la web lo enseña.
    disponibles = {k: v for k, v in subscores.items() if v is not None}
    peso_total = sum(pesos[k] for k in disponibles)
    total = (
        sum(v * pesos[k] for k, v in disponibles.items()) / peso_total if peso_total else 0.0
    )

    return Score(
        total=round(total, 1),
        texto=s_texto,
        precio=s_precio,
        localizacion=s_loc,
        detalle={
            "texto": d_texto,
            "precio": d_precio,
            "localizacion": d_loc,
            "dimensiones": sorted(disponibles),
        },
    )


def puntuar_todos(store: Store, source: str, config: dict[str, Any]) -> int:
    """Recalcula la nota de todos los anuncios activos.

    Se recalcula entero en cada ejecución, no solo lo nuevo, porque la nota
    depende de tres cosas que cambian solas: el precio del propio anuncio, la
    mediana del barrio (que crece con cada búsqueda) y los POIs. Cachearla
    invita a que se quede obsoleta en silencio, y son milisegundos de CPU.
    """
    huella = config_hash(config)
    pendientes = store.activos(source)
    if not pendientes:
        return 0

    cfg = config.get("scoring") or {}
    cfg_precio = cfg.get("precio") or {}
    fallback = cfg_precio.get("fallback_eur_m2") or {}

    try:
        pois = pois_mod.cargar()
    except pois_mod.POIsNoDisponibles as e:
        log.warning("%s -- puntúo sin localización", e)
        pois = None

    scores = {}
    for l in pendientes:
        mediana, n = store.mediana_precio_m2(
            l.barrio,
            l.familia,
            source=source,  # que el mock no contamine la mediana de los anuncios reales
            excluir=l.property_code,
            min_muestra=int(cfg_precio.get("min_muestra", 8)),
            dias=int(cfg_precio.get("ventana_dias", 180)),
        )
        fuente = "propia"
        if mediana is None:
            mediana = (fallback.get(l.barrio) or {}).get(l.familia)
            fuente = "config" if mediana else "ninguna"

        scores[l.property_code] = puntua(
            l,
            mediana=mediana,
            muestra=n,
            fuente_mediana=fuente,
            pois=pois,
            config=config,
        )

    store.guardar_scores(source, scores, huella)
    log.info("puntuados %d anuncios (config %s)", len(scores), huella)
    return len(scores)
