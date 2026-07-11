from __future__ import annotations

import pytest

from src.models import Score
from src.scoring import calibracion
from src.store import Store


@pytest.fixture
def store(tmp_path, listing_factory):
    with Store(tmp_path / "test.db") as s:
        s.sync("mock", [listing_factory(property_code=c) for c in ("A", "B", "C")])
        yield s


def test_un_anuncio_empieza_sin_opinion(store):
    assert store.ficha("mock", "A")["opinion"] is None


def test_se_guarda_la_valoracion(store):
    store.opinar("mock", "A", "interesa")
    assert store.ficha("mock", "A")["opinion"] == "interesa"


def test_se_puede_cambiar_de_opinion(store):
    store.opinar("mock", "A", "interesa")
    store.opinar("mock", "A", "descartado")
    assert store.ficha("mock", "A")["opinion"] == "descartado"
    assert len(store.opiniones()) == 1, "es una opinión por anuncio, no un historial"


def test_se_puede_retirar_la_valoracion(store):
    store.opinar("mock", "A", "interesa")
    store.borrar_opinion("mock", "A")
    assert store.ficha("mock", "A")["opinion"] is None


def test_una_valoracion_inventada_se_rechaza(store):
    with pytest.raises(ValueError, match="quizas"):
        store.opinar("mock", "A", "quizas")


def test_los_descartados_se_ocultan_por_defecto(store):
    store.opinar("mock", "B", "descartado")

    visibles = [a["property_code"] for a in store.buscar()]
    todos = [a["property_code"] for a in store.buscar(ocultar_descartados=False)]

    assert "B" not in visibles
    assert "B" in todos


def test_congela_la_nota_del_momento_en_que_juzgaste(store, listing_factory):
    """La nota se recalcula cada día. Sin esta foto no sabríamos qué viste al decidir."""
    store.guardar_scores("mock", {"A": Score(total=81.0, precio=90.0, detalle={"x": 1})}, "hash1")
    store.opinar("mock", "A", "interesa")

    # Al día siguiente la mediana del barrio cambia y la nota se recalcula.
    store.guardar_scores("mock", {"A": Score(total=42.0, precio=10.0, detalle={"x": 9})}, "hash2")

    (opinion,) = store.opiniones()
    assert opinion["score"] == 81.0, "la opinión conserva la nota que tenía al valorarla"
    assert opinion["detalle"] == {"x": 1}
    assert store.ficha("mock", "A")["score"] == 42.0, "pero la nota vigente sí se actualiza"


# --- calibración -------------------------------------------------------------


def _opinion(valoracion, score, positivas=(), negativas=(), desviacion=None, barrio="Mirasierra"):
    return {
        "valoracion": valoracion,
        "score": score,
        "barrio": barrio,
        "detalle": {
            "texto": {"positivas": list(positivas), "negativas": list(negativas)},
            "precio": {"desviacion_pct": desviacion},
        },
    }


def test_sin_muestra_el_informe_lo_dice_en_vez_de_inventarse_conclusiones():
    a = calibracion.analizar([_opinion("interesa", 80), _opinion("descartado", 40)])
    assert a["suficiente"] is False
    assert "Hacen falta al menos" in calibracion.informe(a)


def test_compara_los_dos_grupos():
    opiniones = [_opinion("interesa", 80, positivas=["terraza"], desviacion=-10) for _ in range(5)]
    opiniones += [
        _opinion("descartado", 40, negativas=["a reformar"], desviacion=5) for _ in range(5)
    ]

    a = calibracion.analizar(opiniones)

    assert a["suficiente"] is True
    assert a["nota_interesa"] == 80
    assert a["nota_descartado"] == 40
    assert a["positivas_interesa"] == [("terraza", 5)]
    assert a["negativas_descartado"] == [("a reformar", 5)]
    assert "separa los dos grupos por 40 puntos" in calibracion.informe(a)


def test_avisa_si_la_nota_no_distingue_nada():
    """Si el scoring no separa tus gustos, hay que saberlo: es el caso que importa."""
    opiniones = [_opinion("interesa", 62) for _ in range(5)]
    opiniones += [_opinion("descartado", 60) for _ in range(5)]

    informe = calibracion.informe(calibracion.analizar(opiniones))

    assert "no está capturando tu criterio" in informe
