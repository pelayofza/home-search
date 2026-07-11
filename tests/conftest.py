from __future__ import annotations

import pytest

from src.models import Listing


@pytest.fixture
def listing_factory():
    """Crea un Listing que pasa los filtros por defecto; sobreescribe lo que necesites."""

    defaults = dict(
        property_code="X-1",
        precio=795_000,
        m2=128,
        habitaciones=3,
        banos=2,
        planta=4,
        ascensor=True,
        barrio="Montecarmelo",
        url="https://example.com/X-1",
        tipo="piso",
        exterior=True,
        foto_url=None,
        descripcion="Piso bonito",
    )

    def make(**overrides) -> Listing:
        return Listing(**{**defaults, **overrides})

    return make


@pytest.fixture
def config() -> dict:
    """Espejo de config.yaml, en pequeño."""
    return {
        "filtros": {
            "precio_min": 0,
            "precio_max": 1_100_000,
            "m2_min": 110,
            "habitaciones_min": 3,
            "banos_min": 1,
            "tipos": ["piso", "atico", "duplex", "chalet", "adosado"],
            "exterior": True,
            "planta_min": 1,
            "ascensor": None,
            "barrios_incluidos": ["Montecarmelo", "Mirasierra", "Pinar de Chamartín"],
            "barrios_excluidos": [],
            "palabras_excluidas": ["okupa", "subasta"],
        }
    }
