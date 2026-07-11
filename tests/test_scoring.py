from __future__ import annotations

import pytest

from src import scoring
from src.scoring import localizacion, precio, texto


@pytest.fixture
def cfg_scoring() -> dict:
    return {
        "scoring": {
            "pesos": {"texto": 0.30, "precio": 0.40, "localizacion": 0.30},
            "texto": {
                "base": 50,
                "positivas": {"terraza": 10, "reformado": 8},
                "negativas": {"a reformar": -12, "patio interior": -8},
            },
            "precio": {"rango_pct": 25, "saturacion_pct": 35, "min_muestra": 8},
            "localizacion": {
                "umbrales_m": {
                    "metro": {"optimo": 400, "malo": 1500, "peso": 0.5},
                    "parque": {"optimo": 300, "malo": 1200, "peso": 0.5},
                }
            },
        }
    }


@pytest.fixture
def pois() -> dict:
    return {
        "bbox": [40.44, -3.78, 40.58, -3.60],
        "puntos": {
            "metro": [{"lat": 40.4867, "lon": -3.6944, "nombre": "Montecarmelo"}],
            "parque": [
                {
                    "lat": 40.49,
                    "lon": -3.70,
                    "nombre": "Parque grande",
                    "bbox": [40.485, -3.71, 40.495, -3.69],
                }
            ],
        },
    }


# --- texto -------------------------------------------------------------------


def test_texto_suma_las_positivas(cfg_scoring):
    reglas = cfg_scoring["scoring"]["texto"]
    nota, detalle = texto.puntua("Piso reformado con terraza", reglas)
    assert nota == 50 + 8 + 10
    assert sorted(detalle["positivas"]) == ["reformado", "terraza"]


def test_texto_resta_las_negativas(cfg_scoring):
    nota, detalle = texto.puntua("Piso para vivir, a reformar", cfg_scoring["scoring"]["texto"])
    assert nota == 50 - 12
    assert detalle["negativas"] == ["a reformar"]


def test_texto_ignora_tildes_y_mayusculas(cfg_scoring):
    nota, _ = texto.puntua("PISO REFORMADO", cfg_scoring["scoring"]["texto"])
    assert nota == 58


def test_texto_recorta_a_cien(cfg_scoring):
    reglas = {"base": 50, "positivas": {"terraza": 90}, "negativas": {}}
    nota, _ = texto.puntua("con terraza", reglas)
    assert nota == 100


# --- precio ------------------------------------------------------------------


def test_precio_en_la_mediana_saca_cincuenta(listing_factory, cfg_scoring):
    l = listing_factory(precio=500_000, m2=100)  # 5.000 €/m²
    nota, _ = precio.puntua(l, 5000, 20, cfg_scoring["scoring"]["precio"])
    assert nota == 50


def test_precio_por_debajo_puntua_mas(listing_factory, cfg_scoring):
    l = listing_factory(precio=500_000, m2=100)  # 5.000 €/m²
    nota, detalle = precio.puntua(l, 6250, 20, cfg_scoring["scoring"]["precio"])  # -20%
    assert nota == 90
    assert detalle["desviacion_pct"] == -20.0


def test_un_descuento_absurdo_no_puntua_mas_que_uno_bueno(listing_factory, cfg_scoring):
    """Un -60% no es un chollo: es una ruina. La saturación evita que lidere el ranking."""
    cfg = cfg_scoring["scoring"]["precio"]
    sospechoso = listing_factory(precio=200_000, m2=100)  # -60% vs 5.000
    bueno = listing_factory(precio=325_000, m2=100)  # -35% vs 5.000

    nota_sospechoso, detalle = precio.puntua(sospechoso, 5000, 20, cfg)
    nota_bueno, _ = precio.puntua(bueno, 5000, 20, cfg)

    assert nota_sospechoso == nota_bueno
    assert detalle["saturado"] is True


def test_sin_mediana_no_hay_nota_de_precio(listing_factory, cfg_scoring):
    nota, _ = precio.puntua(listing_factory(), None, 0, cfg_scoring["scoring"]["precio"])
    assert nota is None, "inventar un 50 sería ruido disfrazado de neutralidad"


# --- localización ------------------------------------------------------------


def test_distancia_al_metro_de_al_lado(listing_factory, pois, cfg_scoring):
    encima = listing_factory(lat=40.4867, lon=-3.6944)
    dists = localizacion.distancias(encima, pois)
    assert dists["metro"] < 10


def test_dentro_de_un_parque_la_distancia_es_cero(listing_factory, pois, cfg_scoring):
    """El parque es un polígono: medir al centroide mentiría a quien lo tiene enfrente."""
    dentro = listing_factory(lat=40.49, lon=-3.70)
    dists = localizacion.distancias(dentro, pois)
    assert dists["parque"] == 0


def test_sin_coordenadas_no_hay_subscore(listing_factory, pois, cfg_scoring):
    sin_geo = listing_factory(lat=None, lon=None)
    assert localizacion.distancias(sin_geo, pois) is None


def test_fuera_del_bbox_no_se_inventa_distancia(listing_factory, pois, cfg_scoring):
    """Un anuncio en Alcobendas diría 'metro a 8 km' y nadie se enteraría del error."""
    lejos = listing_factory(lat=41.0, lon=-3.70)
    assert localizacion.distancias(lejos, pois) is None


def test_la_nota_decae_con_la_distancia(cfg_scoring):
    cfg = cfg_scoring["scoring"]["localizacion"]
    cerca, _ = localizacion.puntua({"metro": 300, "parque": 200}, cfg)
    lejos, _ = localizacion.puntua({"metro": 2000, "parque": 2000}, cfg)
    medio, _ = localizacion.puntua({"metro": 950, "parque": 750}, cfg)

    assert cerca == 100
    assert lejos == 0
    assert 45 < medio < 55


# --- total -------------------------------------------------------------------


def test_el_total_pondera_las_tres_dimensiones(listing_factory, pois, cfg_scoring):
    l = listing_factory(precio=500_000, m2=100, lat=40.4867, lon=-3.6944, descripcion="con terraza")
    score = scoring.puntua(
        l, mediana=5000, muestra=20, fuente_mediana="propia", pois=pois, config=cfg_scoring
    )

    assert score.dimensiones == 3
    assert score.texto == 60
    assert score.precio == 50
    esperado = 0.30 * score.texto + 0.40 * score.precio + 0.30 * score.localizacion
    assert score.total == pytest.approx(round(esperado, 1))


def test_sin_geo_se_renormalizan_los_pesos(listing_factory, cfg_scoring):
    """Un anuncio sin coordenadas no debe hundirse por un fallo nuestro, no suyo."""
    l = listing_factory(precio=500_000, m2=100, lat=None, lon=None, descripcion="con terraza")
    score = scoring.puntua(
        l, mediana=5000, muestra=20, fuente_mediana="propia", pois=None, config=cfg_scoring
    )

    assert score.localizacion is None
    assert score.dimensiones == 2
    # 60 con peso 0,30 y 50 con peso 0,40 -> (18 + 20) / 0,70
    assert score.total == pytest.approx(54.3, abs=0.1)
    assert score.detalle["dimensiones"] == ["precio", "texto"]


def test_el_hash_cambia_si_cambian_los_pesos(cfg_scoring):
    antes = scoring.config_hash(cfg_scoring)
    cfg_scoring["scoring"]["pesos"]["precio"] = 0.9
    assert scoring.config_hash(cfg_scoring) != antes
