"""Filtrado de anuncios según los criterios de config.yaml."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from src.models import Listing
from src.text import normalize as _normalize

log = logging.getLogger(__name__)


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def matches(listing: Listing, filtros: dict[str, Any]) -> bool:
    """True si el anuncio cumple todos los criterios definidos.

    Un criterio ausente (o a None) no filtra nada.
    """
    precio_min = filtros.get("precio_min")
    if precio_min is not None and listing.precio < precio_min:
        return False

    precio_max = filtros.get("precio_max")
    if precio_max is not None and listing.precio > precio_max:
        return False

    m2_min = filtros.get("m2_min")
    if m2_min is not None and listing.m2 < m2_min:
        return False

    habitaciones_min = filtros.get("habitaciones_min")
    if habitaciones_min is not None and listing.habitaciones < habitaciones_min:
        return False

    banos_min = filtros.get("banos_min")
    if banos_min is not None and listing.banos < banos_min:
        return False

    tipos = filtros.get("tipos") or []
    if tipos and _normalize(listing.tipo) not in {_normalize(t) for t in tipos}:
        return False

    # Un chalet o un adosado no tienen "planta": exigirles un primero o superior
    # los dejaría a todos fuera.
    planta_min = filtros.get("planta_min")
    if planta_min is not None and listing.tiene_planta and listing.planta < planta_min:
        return False

    exterior = filtros.get("exterior")
    if exterior is not None and listing.exterior != bool(exterior):
        return False

    ascensor = filtros.get("ascensor")
    if ascensor is not None and listing.ascensor != bool(ascensor):
        return False

    barrio = _normalize(listing.barrio)

    incluidos = filtros.get("barrios_incluidos") or []
    if incluidos and barrio not in {_normalize(b) for b in incluidos}:
        return False

    excluidos = filtros.get("barrios_excluidos") or []
    if barrio in {_normalize(b) for b in excluidos}:
        return False

    descripcion = _normalize(listing.descripcion)
    for palabra in filtros.get("palabras_excluidas") or []:
        if _normalize(palabra) in descripcion:
            return False

    return True


def apply_filters(listings: list[Listing], config: dict[str, Any]) -> list[Listing]:
    filtros = config.get("filtros") or {}
    if not filtros:
        log.warning("no hay filtros configurados: se aceptan todos los anuncios")
        return list(listings)

    kept = [l for l in listings if matches(l, filtros)]
    log.info("filtrado: %d de %d anuncios pasan los criterios", len(kept), len(listings))
    return kept
