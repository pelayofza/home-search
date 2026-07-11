from __future__ import annotations

from dataclasses import replace

import pytest

from src.models import Cambio
from src.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


def cambios(novedades) -> dict[str, str]:
    return {n.listing.property_code: n.cambio.value for n in novedades}


def test_bd_arranca_vacia(store):
    assert store.count() == 0


def test_todo_es_nuevo_la_primera_vez(store, listing_factory):
    listings = [listing_factory(property_code=c) for c in ("A", "B")]
    assert cambios(store.diff("mock", listings)) == {"A": "nuevo", "B": "nuevo"}


def test_diff_no_escribe(store, listing_factory):
    store.diff("mock", [listing_factory(property_code="A")])
    assert store.count() == 0


def test_dedup_dentro_del_mismo_lote(store, listing_factory):
    duplicados = [listing_factory(property_code="A"), listing_factory(property_code="A")]
    assert len(store.diff("mock", duplicados)) == 1


def test_lo_guardado_ya_no_es_nuevo(store, listing_factory):
    listings = [listing_factory(property_code=c) for c in ("A", "B")]
    store.sync("mock", listings)

    nuevos = store.filter_new("mock", listings + [listing_factory(property_code="C")])

    assert [l.property_code for l in nuevos] == ["C"]
    assert store.count() == 3 - 1  # A y B; C no se ha guardado


def test_sync_repetido_es_idempotente(store, listing_factory):
    """Dos ejecuciones el mismo día no deben duplicar histórico ni eventos."""
    listings = [listing_factory(property_code="A")]
    store.sync("mock", listings)
    store.sync("mock", listings)

    assert store.count() == 1
    assert len(store.historico("mock", "A")) == 1
    assert len(store.eventos_pendientes()) == 1


def test_el_mismo_code_en_otra_fuente_es_nuevo(store, listing_factory):
    listing = listing_factory(property_code="A")
    store.sync("mock", [listing])

    assert store.is_known("mock", "A") is True
    assert store.is_known("idealista", "A") is False
    assert cambios(store.diff("idealista", [listing])) == {"A": "nuevo"}


def test_persiste_entre_conexiones(tmp_path, listing_factory):
    db = tmp_path / "test.db"
    with Store(db) as s:
        s.sync("mock", [listing_factory(property_code="A")])

    with Store(db) as s:
        assert s.is_known("mock", "A") is True
        assert s.count() == 1


def test_crea_el_directorio_de_la_bd(tmp_path, listing_factory):
    with Store(tmp_path / "sub" / "dir" / "test.db") as s:
        s.sync("mock", [listing_factory(property_code="A")])
        assert s.count() == 1


# --- histórico de precios ----------------------------------------------------


def test_una_bajada_se_detecta_y_deja_escalon(store, listing_factory):
    caro = listing_factory(property_code="A", precio=900_000)
    barato = replace(caro, precio=850_000)

    store.sync("mock", [caro])
    novedades = store.sync("mock", [barato])

    (n,) = novedades
    assert n.cambio is Cambio.BAJADA
    assert n.precio_anterior == 900_000
    assert n.delta == -50_000
    assert round(n.delta_pct, 1) == -5.6

    historico = [precio for _, precio in store.historico("mock", "A")]
    assert historico == [900_000, 850_000]


def test_una_subida_tambien_se_registra(store, listing_factory):
    barato = listing_factory(property_code="A", precio=800_000)
    store.sync("mock", [barato])
    (n,) = store.sync("mock", [replace(barato, precio=820_000)])

    assert n.cambio is Cambio.SUBIDA
    assert [p for _, p in store.historico("mock", "A")] == [800_000, 820_000]


def test_sin_cambio_de_precio_no_crece_el_historico(store, listing_factory):
    listing = listing_factory(property_code="A")
    store.sync("mock", [listing])
    (n,) = store.sync("mock", [listing])

    assert n.cambio is Cambio.IGUAL
    assert len(store.historico("mock", "A")) == 1


def test_precio_inicial_se_conserva(store, listing_factory):
    caro = listing_factory(property_code="A", precio=900_000)
    store.sync("mock", [caro])
    store.sync("mock", [replace(caro, precio=800_000)])

    fila = store.ficha("mock", "A")
    assert fila["precio_inicial"] == 900_000
    assert fila["precio"] == 800_000


# --- desaparecidos -----------------------------------------------------------


def test_hacen_falta_dos_ausencias_para_retirar(store, listing_factory):
    lote = [listing_factory(property_code=c) for c in ("A", "B", "C", "D")]
    store.sync("mock", lote)
    sin_a = lote[1:]

    novedades = store.sync("mock", sin_a)
    assert cambios(novedades)["A"] == "desaparecido"
    assert store.count(solo_activos=True) == 4, "una sola ausencia no retira nada"

    store.sync("mock", sin_a)
    assert store.count(solo_activos=True) == 3


