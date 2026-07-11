"""La BD la genera el cron de GitHub; tus votos viven en tu máquina.

Importar tiene que traer lo primero SIN pisar lo segundo. Si esto se rompe, cada
sincronización te borra las valoraciones y no te enteras.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.models import Score
from src.store import EsquemaObsoleto, Store


@pytest.fixture
def remota(tmp_path, listing_factory):
    """La BD tal y como la dejaría GitHub Actions: anuncios, precios y notas."""
    ruta = tmp_path / "remota.db"
    with Store(ruta) as s:
        caro = listing_factory(property_code="A", precio=900_000)
        s.sync("gh", [caro, listing_factory(property_code="B")])
        s.sync("gh", [replace(caro, precio=850_000), listing_factory(property_code="B")])
        s.guardar_scores("gh", {"A": Score(total=80.0, detalle={})}, "hash")
        s.apuntar_llamada("idealista", "search")
    return ruta


@pytest.fixture
def local(tmp_path, listing_factory):
    """Tu BD: la de ayer, más los votos que has ido dejando en la web."""
    with Store(tmp_path / "local.db") as s:
        s.sync("gh", [listing_factory(property_code="A", precio=900_000)])
        s.opinar("gh", "A", "interesa")
        yield s


def test_trae_los_anuncios_nuevos(local, remota):
    local.importar(remota)
    assert {a["property_code"] for a in local.buscar()} == {"A", "B"}


def test_trae_el_historico_de_precios(local, remota):
    assert len(local.historico("gh", "A")) == 1

    local.importar(remota)

    assert [p for _, p in local.historico("gh", "A")] == [900_000, 850_000]
    assert local.ficha("gh", "A")["precio"] == 850_000


def test_trae_las_notas_y_la_cuota(local, remota):
    local.importar(remota)
    assert local.ficha("gh", "A")["score"] == 80.0
    assert local.llamadas_del_mes("idealista") == 1


def test_NO_pisa_tus_valoraciones(local, remota):
    """Lo que de verdad importa: importar no puede costarte tus votos."""
    local.importar(remota)

    assert local.ficha("gh", "A")["opinion"] == "interesa"
    assert len(local.opiniones()) == 1


def test_importar_dos_veces_no_duplica_nada(local, remota):
    local.importar(remota)
    local.importar(remota)

    assert local.count() == 2
    assert len(local.historico("gh", "A")) == 2
    assert local.llamadas_del_mes("idealista") == 1


def test_importar_sobre_una_bd_vacia(tmp_path, remota):
    with Store(tmp_path / "nueva.db") as nueva:
        nueva.importar(remota)
        assert nueva.count() == 2


def test_una_bd_de_otra_version_se_rechaza(local, remota):
    with Store(remota) as vieja:
        vieja.conn.execute("PRAGMA user_version = 99")
        vieja.conn.commit()

    with pytest.raises(EsquemaObsoleto, match="v99"):
        local.importar(remota)


def test_una_ruta_que_no_existe_lo_dice_claro(local, tmp_path):
    with pytest.raises(FileNotFoundError):
        local.importar(tmp_path / "no-existe.db")
