"""Distancias sobre la esfera. Nada de dependencias: son cuatro fórmulas."""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

RADIO_TIERRA_M = 6_371_000


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia en metros entre dos puntos."""
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * RADIO_TIERRA_M * asin(sqrt(a))


def distancia_a_bbox(lat: float, lon: float, bbox: list[float]) -> float:
    """Distancia al rectángulo [sur, oeste, norte, este]. 0 si está dentro.

    Es una cota INFERIOR de la distancia al polígono que contiene: sirve para
    descartar candidatos rápido, no como respuesta final.
    """
    sur, oeste, norte, este = bbox
    return haversine(lat, lon, min(max(lat, sur), norte), min(max(lon, oeste), este))


def _a_metros(lat_ref: float, lat: float, lon: float) -> tuple[float, float]:
    """Proyección local plana. A escala de barrio el error es despreciable."""
    return (
        radians(lon) * RADIO_TIERRA_M * cos(radians(lat_ref)),
        radians(lat) * RADIO_TIERRA_M,
    )


def _distancia_a_segmento(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return sqrt((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2)


def _dentro(p, anillo) -> bool:
    """Ray casting: ¿el punto cae dentro del anillo?"""
    px, py = p
    dentro = False
    for (ax, ay), (bx, by) in zip(anillo, anillo[1:] + anillo[:1]):
        if (ay > py) != (by > py) and px < (bx - ax) * (py - ay) / (by - ay) + ax:
            dentro = not dentro
    return dentro


def distancia_a_poligono(lat: float, lon: float, anillos: list[list[list[float]]]) -> float:
    """Distancia en metros al polígono. 0 si el punto está dentro.

    El rectángulo envolvente no vale: un parque largo y diagonal (el Canal Bajo,
    sin ir más lejos) tiene un rectángulo que cubre kilómetros de calles que no
    son parque, y regalaría "zona verde a 0 m" a medio barrio.
    """
    p = _a_metros(lat, lat, lon)
    mejor = float("inf")

    for anillo in anillos:
        if len(anillo) < 3:
            continue
        puntos = [_a_metros(lat, vlat, vlon) for vlat, vlon in anillo]
        if _dentro(p, puntos):
            return 0.0
        for a, b in zip(puntos, puntos[1:] + puntos[:1]):
            mejor = min(mejor, _distancia_a_segmento(p, a, b))

    return mejor


def mas_cercano(lat: float, lon: float, puntos: list[dict]) -> tuple[float | None, dict | None]:
    """El POI más cercano y su distancia, midiendo contra el polígono si lo tiene.

    Calcular el polígono de los ~1.000 parques por anuncio sería lento sin
    necesidad, así que primero se ordena por la distancia al rectángulo (que es
    una cota inferior) y se corta en cuanto el siguiente candidato ya no puede
    mejorar al mejor encontrado.
    """
    if not puntos:
        return None, None

    candidatos = sorted(
        (
            (
                distancia_a_bbox(lat, lon, p["bbox"])
                if p.get("bbox")
                else haversine(lat, lon, p["lat"], p["lon"]),
                i,
                p,
            )
            for i, p in enumerate(puntos)
        ),
        key=lambda c: c[0],
    )

    mejor_d, mejor_p = float("inf"), None
    for cota, _, p in candidatos:
        if cota >= mejor_d:
            break  # los que quedan están aún más lejos
        d = distancia_a_poligono(lat, lon, p["polys"]) if p.get("polys") else cota
        if d < mejor_d:
            mejor_d, mejor_p = d, p

    return mejor_d, mejor_p
