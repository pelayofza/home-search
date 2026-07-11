from __future__ import annotations

import pytest

from src.geo.distancias import distancia_a_bbox, distancia_a_poligono, haversine, mas_cercano

# Una franja larga y diagonal, como el Canal Bajo de Madrid: un parque estrecho
# cuyo rectángulo envolvente cubre kilómetros de calles que no son parque.
FRANJA = [[[40.480, -3.720], [40.500, -3.700], [40.5005, -3.7005], [40.4805, -3.7205]]]
BBOX_FRANJA = [40.480, -3.7205, 40.5005, -3.700]


def test_haversine_conocido():
    # Sol -> Cibeles, poco más de 1 km en línea recta.
    d = haversine(40.4169, -3.7035, 40.4192, -3.6934)
    assert 800 < d < 950


def test_el_rectangulo_de_una_franja_diagonal_miente():
    """Este es el bug: el punto está a 1,4 km del parque, pero DENTRO de su rectángulo."""
    lat, lon = 40.4995, -3.7195  # esquina noroeste del rectángulo, lejos de la franja

    assert distancia_a_bbox(lat, lon, BBOX_FRANJA) == 0, "el rectángulo lo da por dentro"
    assert distancia_a_poligono(lat, lon, FRANJA) > 1000, "el polígono dice la verdad"


def test_dentro_del_poligono_la_distancia_es_cero():
    # En mitad de la franja: los dos bordes van de (40.480,-3.720) a (40.500,-3.700),
    # separados 0,0005° en diagonal, así que el interior está justo entre ambos.
    assert distancia_a_poligono(40.49025, -3.71025, FRANJA) == 0


def test_junto_al_borde_la_distancia_es_pequena():
    d = distancia_a_poligono(40.4895, -3.7095, FRANJA)  # a un lado de la franja
    assert 0 < d < 120


def test_mas_cercano_usa_el_poligono_si_lo_hay():
    lejos_pero_en_el_bbox = {"lat": 40.49, "lon": -3.71, "bbox": BBOX_FRANJA, "polys": FRANJA}
    cerca_de_verdad = {"lat": 40.4996, "lon": -3.7190, "nombre": "plaza pequeña"}

    d, p = mas_cercano(40.4995, -3.7195, [lejos_pero_en_el_bbox, cerca_de_verdad])

    assert p["nombre"] == "plaza pequeña"
    assert d < 100


def test_sin_puntos_no_hay_distancia():
    assert mas_cercano(40.49, -3.71, []) == (None, None)


@pytest.mark.parametrize("anillo", [[], [[40.0, -3.0]], [[40.0, -3.0], [40.1, -3.1]]])
def test_un_anillo_degenerado_no_revienta(anillo):
    assert distancia_a_poligono(40.49, -3.71, [anillo]) == float("inf")
