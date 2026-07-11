"""Subscore por precio: ¿está caro o barato comparado con su barrio?"""

from __future__ import annotations

from typing import Any

from src.models import Listing


def puntua(
    listing: Listing,
    mediana: float | None,
    n: int,
    cfg: dict[str, Any],
    fuente: str = "propia",
) -> tuple[float | None, dict[str, Any]]:
    """Compara el €/m² del anuncio contra la mediana de su (barrio, familia).

    Sin mediana no hay nota: devolver 50 "por si acaso" sería inventarse un dato
    y contaminar el total con ruido disfrazado de neutralidad.
    """
    if not mediana or not listing.m2:
        return None, {"comparado_con": None}

    rango = float(cfg.get("rango_pct", 25))
    saturacion = float(cfg.get("saturacion_pct", 35))

    desviacion = 100 * (listing.precio_m2 - mediana) / mediana

    # Un -50% no es el doble de chollo que un -25%: es una señal de que algo va
    # mal con el inmueble. Por encima de la saturación, el descuento deja de sumar.
    efectiva = max(desviacion, -saturacion)

    # -rango% -> 100 puntos; igual que la mediana -> 50; +rango% -> 0.
    nota = max(0.0, min(100.0, 50 - 50 * efectiva / rango))

    detalle = {
        "precio_m2": listing.precio_m2,
        "mediana_m2": round(mediana),
        "desviacion_pct": round(desviacion, 1),
        "muestra": n,
        "comparado_con": fuente,
        "saturado": desviacion < -saturacion,
    }
    return nota, detalle
