"""Subscore por localización: qué tienes cerca y a qué distancia."""

from __future__ import annotations

import logging
from typing import Any

from src.geo.distancias import mas_cercano
from src.models import Listing

log = logging.getLogger(__name__)


def distancias(listing: Listing, pois: dict[str, Any]) -> dict[str, float] | None:
    """Metros hasta lo más cercano de cada categoría. None si no hay coordenadas."""
    if listing.lat is None or listing.lon is None:
        return None

    sur, oeste, norte, este = pois["bbox"]
    if not (sur <= listing.lat <= norte and oeste <= listing.lon <= este):
        # Sin esto, un anuncio fuera del bbox diría "metro a 8 km" y nadie se
        # enteraría de que el problema es que no tenemos datos de su zona.
        log.warning(
            "%s (%s) cae fuera del bbox de POIs: sin subscore de localización",
            listing.property_code,
            listing.barrio,
        )
        return None

    dists: dict[str, float] = {}
    for categoria, puntos in pois["puntos"].items():
        d, _ = mas_cercano(listing.lat, listing.lon, puntos)
        if d is not None:
            dists[categoria] = d

    return dists


def puntua(
    dists: dict[str, float] | None, cfg: dict[str, Any]
) -> tuple[float | None, dict[str, Any]]:
    """Media ponderada de las distancias, cada una convertida a 0-100."""
    if not dists:
        return None, {}

    umbrales = cfg.get("umbrales_m") or {}
    total, peso_total = 0.0, 0.0
    detalle: dict[str, Any] = {}

    for categoria, u in umbrales.items():
        d = dists.get(categoria)
        if d is None:
            continue  # no hay POIs de esa categoría en el caché
        nota = _decae(d, u["optimo"], u["malo"])
        peso = float(u.get("peso", 1))
        total += nota * peso
        peso_total += peso
        detalle[categoria] = {"metros": round(d), "nota": round(nota)}

    if peso_total == 0:
        return None, {}
    return total / peso_total, detalle


def _decae(distancia: float, optimo: float, malo: float) -> float:
    """100 puntos hasta `optimo`, 0 a partir de `malo`, lineal entre medias."""
    if distancia <= optimo:
        return 100.0
    if distancia >= malo:
        return 0.0
    return 100 * (malo - distancia) / (malo - optimo)
