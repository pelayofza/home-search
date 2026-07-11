"""Caché local de puntos de interés, descargado una vez desde OpenStreetMap.

Los POIs cambian en escala de años; los anuncios, a diario. Consultar Overpass
en cada ejecución sería pagar una dependencia de red frágil por un dato estático,
y haría que la nota de un anuncio dependiera de lo que conteste un servidor hoy.

Uso:
    python -m src.geo.pois --refresh
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

OVERPASS = "https://overpass-api.de/api/interpreter"
CACHE = Path("data/pois.json")

#: Zona norte de Madrid, con margen. [sur, oeste, norte, este]
BBOX_DEFECTO = [40.44, -3.78, 40.58, -3.60]

#: Qué pedimos a OSM para cada categoría del scoring.
CONSULTAS = {
    "metro": 'node["railway"="station"]["station"="subway"]',
    "cercanias": 'node["railway"="station"]["station"!="subway"]',
    "bus": 'node["highway"="bus_stop"]',
    "hospital": 'nwr["amenity"="hospital"]',
    "parque": 'nwr["leisure"="park"]',
}


class POIsNoDisponibles(RuntimeError):
    """No hay caché de POIs. Ejecuta `python -m src.geo.pois --refresh`."""


def descargar(bbox: list[float] | None = None, destino: Path = CACHE) -> dict[str, Any]:
    bbox = bbox or BBOX_DEFECTO
    area = ",".join(str(c) for c in bbox)

    partes = [f"{selector}({area});" for selector in CONSULTAS.values()]
    # `out geom` trae la geometría completa: hace falta para medir la distancia
    # real a un parque, porque su rectángulo envolvente cubre mucho más que el
    # parque. Los nodos ya traen lat/lon.
    query = f"[out:json][timeout:180];({''.join(partes)});out geom;"

    log.info("pidiendo POIs a Overpass para el bbox %s...", area)
    r = requests.post(
        OVERPASS,
        data={"data": query},
        # Sin User-Agent, Overpass responde 406 y te deja adivinando.
        headers={"User-Agent": "home-search/0.1 (uso personal)"},
        timeout=200,
    )
    r.raise_for_status()

    puntos: dict[str, list[dict]] = {k: [] for k in CONSULTAS}
    for el in r.json().get("elements", []):
        categoria = _clasificar(el.get("tags") or {})
        if categoria is None:
            continue
        punto = _a_punto(el)
        if punto:
            puntos[categoria].append(punto)

    datos = {
        "bbox": bbox,
        "generado": datetime.now().isoformat(timespec="seconds"),
        "puntos": puntos,
    }
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")

    log.info(
        "POIs guardados en %s: %s",
        destino,
        ", ".join(f"{k}={len(v)}" for k, v in puntos.items()),
    )
    return datos


def _clasificar(tags: dict[str, str]) -> str | None:
    if tags.get("railway") == "station":
        return "metro" if tags.get("station") == "subway" else "cercanias"
    if tags.get("highway") == "bus_stop":
        return "bus"
    if tags.get("amenity") == "hospital":
        return "hospital"
    if tags.get("leisure") == "park":
        return "parque"
    return None


def _anillos(el: dict[str, Any]) -> list[list[list[float]]]:
    """Los contornos del elemento: uno si es un way, varios si es una relation."""
    if geom := el.get("geometry"):
        return [[[g["lat"], g["lon"]] for g in geom if g]]

    anillos = []
    for m in el.get("members") or []:
        if m.get("role") == "outer" and (geom := m.get("geometry")):
            anillos.append([[g["lat"], g["lon"]] for g in geom if g])
    return anillos


def _a_punto(el: dict[str, Any]) -> dict[str, Any] | None:
    bbox = None
    if b := el.get("bounds"):
        bbox = [b["minlat"], b["minlon"], b["maxlat"], b["maxlon"]]

    if "lat" in el:  # un nodo
        lat, lon = el["lat"], el["lon"]
    elif bbox:  # un way o una relation: el centro del rectángulo
        lat, lon = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    else:
        return None

    punto: dict[str, Any] = {
        "lat": lat,
        "lon": lon,
        "nombre": (el.get("tags") or {}).get("name", ""),
    }
    if bbox:
        punto["bbox"] = bbox
    if anillos := _anillos(el):
        punto["polys"] = anillos
    return punto


def cargar(origen: Path = CACHE) -> dict[str, Any]:
    if not origen.exists():
        raise POIsNoDisponibles(
            f"no encuentro {origen}. Ejecuta: python -m src.geo.pois --refresh"
        )
    return json.loads(origen.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Descarga los POIs de OpenStreetMap.")
    parser.add_argument("--refresh", action="store_true", help="vuelve a descargarlos")
    args = parser.parse_args()

    if args.refresh or not CACHE.exists():
        descargar()
    else:
        datos = cargar()
        print(f"Caché del {datos['generado']}:")
        for k, v in datos["puntos"].items():
            print(f"  {k}: {len(v)}")
