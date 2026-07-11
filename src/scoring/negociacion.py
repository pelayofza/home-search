"""Estimación de cuánto margen hay para regatear.

Esto es una HEURÍSTICA, no un modelo. No hay datos de precio de cierre en ningún
sitio público, así que nadie puede decirte de verdad cuánto vas a rebajar. Lo que
sí se puede hacer es agregar las tres señales que un comprador con experiencia
mira, y enseñarlas juntas con su razonamiento a la vista:

  - Cuánto lleva sin venderse. Un piso que lleva medio año en el mercado tiene al
    vendedor mucho más blando que uno que salió el martes.
  - Cuántas veces ya ha bajado. Un vendedor que ya ha cedido dos veces ha
    demostrado que cede.
  - Cuánto se sale del precio de su barrio. Lo que está caro tiene de dónde bajar;
    lo que ya está barato, no.
"""

from __future__ import annotations

from typing import Any

TOPE_PCT = 15.0


def estimar(
    dias_vistos: int | None,
    bajadas: int,
    bajada_acumulada_pct: float,
    desviacion_vs_mediana: float | None,
) -> dict[str, Any]:
    """Margen estimado en % sobre el precio actual, con los motivos que lo sostienen."""
    margen = 2.0  # el regateo de cortesía que casi siempre se acepta
    motivos: list[str] = []

    if dias_vistos is not None and dias_vistos >= 45:
        # ~1 punto por cada mes y medio, topado: pasado un año ya no dice más.
        extra = min(4.0, dias_vistos / 90)
        margen += extra
        motivos.append(f"lleva {dias_vistos} días sin venderse")

    if bajadas:
        margen += min(4.0, 1.5 * bajadas)
        motivos.append(
            f"ya ha bajado {bajadas} {'vez' if bajadas == 1 else 'veces'}"
            + (f" ({abs(bajada_acumulada_pct):.0f}% acumulado)" if bajada_acumulada_pct else "")
        )

    if desviacion_vs_mediana is not None and desviacion_vs_mediana > 5:
        margen += min(5.0, desviacion_vs_mediana / 4)
        motivos.append(f"está un {desviacion_vs_mediana:.0f}% por encima de su barrio")
    elif desviacion_vs_mediana is not None and desviacion_vs_mediana < -10:
        # Ya está por debajo de mercado: pedir rebaja aquí es pedir peras al olmo.
        margen = min(margen, 3.0)
        motivos.append("ya está por debajo del precio de su barrio: poco recorrido")

    margen = min(margen, TOPE_PCT)
    return {
        "margen_pct": round(margen, 1),
        "motivos": motivos,
        "heuristica": True,  # que nadie lo confunda con una predicción
    }


def objetivo(precio: int, margen_pct: float) -> int:
    """El precio al que apuntar, redondeado al millar."""
    return round(precio * (1 - margen_pct / 100) / 1000) * 1000


def texto(estimacion: dict[str, Any] | None, precio: int) -> str | None:
    if not estimacion:
        return None
    pct = estimacion["margen_pct"]
    meta = estimacion.get("objetivo") or objetivo(precio, pct)
    motivos = "; ".join(estimacion["motivos"]) or "sin señales de flexibilidad"
    return f"margen estimado ~{pct:.0f}% (unos {meta:,} €) — {motivos}".replace(",", ".")
