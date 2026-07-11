from __future__ import annotations

import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from src.models import Score
from src.store import Store
from src.web import app as web


@pytest.fixture
def cliente(tmp_path, monkeypatch, listing_factory):
    db = tmp_path / "web.db"
    with Store(db) as s:
        caro = listing_factory(property_code="A", precio=900_000, barrio="Mirasierra")
        s.sync("mock", [caro, listing_factory(property_code="B", barrio="Las Tablas")])
        s.sync("mock", [replace(caro, precio=850_000), listing_factory(property_code="B")])
        s.guardar_scores(
            "mock",
            {"A": Score(total=82.0, precio=90.0, detalle={}), "B": Score(total=41.0, detalle={})},
            "hash",
        )
        s.snapshot_mercado(min_muestra=1)

    monkeypatch.setattr(web, "DB", str(db))
    return TestClient(web.app)


def codigos(html: str) -> list[str]:
    return re.findall(r"/anuncio/mock/(\w+)", html)


def test_el_panel_carga(cliente):
    r = cliente.get("/")
    assert r.status_code == 200
    assert "home-search" in r.text


def test_la_tabla_ordena_por_nota(cliente):
    assert codigos(cliente.get("/tabla").text) == ["A", "B"]


def test_filtra_por_barrio(cliente):
    assert codigos(cliente.get("/tabla", params={"barrio": "Mirasierra"}).text) == ["A"]


def test_filtra_por_nota_minima(cliente):
    assert codigos(cliente.get("/tabla", params={"min_score": 60}).text) == ["A"]


@pytest.mark.parametrize(
    "params",
    [
        {"barrio": "", "orden": "score", "min_score": ""},
        {"barrio": "", "orden": "score", "min_score": "", "solo_bajadas": "true"},
        {"barrio": "", "orden": "score", "min_score": "no-es-un-numero"},
    ],
    ids=["formulario-vacio", "con-checkbox", "basura-en-el-numero"],
)
def test_los_filtros_vacios_no_revientan(cliente, params):
    """El navegador manda `min_score=` cuando el campo está vacío.

    Tipar eso como float daba un 422, HTMX no hacía swap y la tabla se quedaba
    colgada en "Cargando…" para siempre. Mis tests no lo veían porque yo siempre
    mandaba el parámetro con valor o no lo mandaba: nunca vacío.
    """
    r = cliente.get("/tabla", params=params)
    assert r.status_code == 200
    assert codigos(r.text)


def test_solo_bajadas(cliente):
    assert codigos(cliente.get("/tabla", params={"solo_bajadas": "true"}).text) == ["A"]


def test_la_ficha_enlaza_al_anuncio_original(cliente):
    html = cliente.get("/anuncio/mock/A").text
    assert 'href="https://example.com/X-1"' in html
    assert 'target="_blank"' in html


def test_la_ficha_dibuja_el_historico_escalonado(cliente):
    html = cliente.get("/anuncio/mock/A").text
    puntos = re.search(r'points="([^"]+)"', html).group(1).split()
    assert len(puntos) == 3, "dos escalones se dibujan con 3 vértices: horizontal y vertical"
    assert "900.000 €" in html and "850.000 €" in html


def test_la_ficha_enseña_el_margen_de_negociacion(cliente):
    html = cliente.get("/anuncio/mock/A").text
    assert "Margen de negociación" in html
    assert "ya ha bajado 1 vez" in html
    assert "heurística, no una predicción" in html


def test_anuncio_inexistente(cliente):
    assert cliente.get("/anuncio/mock/NO-EXISTE").status_code == 404


# --- valoraciones ------------------------------------------------------------


def test_votar_y_deshacer(cliente):
    assert cliente.post("/opinion/mock/A?valoracion=interesa").status_code == 200
    assert 'class="boton voto activo"' in cliente.get("/anuncio/mock/A").text

    cliente.post("/opinion/mock/A?valoracion=interesa")  # segundo clic = deshacer
    assert 'class="boton voto activo"' not in cliente.get("/anuncio/mock/A").text


def test_lo_descartado_desaparece_de_la_tabla(cliente):
    cliente.post("/opinion/mock/B?valoracion=descartado")

    assert codigos(cliente.get("/tabla").text) == ["A"]
    assert codigos(cliente.get("/tabla", params={"ver_descartados": "true"}).text) == ["A", "B"]


def test_una_valoracion_inventada_se_rechaza(cliente):
    assert cliente.post("/opinion/mock/A?valoracion=quizas").status_code == 422


def test_votar_un_anuncio_inexistente(cliente):
    assert cliente.post("/opinion/mock/NO-EXISTE?valoracion=interesa").status_code == 404


# --- mercado y salud ---------------------------------------------------------


def test_el_mercado_avisa_si_no_hay_serie(cliente):
    """Con una sola foto no hay línea que dibujar, y hay que decirlo, no fingirla."""
    html = cliente.get("/mercado").text
    assert "Todavía no hay suficientes fotos" in html


def test_salud(cliente):
    assert cliente.get("/salud").json()["anuncios_activos"] == 2


# --- la promesa de solo lectura ----------------------------------------------


def test_la_ruta_de_lectura_no_puede_escribir(tmp_path, listing_factory):
    """No es una promesa mía: es el driver de SQLite quien lo impide."""
    db = tmp_path / "ro.db"
    with Store(db) as s:
        s.sync("mock", [listing_factory(property_code="A")])

    with Store(db, readonly=True) as ro:
        with pytest.raises(Exception, match="readonly database"):
            ro.conn.execute("UPDATE listings SET precio = 1")
