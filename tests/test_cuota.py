from __future__ import annotations

from datetime import datetime

import pytest

from src.cuota import Cuota, CuotaAgotada
from src.store import Store, inicio_de_ciclo


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


# --- el ciclo de facturación no es el mes natural ----------------------------


@pytest.mark.parametrize(
    "hoy,dia_corte,esperado",
    [
        # Ya hemos pasado el corte de este mes: el ciclo empezó este mes.
        ("2026-07-25", 20, "2026-07-20"),
        # Justo el día del corte: el ciclo empieza hoy.
        ("2026-07-20", 20, "2026-07-20"),
        # Aún no hemos llegado: el ciclo viene del mes pasado.
        ("2026-07-05", 20, "2026-06-20"),
        # Cambio de año hacia atrás.
        ("2026-01-05", 20, "2025-12-20"),
        # Corte el día 1 = mes natural, que es el defecto.
        ("2026-07-16", 1, "2026-07-01"),
        # Un corte el 31 no existe en febrero: se topa en 28 y adelanta el ciclo,
        # que es el lado seguro por el que equivocarse.
        ("2026-02-10", 31, "2026-01-28"),
    ],
)
def test_calcula_el_inicio_del_ciclo(hoy, dia_corte, esperado):
    inicio = inicio_de_ciclo(dia_corte, datetime.fromisoformat(f"{hoy}T09:00:00"))
    assert inicio.date().isoformat() == esperado


def test_el_contador_no_se_reinicia_el_dia_1_si_tu_ciclo_no_lo_hace(store):
    """Este es el bug que costaría dinero.

    Con el corte el día 20 y contando por mes natural, lo gastado del 20 al 31 se
    olvidaba al llegar el 1: podías gastar dos cuotas enteras dentro de un mismo
    ciclo de facturación de RapidAPI y llevarte el recargo.
    """
    cuota = Cuota(store, "idealista", {"api": {"cuota_mensual": 100, "dia_corte": 20}})
    for ts in ("2026-06-25T10:00:00", "2026-06-30T10:00:00", "2026-07-02T10:00:00"):
        store.conn.execute(
            "INSERT INTO api_calls (source, ts, endpoint) VALUES (?, ?, ?)",
            ("idealista", ts, "search"),
        )
    store.conn.commit()

    # Estamos a 5 de julio: el ciclo empezó el 20 de junio, así que cuentan las tres.
    ahora = datetime.fromisoformat("2026-07-05T09:00:00")
    assert store.llamadas_del_ciclo("idealista", 20, ahora) == 3
    assert store.llamadas_del_mes("idealista", "2026-07") == 1, "el mes natural solo ve una"


def test_lo_del_ciclo_anterior_ya_no_cuenta(store):
    cuota = Cuota(store, "idealista", {"api": {"cuota_mensual": 100, "dia_corte": 20}})
    store.conn.execute(
        "INSERT INTO api_calls (source, ts, endpoint) VALUES (?, ?, ?)",
        ("idealista", "2026-06-19T10:00:00", "del-ciclo-pasado"),
    )
    store.conn.commit()

    ahora = datetime.fromisoformat("2026-07-05T09:00:00")
    assert store.llamadas_del_ciclo("idealista", 20, ahora) == 0


def test_el_informe_dice_cuando_empezo_el_ciclo(store):
    cuota = Cuota(store, "idealista", {"api": {"cuota_mensual": 15_500, "reserva": 500}})
    informe = cuota.informe()
    assert "de 15500 usadas en este ciclo" in informe
    assert "El ciclo empezó el" in informe
