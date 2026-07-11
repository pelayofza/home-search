from __future__ import annotations

import pytest

from src.filters import apply_filters, matches


def test_acepta_anuncio_que_cumple_todo(listing_factory, config):
    assert matches(listing_factory(), config["filtros"]) is True


@pytest.mark.parametrize(
    "override",
    [
        {"precio": 1_450_000},  # se pasa de precio_max
        {"m2": 95},  # pocos metros
        {"habitaciones": 2},
        {"banos": 0},
        {"planta": 0},  # un piso en un bajo
        {"exterior": False},
        {"tipo": "loft"},  # tipo fuera de la lista
        {"barrio": "Villaverde"},  # fuera de barrios_incluidos
        {"descripcion": "Piso en SUBASTA judicial"},  # palabra excluida
    ],
    ids=[
        "precio",
        "m2",
        "habitaciones",
        "banos",
        "planta",
        "exterior",
        "tipo",
        "barrio",
        "palabra-excluida",
    ],
)
def test_descarta_por_cada_criterio(listing_factory, config, override):
    assert matches(listing_factory(**override), config["filtros"]) is False


# --- planta: solo aplica a los tipos que tienen planta -----------------------


def test_chalet_en_planta_baja_pasa(listing_factory, config):
    """Un chalet siempre está a pie de calle: planta_min no debe descartarlo."""
    chalet = listing_factory(tipo="chalet", planta=0, ascensor=False)
    assert matches(chalet, config["filtros"]) is True


def test_adosado_en_planta_baja_pasa(listing_factory, config):
    assert matches(listing_factory(tipo="adosado", planta=0), config["filtros"]) is True


def test_piso_en_planta_baja_no_pasa(listing_factory, config):
    assert matches(listing_factory(tipo="piso", planta=0), config["filtros"]) is False


def test_atico_si_respeta_planta_min(listing_factory, config):
    assert matches(listing_factory(tipo="atico", planta=0), config["filtros"]) is False


# --- exterior ----------------------------------------------------------------


def test_exterior_desconocido_se_descarta_si_se_exige(listing_factory, config):
    """None = el anuncio no lo dice. Exigiendo exterior, no nos vale."""
    assert matches(listing_factory(exterior=None), config["filtros"]) is False


def test_exterior_null_en_config_acepta_todo(listing_factory, config):
    config["filtros"]["exterior"] = None
    for valor in (True, False, None):
        assert matches(listing_factory(exterior=valor), config["filtros"]) is True


# --- resto -------------------------------------------------------------------


def test_ascensor_null_acepta_ambos(listing_factory, config):
    assert matches(listing_factory(ascensor=False), config["filtros"]) is True
    assert matches(listing_factory(ascensor=True), config["filtros"]) is True


def test_ascensor_obligatorio_descarta_los_que_no_tienen(listing_factory, config):
    config["filtros"]["ascensor"] = True
    assert matches(listing_factory(ascensor=False), config["filtros"]) is False


def test_barrio_ignora_tildes_y_mayusculas(listing_factory, config):
    assert matches(listing_factory(barrio="PINAR DE CHAMARTIN"), config["filtros"]) is True


def test_barrio_excluido_gana(listing_factory, config):
    config["filtros"]["barrios_excluidos"] = ["Mirasierra"]
    assert matches(listing_factory(barrio="Mirasierra"), config["filtros"]) is False


def test_listas_vacias_no_filtran(listing_factory, config):
    config["filtros"]["barrios_incluidos"] = []
    config["filtros"]["tipos"] = []
    assert matches(listing_factory(barrio="Usera", tipo="loft"), config["filtros"]) is True


def test_criterio_ausente_no_filtra(listing_factory):
    filtros = {"precio_max": 1_100_000}
    assert matches(listing_factory(m2=10, exterior=False, planta=-2), filtros) is True


def test_apply_filters_conserva_el_orden(listing_factory, config):
    listings = [
        listing_factory(property_code="A"),
        listing_factory(property_code="B", precio=9_990_000),  # se cae
        listing_factory(property_code="C"),
    ]
    assert [l.property_code for l in apply_filters(listings, config)] == ["A", "C"]


def test_apply_filters_sin_filtros_devuelve_todo(listing_factory):
    listings = [listing_factory(precio=9_000_000)]
    assert apply_filters(listings, {}) == listings
