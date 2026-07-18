"""El conector de RapidAPI, probado contra respuestas grabadas.

Los fixtures están copiados de una respuesta real del playground del proveedor,
no inventados: es un revendedor y el formato lo decide él, así que adivinarlo
sería justo el error que estos tests existen para evitar.

No toca la red, no gasta cuota y no duerme (el freno de 1 req/s está mockeado).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cuota import Cuota, CuotaAgotada
from src.sources import rapidapi
from src.sources.rapidapi import (
    IdealistaRapidApiSource,
    ProveedorCaido,
    RapidApiError,
    _planta,
    _tipo,
)
from src.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(nombre: str) -> dict:
    return json.loads((FIXTURES / nombre).read_text(encoding="utf-8"))


class RespuestaFalsa:
    def __init__(self, datos, status=200, texto=""):
        self._datos = datos
        self.status_code = status
        self.text = texto or json.dumps(datos)

    def json(self):
        return self._datos

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


@pytest.fixture
def sin_esperas(monkeypatch):
    """El freno de 1 req/s es real en producción; en los tests solo lo contamos."""
    dormidas = []
    monkeypatch.setattr(rapidapi.time, "sleep", lambda s: dormidas.append(s))
    return dormidas


@pytest.fixture
def api(monkeypatch, sin_esperas):
    """Sustituye requests.get por las dos páginas grabadas."""
    llamadas = []

    def get(url, **kwargs):
        params = kwargs.get("params") or {}
        llamadas.append((url, params, kwargs.get("headers") or {}))
        return RespuestaFalsa(_fixture(f"rapidapi_pagina{int(params.get('page', 1))}.json"))

    monkeypatch.setattr(rapidapi.requests, "get", get)
    monkeypatch.setenv("RAPIDAPI_KEY", "clave-de-prueba")
    return llamadas


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


@pytest.fixture
def config() -> dict:
    return {
        "api": {
            "cuota_mensual": 15_500,
            "reserva": 0,
            "paginas_max": 60,
            "busqueda": {
                "center": "40.4950,-3.7000",
                "radius_km": 5,
                "precio_max": 1_400_000,
                "m2_min": 90,
                "habitaciones_min": 3,
                "idioma": "es",
            },
        }
    }


@pytest.fixture
def source(store, config, api):
    return IdealistaRapidApiSource(config, Cuota(store, "idealista", config))


def _params(api) -> dict:
    return api[0][1]


# --- mapeo -------------------------------------------------------------------


def test_pagina_las_dos_paginas(source):
    assert [l.property_code for l in source.fetch()] == ["106712345", "106799999", "106788888"]


def test_mapea_los_campos(source):
    piso = source.fetch()[0]
    assert (piso.precio, piso.m2, piso.habitaciones, piso.banos) == (795_000, 128, 3, 2)
    assert (piso.tipo, piso.planta, piso.ascensor, piso.exterior) == ("piso", 4, True, True)
    assert (piso.barrio, piso.lat, piso.lon) == ("Montecarmelo", 40.497, -3.708)
    assert piso.foto_url.startswith("https://img4.idealista.com/")
    assert "terraza" in piso.descripcion


def test_el_codigo_es_el_de_idealista(source):
    """De esto cuelga el histórico entero.

    Si el revendedor se inventara los IDs en vez de pasar los de Idealista, cada
    día entraría todo como nuevo, el histórico de precios no se construiría jamás
    y no saltaría ningún error: solo dejaría de funcionar en silencio.
    """
    piso = source.fetch()[0]
    assert piso.property_code in piso.url


def test_saca_el_envoltorio_data(source):
    """La respuesta viene como {"success": true, "data": {...}}, no como el oficial."""
    assert len(source.fetch()) == 3


def test_lee_la_lista_tanto_si_es_listings_como_elementList(source, monkeypatch):
    """`/property-search-by-coordinates` llama a la lista `listings`, no `elementList`.

    Comprobado contra la API real el 2026-07-18. El primer JSON de ejemplo venía de
    `/property-search`, que sí usa `elementList`; por eso se aceptan las dos.
    """
    from src.sources.rapidapi import _anuncios

    uno = {"propertyCode": "X"}
    assert _anuncios({"listings": [uno]}) == [uno]
    assert _anuncios({"elementList": [uno]}) == [uno]
    assert _anuncios({}) == []


def test_un_anuncio_roto_no_tumba_el_barrido(source):
    """Traer esa página nos ha costado cuota: no la tiramos por un anuncio malo."""
    listings = source.fetch()
    assert "SIN-PRECIO" not in [l.property_code for l in listings]
    assert len(listings) == 3, "los buenos siguen ahí"


@pytest.mark.parametrize(
    "floor,esperado",
    [("4", 4), ("bj", 0), ("ss", -1), ("st", -1), ("en", 0), (None, 0), ("?", 0)],
)
def test_traduce_la_planta(floor, esperado):
    assert _planta(floor) == esperado


@pytest.mark.parametrize(
    "elemento,esperado",
    [
        ({"propertyType": "flat"}, "piso"),
        ({"propertyType": "duplex"}, "duplex"),
        ({"propertyType": "penthouse"}, "atico"),
        ({"propertyType": "semidetachedHouse"}, "adosado"),
        ({"propertyType": "chalet"}, "chalet"),
        # Este proveedor no manda `detailedType`, pero si algún día lo añade debe ganar.
        ({"propertyType": "chalet", "detailedType": {"subTypology": "terracedHouse"}}, "adosado"),
        ({}, "piso"),
    ],
)
def test_traduce_el_tipo(elemento, esperado):
    assert _tipo(elemento) == esperado


# --- parámetros de la petición -----------------------------------------------


def test_pide_las_descripciones_en_espanol(source, api):
    """El defecto de la API es `en`, y con texto en inglés el scoring de texto
    se queda clavado en la nota base para todos los anuncios, sin dar error."""
    source.fetch()
    assert _params(api)["language"] == "es"


def test_pide_cincuenta_por_pagina(source, api):
    """El defecto del proveedor es 30; 50 es el máximo y son un 40% menos de páginas."""
    source.fetch()
    assert _params(api)["result_count"] == 50


def test_parte_el_centro_en_latitud_y_longitud(source, api):
    source.fetch()
    assert (_params(api)["latitude"], _params(api)["longitude"]) == (40.495, -3.7)
    assert _params(api)["radius_km"] == 5


def test_pide_una_busqueda_mas_ancha_que_los_filtros(source, api):
    """Sin esto, el chalet de 1,3 M nunca se guarda y su bajada a 1,05 M parece una novedad."""
    source.fetch()
    assert _params(api)["max_price"] == 1_400_000
    assert _params(api)["min_size"] == 90


def test_no_filtra_por_exterior_ni_por_barrio_en_la_api(source, api):
    """Filtrar en el servidor sesgaría la mediana de €/m² con nuestros propios gustos."""
    source.fetch()
    params = _params(api)
    assert "filters" not in params
    assert not any("exterior" in str(k).lower() for k in params)


def test_pagina_de_la_mas_vieja_a_la_mas_nueva(source, api):
    """Con `newest`, un anuncio publicado a media paginación empuja a los demás y
    nos saltaríamos uno. Con `oldest` lo nuevo cae al final y las páginas no se mueven."""
    source.fetch()
    assert _params(api)["sort_order"] == "oldest"


def test_manda_las_cabeceras_de_rapidapi(source, api):
    source.fetch()
    _, _, cabeceras = api[0]
    assert cabeceras["X-RapidAPI-Key"] == "clave-de-prueba"
    assert cabeceras["X-RapidAPI-Host"] == "idealista17.p.rapidapi.com"


def test_sin_clave_no_sale_a_la_red(store, config, api, monkeypatch):
    monkeypatch.delenv("RAPIDAPI_KEY")
    source = IdealistaRapidApiSource(config, Cuota(store, "idealista", config))

    with pytest.raises(RapidApiError, match="RAPIDAPI_KEY"):
        source.fetch()


# --- freno y cuota -----------------------------------------------------------


def test_frena_a_una_peticion_por_segundo(source, sin_esperas):
    """El plan PRO corta a 1 req/s. Sin freno, paginar en bucle da 429 a la tercera."""
    source.fetch()
    assert sin_esperas, "no ha esperado entre páginas"
    assert all(s <= rapidapi.INTERVALO_MIN_S for s in sin_esperas)


def test_cada_pagina_gasta_cuota(source, store):
    source.fetch()
    assert store.llamadas_del_mes("idealista") == 2, "dos páginas, y sin token que pagar"


def test_sin_cuota_no_sale_a_la_red(store, config, api):
    config["api"]["cuota_mensual"] = 0
    source = IdealistaRapidApiSource(config, Cuota(store, "idealista", config))

    with pytest.raises(CuotaAgotada):
        source.fetch()
    assert api == [], "no se ha llegado a llamar a nadie"


def test_si_se_agota_a_media_paginacion_devuelve_lo_que_lleva(store, config, api, caplog):
    config["api"]["cuota_mensual"] = 1  # solo la página 1
    source = IdealistaRapidApiSource(config, Cuota(store, "idealista", config))

    listings = source.fetch()

    assert [l.property_code for l in listings] == ["106712345", "106799999"]
    assert "cuota agotada en la página 2" in caplog.text


def test_el_tope_de_paginas_te_salva_de_fundirte_el_mes(store, config, api, caplog, monkeypatch):
    """Si se cae `precio_max` del config, la búsqueda pasa de ~30 páginas a 5.571."""
    gorda = _fixture("rapidapi_pagina1.json")
    gorda["data"]["totalPages"] = 5571
    monkeypatch.setattr(rapidapi.requests, "get", lambda url, **kw: RespuestaFalsa(gorda))

    config["api"]["paginas_max"] = 3
    source = IdealistaRapidApiSource(config, Cuota(store, "idealista", config))
    source.fetch()

    assert store.llamadas_del_mes("idealista") == 3, "se ha parado en el tope"
    assert "demasiado ancha" in caplog.text


# --- errores del proveedor ---------------------------------------------------


def test_un_429_se_explica(store, config, api, monkeypatch):
    monkeypatch.setattr(
        rapidapi.requests, "get", lambda url, **kw: RespuestaFalsa({}, status=429)
    )
    source = IdealistaRapidApiSource(config, Cuota(store, "idealista", config))

    with pytest.raises(RapidApiError, match="429"):
        source.fetch()


def test_el_proveedor_caido_se_distingue_de_un_fallo_nuestro(store, config, api, monkeypatch):
    """RapidAPI tuvo la API apagada al montar esto (405, comprobado el 2026-07-16).

    Tiene excepción propia y código de salida propio para que el cron diario no lo
    trate como un error nuestro: mientras dure, no toca nada y reintenta al día
    siguiente, y cuando la reactiven arranca solo sin que nadie venga a tocar nada.
    """
    caida = RespuestaFalsa(
        {"message": "The API provider has disabled request access to the API."},
        status=405,
        texto='{"message":"The API provider has disabled request access to the API."}',
    )
    monkeypatch.setattr(rapidapi.requests, "get", lambda url, **kw: caida)
    source = IdealistaRapidApiSource(config, Cuota(store, "idealista", config))

    with pytest.raises(ProveedorCaido, match="apagada del lado del proveedor"):
        source.fetch()


def test_un_405_por_otro_motivo_no_se_confunde_con_la_caida(store, config, api, monkeypatch):
    """Si algún día el 405 fuera de verdad un método mal puesto, hay que verlo."""
    monkeypatch.setattr(
        rapidapi.requests,
        "get",
        lambda url, **kw: RespuestaFalsa({"message": "Method Not Allowed"}, status=405),
    )
    source = IdealistaRapidApiSource(config, Cuota(store, "idealista", config))

    with pytest.raises(AssertionError):  # raise_for_status del test doble
        source.fetch()


def test_success_false_no_pasa_por_barrido_vacio(store, config, api, monkeypatch):
    """Devuelve HTTP 200 con success:false. Si lo tragáramos, un fallo del proveedor
    daría por desaparecido el catálogo entero y te llegaría un email absurdo."""
    monkeypatch.setattr(
        rapidapi.requests,
        "get",
        lambda url, **kw: RespuestaFalsa({"success": False, "message": "quota exceeded"}),
    )
    source = IdealistaRapidApiSource(config, Cuota(store, "idealista", config))

    with pytest.raises(RapidApiError, match="success=false"):
        source.fetch()
