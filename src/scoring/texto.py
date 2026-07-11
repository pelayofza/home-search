"""Subscore por la descripción del anuncio: reglas y palabras clave, sin LLM."""

from __future__ import annotations

from typing import Any

from src.text import normalize

BASE = 50.0


def puntua(descripcion: str, reglas: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Parte de 50 y suma/resta según lo que diga el anuncio. Recorta a [0, 100].

    Devuelve también qué palabras han disparado, para poder explicar la nota:
    una nota sin motivo no se puede discutir, y por tanto no se puede afinar.
    """
    texto = normalize(descripcion)
    base = float(reglas.get("base", BASE))

    aciertos: list[tuple[str, float]] = []
    for grupo in ("positivas", "negativas"):
        for palabra, puntos in (reglas.get(grupo) or {}).items():
            if normalize(palabra) in texto:
                aciertos.append((palabra, float(puntos)))

    nota = max(0.0, min(100.0, base + sum(p for _, p in aciertos)))
    detalle = {
        "positivas": [w for w, p in aciertos if p > 0],
        "negativas": [w for w, p in aciertos if p < 0],
    }
    return nota, detalle
