"""Compara lo que marcas como interesante con lo que descartas.

Esto NO entrena nada ni ajusta pesos solo. Con veinte valoraciones no hay
estadística que valga, y un ajuste automático sobre esa muestra se sobreajustaría
a tus primeras impresiones. Lo que hace es enseñarte en qué se diferencian los
dos grupos, para que decidas tú si tocar `config.yaml`.

    python main.py --calibrar
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

MIN_UTIL = 5  # por debajo de esto, cualquier diferencia es ruido


def _media(valores: list[float]) -> float | None:
    return statistics.mean(valores) if valores else None


def analizar(opiniones: list[dict[str, Any]]) -> dict[str, Any]:
    interesan = [o for o in opiniones if o["valoracion"] == "interesa"]
    descartados = [o for o in opiniones if o["valoracion"] == "descartado"]

    def palabras(grupo: list[dict], signo: str) -> Counter:
        c: Counter = Counter()
        for o in grupo:
            c.update((o["detalle"].get("texto") or {}).get(signo) or [])
        return c

    def desviacion(grupo: list[dict]) -> float | None:
        valores = [
            o["detalle"]["precio"]["desviacion_pct"]
            for o in grupo
            if (o["detalle"].get("precio") or {}).get("desviacion_pct") is not None
        ]
        return _media(valores)

    return {
        "n_interesa": len(interesan),
        "n_descartado": len(descartados),
        "suficiente": min(len(interesan), len(descartados)) >= MIN_UTIL,
        "nota_interesa": _media([o["score"] for o in interesan if o["score"] is not None]),
        "nota_descartado": _media([o["score"] for o in descartados if o["score"] is not None]),
        "desviacion_precio_interesa": desviacion(interesan),
        "desviacion_precio_descartado": desviacion(descartados),
        "positivas_interesa": palabras(interesan, "positivas").most_common(8),
        "positivas_descartado": palabras(descartados, "positivas").most_common(8),
        "negativas_interesa": palabras(interesan, "negativas").most_common(8),
        "negativas_descartado": palabras(descartados, "negativas").most_common(8),
        "barrios_interesa": Counter(o["barrio"] for o in interesan).most_common(5),
        "barrios_descartado": Counter(o["barrio"] for o in descartados).most_common(5),
    }


def informe(analisis: dict[str, Any]) -> str:
    a = analisis
    lineas = [
        f"Valoraciones: {a['n_interesa']} interesan, {a['n_descartado']} descartadas.",
        "",
    ]

    if not a["suficiente"]:
        lineas += [
            f"Hacen falta al menos {MIN_UTIL} de cada para que esto signifique algo.",
            "Sigue valorando anuncios en la web y vuelve a ejecutarlo.",
            "",
        ]

    def cmp(titulo: str, x: float | None, y: float | None, unidad: str = "") -> str:
        if x is None or y is None:
            return f"{titulo}: sin datos suficientes."
        return f"{titulo}: {x:.0f}{unidad} en los que interesan, {y:.0f}{unidad} en los descartados."

    lineas += [
        cmp("Nota media", a["nota_interesa"], a["nota_descartado"]),
        cmp(
            "Desviación de precio vs mediana",
            a["desviacion_precio_interesa"],
            a["desviacion_precio_descartado"],
            "%",
        ),
        "",
    ]

    if a["nota_interesa"] and a["nota_descartado"]:
        brecha = a["nota_interesa"] - a["nota_descartado"]
        if brecha < 5:
            lineas += [
                "⚠ La nota apenas separa lo que te gusta de lo que no: el scoring, tal y",
                "  como está configurado, no está capturando tu criterio. Mira abajo qué",
                "  palabras y barrios distinguen a un grupo del otro y ajusta los pesos.",
                "",
            ]
        else:
            lineas += [f"La nota separa los dos grupos por {brecha:.0f} puntos.", ""]

    for etiqueta, clave in (
        ("Rasgos frecuentes en los que INTERESAN", "positivas_interesa"),
        ("Rasgos frecuentes en los DESCARTADOS", "positivas_descartado"),
        ("Pegas en los DESCARTADOS", "negativas_descartado"),
    ):
        if items := a[clave]:
            lineas.append(f"{etiqueta}: " + ", ".join(f"{w} (×{n})" for w, n in items))

    lineas.append("")
    for etiqueta, clave in (
        ("Barrios que interesan", "barrios_interesa"),
        ("Barrios descartados", "barrios_descartado"),
    ):
        if items := a[clave]:
            lineas.append(f"{etiqueta}: " + ", ".join(f"{b} (×{n})" for b, n in items))

    return "\n".join(lineas)
