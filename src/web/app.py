"""Panel web de resultados. Solo lectura: nunca llama a Idealista.

    uvicorn src.web.app:app --reload

Sobre el "tiempo real": HTMX repinta la tabla cada pocos minutos, pero los datos
solo cambian cuando corre la búsqueda diaria. La web está al día respecto a la
BD, no respecto a Idealista.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.filters import load_config
from src.store import Store

AQUI = Path(__file__).parent

app = FastAPI(title="home-search")
app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")

plantillas = Jinja2Templates(directory=AQUI / "templates")
plantillas.env.filters["miles"] = lambda n: f"{int(n):,}".replace(",", ".")

CONFIG = load_config()
DB = (CONFIG.get("almacenamiento") or {}).get("db_path", "data/listings.db")


def get_store():
    """Conexión de solo lectura, garantizada por el driver y no por mi buena fe."""
    if not Path(DB).exists():
        raise HTTPException(503, f"todavía no hay base de datos en {DB}: ejecuta main.py")
    store = Store(DB, readonly=True)
    try:
        yield store
    finally:
        store.close()


def get_escritor():
    """Conexión de escritura, SOLO para guardar tus valoraciones.

    Es la única grieta en el "la web no escribe". Está separada a propósito: si
    algún día alguien añade un endpoint que muta anuncios o precios, tendrá que
    pedir explícitamente esta dependencia, y eso se ve en la revisión.
    """
    if not Path(DB).exists():
        raise HTTPException(503, f"todavía no hay base de datos en {DB}: ejecuta main.py")
    store = Store(DB)
    try:
        yield store
    finally:
        store.close()


Almacen = Annotated[Store, Depends(get_store)]
Escritor = Annotated[Store, Depends(get_escritor)]


@app.get("/", response_class=HTMLResponse)
def panel(request: Request, store: Almacen):
    return plantillas.TemplateResponse(
        request,
        "index.html",
        {"barrios": store.barrios(), "total": store.count(solo_activos=True)},
    )


def _numero(valor: str) -> float | None:
    """Un formulario HTML manda `min_score=` cuando el campo está vacío.

    Si se tipa como `float | None`, FastAPI intenta convertir la cadena vacía,
    falla con un 422, y HTMX (que no hace swap ante un 4xx) deja la tabla
    colgada en "Cargando…" para siempre. Aquí lo vacío es None, sin drama.
    """
    valor = (valor or "").strip()
    if not valor:
        return None
    try:
        return float(valor)
    except ValueError:
        return None


@app.get("/tabla", response_class=HTMLResponse)
def tabla(
    request: Request,
    store: Almacen,
    barrio: str = "",
    min_score: str = "",
    solo_bajadas: bool = False,
    ver_descartados: bool = False,
    orden: str = "score",
):
    anuncios = store.buscar(
        barrio=barrio or None,
        min_score=_numero(min_score),
        solo_bajadas=solo_bajadas,
        ocultar_descartados=not ver_descartados,
        orden=orden,
    )
    return plantillas.TemplateResponse(
        request, "_tabla.html", {"anuncios": anuncios, "orden": orden}
    )


@app.post("/opinion/{source}/{property_code}", response_class=HTMLResponse)
def opinar(
    request: Request,
    store: Escritor,
    source: str,
    property_code: str,
    valoracion: str,  # por query, para no arrastrar python-multipart por dos botones
    vista: str = "fila",  # 'fila' desde la tabla, 'voto' desde la ficha
):
    """Guarda (o retira, si repites el mismo botón) tu veredicto sobre un anuncio."""
    if valoracion not in ("interesa", "descartado"):
        raise HTTPException(422, f"valoración desconocida: {valoracion!r}")

    actual = store.ficha(source, property_code)
    if actual is None:
        raise HTTPException(404, "ese anuncio no está en la base de datos")

    if actual["opinion"] == valoracion:
        store.borrar_opinion(source, property_code)  # segundo clic = deshacer
    else:
        store.opinar(source, property_code, valoracion)

    # Se devuelve repintado solo el trozo afectado (la fila, o los botones de la
    # ficha): HTMX lo sustituye en su sitio y no se recarga la tabla ni se pierde
    # el scroll.
    anuncio = store.ficha(source, property_code)
    plantilla = "_voto.html" if vista == "voto" else "_fila.html"
    return plantillas.TemplateResponse(request, plantilla, {"a": anuncio})


@app.get("/anuncio/{source}/{property_code}", response_class=HTMLResponse)
def ficha(request: Request, store: Almacen, source: str, property_code: str):
    datos = store.ficha(source, property_code)
    if datos is None:
        raise HTTPException(404, "ese anuncio no está en la base de datos")
    return plantillas.TemplateResponse(
        request,
        "ficha.html",
        {"a": datos, "grafica": _sparkline(datos["historico"])},
    )


@app.get("/mercado", response_class=HTMLResponse)
def mercado(request: Request, store: Almacen, familia: str = "piso"):
    """Evolución de la mediana de €/m² por barrio: no solo qué comprar, también cuándo."""
    series = store.serie_mercado(familia)
    return plantillas.TemplateResponse(
        request,
        "mercado.html",
        {"familia": familia, "grafica": _series(series), "barrios": sorted(series)},
    )


@app.get("/salud")
def salud(store: Almacen) -> dict[str, Any]:
    return {
        "anuncios_activos": store.count(solo_activos=True),
        "anuncios_totales": store.count(),
        "db": DB,
    }


COLORES = ["#0b5ed7", "#15803d", "#b91c1c", "#a16207", "#7c3aed", "#0891b2", "#db2777"]


def _series(series: dict[str, list[tuple[str, float]]], ancho: int = 720, alto: int = 260) -> dict:
    """Una polilínea por barrio, escaladas a un eje común para poder compararlas."""
    todos = [v for puntos in series.values() for _, v in puntos]
    fechas = sorted({f for puntos in series.values() for f, _ in puntos})
    if not todos or len(fechas) < 2:
        return {"lineas": [], "fechas": fechas, "min": 0, "max": 0}

    lo, hi = min(todos), max(todos)
    rango = (hi - lo) or 1
    margen = 30

    def y(v: float) -> float:
        return margen + (alto - 2 * margen) * (1 - (v - lo) / rango)

    def x(fecha: str) -> float:
        i = fechas.index(fecha)
        return margen + (ancho - 2 * margen) * i / (len(fechas) - 1)

    lineas = []
    for i, (barrio, puntos) in enumerate(sorted(series.items())):
        lineas.append(
            {
                "barrio": barrio,
                "color": COLORES[i % len(COLORES)],
                "puntos": " ".join(f"{x(f):.1f},{y(v):.1f}" for f, v in puntos),
                "ultimo": round(puntos[-1][1]),
            }
        )

    return {
        "lineas": lineas,
        "fechas": fechas,
        "min": round(lo),
        "max": round(hi),
        "ancho": ancho,
        "alto": alto,
    }


def _sparkline(historico: list[tuple[str, int]], ancho: int = 560, alto: int = 160) -> dict:
    """Puntos de la gráfica de precios, ya escalados al viewBox.

    El histórico solo guarda escalones (el precio cuando CAMBIA), así que hay que
    dibujarlo como una función escalonada. Interpolar entre dos puntos sugeriría
    una bajada gradual que nunca ocurrió.
    """
    if not historico:
        return {"puntos": "", "min": 0, "max": 0, "escalones": []}

    precios = [p for _, p in historico]
    lo, hi = min(precios), max(precios)
    rango = (hi - lo) or 1
    margen = 20

    def y(precio: int) -> float:
        return margen + (alto - 2 * margen) * (1 - (precio - lo) / rango)

    def x(i: int) -> float:
        if len(historico) == 1:
            return ancho / 2
        return margen + (ancho - 2 * margen) * i / (len(historico) - 1)

    # Escalonada: horizontal hasta el siguiente cambio, luego vertical.
    puntos = []
    for i, precio in enumerate(precios):
        puntos.append((x(i), y(precio)))
        if i + 1 < len(precios):
            puntos.append((x(i + 1), y(precio)))

    return {
        "puntos": " ".join(f"{px:.1f},{py:.1f}" for px, py in puntos),
        "escalones": [
            {"x": x(i), "y": y(p), "precio": p, "fecha": ts[:10]}
            for i, (ts, p) in enumerate(historico)
        ],
        "min": lo,
        "max": hi,
        "ancho": ancho,
        "alto": alto,
    }
