"""El conector, probado contra respuestas grabadas. No toca la red ni gasta cuota."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cuota import Cuota, CuotaAgotada
from src.sources import idealista
from src.sources.idealista import IdealistaSource, _planta, _tipo
from src.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(nombre: str) -> dict:
    return json.loads((FIXTURES / nombre).read_text(encoding="utf-8"))


class RespuestaFalsa:
    def __init__(self, datos, status=200):
        self._datos = datos
        self.status_code = status

    def json(self):
        return self._datos

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


@pytest.fixture
def api(monkeypatch):
    """Sustituye requests.post: token + las dos páginas grabadas."""
    llamadas = []

    def post(url, **kwargs):
        llamadas.append((url, kwargs.get("data") or {}))
        if url == idealista.TOKEN_URL:
            return RespuestaFalsa({"access_token": "tok-123", "expires_in": 3600})
        pagina = int((kwargs.get("data") or {}).get("numPage", 1))
        return RespuestaFalsa(_fixture(f"idealista_pagina{pagina}.json"))

    monkeypatch.setattr(idealista.requests, "post", post)
    monkeypatch.setenv("IDEALISTA_API_KEY", "key")
    monkeypatch.setenv("IDEALISTA_API_SECRET", "secret")
    return llamadas


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


@pytest.fixture
def config() -> dict:
    return {
        "api": {
            "cuota_mensual": 100,
            "reserva": 0,
            "busqueda": {"precio_max": 1_400_000, "m2_min": 90, "habitaciones_min": 3},
        }
    }


@pytest.fixture
def source(store, config, api):
    return IdealistaSource(config, Cuota(store, "idealista", config))


# --- mapeo -------------------------------------------------------------------


def test_pagina_las_dos_paginas(source):
    listings = source.fetch()
    assert [l.property_code for l in listings] == ["106712345", "106799999", "106788888"]


def test_mapea_los_campos(source):
    piso = source.fetch()[0]
    assert (piso.precio, piso.m2, piso.habitaciones, piso.banos) == (795_000, 128, 3, 2)
    assert (piso.tipo, piso.planta, piso.ascensor, piso.exterior) == ("piso", 4, True, True)
    assert (piso.barrio, piso.lat, piso.lon) == ("Montecarmelo", 40.497, -3.708)
    assert piso.url.startswith("https://www.idealista.com/inmueble/")


def test_un_anuncio_roto_no_tumba_el_barrido(source, caplog):
    """Traer esa página nos ha costado cuota: no la tiramos por un anuncio malo."""
    listings = source.fetch()
    assert "SIN-PRECIO" not in [l.property_code for l in listings]
    assert len(listings) == 3, "los tres buenos siguen ahí"


@pytest.mark.parametrize(
    "floor,esperado",
    [("4", 4), ("bj", 0), ("ss", -1), ("st", -1), ("en", 0), (None, 0), ("?", 0)],
)
def test_traduce_la_planta(floor, esperado):
    assert _planta(floor) == esperado


@pytest.mark.parametrize(
    "elemento,esperado",
    [
        ({"detailedType": {"typology": "flat"}}, "piso"),
        ({"detailedType": {"typology": "chalet", "subTypology": "semidetachedHouse"}}, "adosado"),
        ({"detailedType": {"typology": "chalet", "subTypology": "independantHouse"}}, "chalet"),
        ({"propertyType": "penthouse"}, "atico"),
        ({}, "piso"),
    ],
)
def test_traduce_el_tipo(elemento, esperado):
    assert _tipo(elemento) == esperado


# --- cuota -------------------------------------------------------------------


def test_cada_pagina_gasta_cuota(source, store):
    source.fetch()
    # 1 token + 2 páginas
    assert store.llamadas_del_mes("idealista") == 3


def test_el_token_se_cachea(source, api):
    source.fetch()
    source.fetch()
    tokens = [url for url, _ in api if url == idealista.TOKEN_URL]
    assert len(tokens) == 1, "pedir token por página duplicaría el gasto"


def test_sin_cuota_no_sale_a_la_red(store, config, api):
    config["api"]["cuota_mensual"] = 0
    source = IdealistaSource(config, Cuota(store, "idealista", config))

    with pytest.raises(CuotaAgotada):
        source.fetch()
    assert api == [], "no se ha llegado a llamar a nadie"


def test_si_se_agota_a_media_paginacion_devuelve_lo_que_lleva(store, config, api, caplog):
    config["api"]["cuota_mensual"] = 2  # token + página 1, y se acabó
    source = IdealistaSource(config, Cuota(store, "idealista", config))

    listings = source.fetch()

    assert [l.property_code for l in listings] == ["106712345", "106799999"]
    assert "cuota agotada en la página 2" in caplog.text


def test_solo_recientes_pide_la_ultima_semana(source, api):
    source.fetch(solo_recientes=True)
    _, params = next((u, d) for u, d in api if u == idealista.SEARCH_URL)
    assert params["sinceDate"] == "W"


def test_el_barrido_completo_no_filtra_por_fecha(source, api):
    source.fetch(solo_recientes=False)
    _, params = next((u, d) for u, d in api if u == idealista.SEARCH_URL)
    assert "sinceDate" not in params


def test_pide_una_busqueda_mas_ancha_que_los_filtros(source, api):
    """Sin esto, el piso de 1,3 M nunca se guarda y su bajada a 1,05 M parece una novedad."""
    source.fetch()
    _, params = next((u, d) for u, d in api if u == idealista.SEARCH_URL)
    assert params["maxPrice"] == 1_400_000
    assert params["minSize"] == 90
    assert params["maxItems"] == 50
