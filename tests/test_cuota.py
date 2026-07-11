from __future__ import annotations

import pytest

from src.cuota import Cuota, CuotaAgotada
from src.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


@pytest.fixture
def cuota(store):
    return Cuota(store, "idealista", {"api": {"cuota_mensual": 100, "reserva": 5}})


def test_empieza_entera(cuota):
    assert cuota.gastadas == 0
    assert cuota.disponibles == 95, "100 menos la reserva de 5"


def test_cada_llamada_descuenta(cuota):
    cuota.consumir("search?numPage=1")
    cuota.consumir("search?numPage=2")
    assert cuota.gastadas == 2
    assert cuota.disponibles == 93


def test_corta_en_seco_al_agotarse(store):
    cuota = Cuota(store, "idealista", {"api": {"cuota_mensual": 3, "reserva": 1}})
    cuota.consumir("a")
    cuota.consumir("b")

    assert cuota.disponibles == 0
    with pytest.raises(CuotaAgotada, match="quedan 0 peticiones"):
        cuota.consumir("c")


def test_la_reserva_no_se_toca_ni_queriendo(store):
    cuota = Cuota(store, "idealista", {"api": {"cuota_mensual": 10, "reserva": 4}})
    for i in range(6):
        cuota.consumir(f"p{i}")

    with pytest.raises(CuotaAgotada):
        cuota.consumir("una-mas")
    assert cuota.gastadas == 6, "quedan 4 en el contador real, pero son intocables"


def test_sin_limite_configurado_no_hay_red(store):
    cuota = Cuota(store, "idealista", {})
    cuota.consumir("a")
    assert cuota.disponibles > 1000
    assert "sin límite y sin red" in cuota.informe()


def test_el_mock_no_gasta_la_cuota_de_idealista(store):
    idealista = Cuota(store, "idealista", {"api": {"cuota_mensual": 100}})
    idealista.consumir("search")

    mock = Cuota(store, "mock", {"api": {"cuota_mensual": 100}})
    assert mock.gastadas == 0


def test_solo_cuenta_el_mes_en_curso(store, cuota):
    cuota.consumir("de-este-mes")
    store.conn.execute("INSERT INTO api_calls (source, ts, endpoint) VALUES (?, ?, ?)",
                       ("idealista", "2020-01-15T10:00:00", "de-hace-anios"))
    store.conn.commit()

    assert cuota.gastadas == 1
    assert len(store.consumo_por_mes("idealista")) == 2