def test_un_fetch_sospechosamente_corto_no_retira_nada(store, listing_factory):
    """Un fetch a medias marcaría media BD como vendida y llenaría el email de ruido."""
    lote = [listing_factory(property_code=c) for c in "ABCDEFGH"]
    store.sync("mock", lote)

    novedades = store.diff("mock", lote[:2])  # solo llegan 2 de 8

    assert not [n for n in novedades if n.cambio is Cambio.DESAPARECIDO]


def test_un_anuncio_retirado_puede_reaparecer(store, listing_factory):
    lote = [listing_factory(property_code=c) for c in ("A", "B", "C", "D")]
    store.sync("mock", lote)
    store.sync("mock", lote[1:])
    store.sync("mock", lote[1:])  # A retirado

    (n,) = [x for x in store.sync("mock", lote) if x.listing.property_code == "A"]
    assert n.cambio is Cambio.REAPARECIDO
    assert store.count(solo_activos=True) == 4


# --- eventos -----------------------------------------------------------------


def test_los_eventos_se_marcan_como_notificados(store, listing_factory):
    store.sync("mock", [listing_factory(property_code="A")])

    pendientes = store.eventos_pendientes()
    assert [n.cambio for n in pendientes] == [Cambio.NUEVO]

    store.marcar_notificados([n.evento_id for n in pendientes])
    assert store.eventos_pendientes() == []


def test_se_pueden_pedir_solo_ciertos_tipos(store, listing_factory):
    caro = listing_factory(property_code="A", precio=900_000)
    store.sync("mock", [caro])
    store.marcar_notificados([n.evento_id for n in store.eventos_pendientes()])
    store.sync("mock", [replace(caro, precio=850_000)])

    pendientes = store.eventos_pendientes(tipos=["bajada"])
    assert [n.cambio for n in pendientes] == [Cambio.BAJADA]


# --- comparables -------------------------------------------------------------


def test_sin_muestra_suficiente_no_hay_mediana(store, listing_factory):
    store.sync("mock", [listing_factory(property_code="A")])
    mediana, n = store.mediana_precio_m2("Montecarmelo", "piso", min_muestra=8)
    assert mediana is None
    assert n == 1


def test_mediana_por_barrio_y_familia(store, listing_factory):
    # 8 pisos a 5.000 €/m² y un chalet carísimo que no debe contaminar.
    pisos = [
        listing_factory(property_code=f"P{i}", precio=500_000, m2=100, tipo="piso")
        for i in range(8)
    ]
    chalet = listing_factory(property_code="C1", precio=2_000_000, m2=200, tipo="chalet")
    store.sync("mock", [*pisos, chalet])

    mediana, n = store.mediana_precio_m2("Montecarmelo", "piso", min_muestra=8)
    assert (mediana, n) == (5000, 8)


def test_el_anuncio_no_entra_en_su_propia_mediana(store, listing_factory):
    pisos = [
        listing_factory(property_code=f"P{i}", precio=500_000, m2=100) for i in range(9)
    ]
    store.sync("mock", pisos)

    _, n = store.mediana_precio_m2("Montecarmelo", "piso", excluir="P0", min_muestra=8)
    assert n == 8


def test_el_mock_no_contamina_la_mediana_de_los_anuncios_reales(store, listing_factory):
    """Los 5 pisos inventados del mock no pueden entrar en la mediana con la que
    se juzga un piso de verdad."""
    reales = [
        listing_factory(property_code=f"R{i}", precio=500_000, m2=100) for i in range(8)
    ]
    inventados = [
        listing_factory(property_code=f"M{i}", precio=900_000, m2=100) for i in range(8)
    ]
    store.sync("idealista", reales)
    store.sync("mock", inventados)

    mediana, n = store.mediana_precio_m2("Montecarmelo", "piso", source="idealista")

    assert (mediana, n) == (5000, 8)


def test_purgar_una_fuente_no_toca_las_demas(store, listing_factory):
    store.sync("mock", [listing_factory(property_code="M1")])
    store.sync("idealista", [listing_factory(property_code="I1")])
    store.opinar("mock", "M1", "interesa")

    store.purgar("mock")

    assert store.is_known("mock", "M1") is False
    assert store.is_known("idealista", "I1") is True
    assert store.opiniones() == []


def test_los_outliers_no_arrastran_la_mediana(store, listing_factory):
    normales = [
        listing_factory(property_code=f"P{i}", precio=500_000, m2=100) for i in range(9)
    ]
    # Un parseo roto: la parcela contada como superficie construida.
    basura = listing_factory(property_code="X", precio=600_000, m2=599)
    store.sync("mock", [*normales, basura])

    mediana, n = store.mediana_precio_m2("Montecarmelo", "piso", min_muestra=8)
    assert mediana == 5000
    assert n == 9, "el outlier queda fuera de la muestra"
