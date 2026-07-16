"""Origen falso, en dos fases, para probar el pipeline entero sin API.

La fase 2 es "el día siguiente": un anuncio baja de precio, otro sube, uno
desaparece y aparece uno nuevo. Sin esto no hay forma de probar el histórico
sin esperar días a que el mercado se mueva.

Las fotos son de un servicio de placeholders: no son pisos, solo sirven para
comprobar que la web y el email pintan bien las imágenes. Las de verdad salen del
campo `thumbnail` que devuelve la API de Idealista.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from src.models import Listing
from src.sources.base import Source

log = logging.getLogger(__name__)

_DIA_1 = [
    Listing(
        property_code="MOCK-001",
        precio=795_000,
        m2=128,
        habitaciones=3,
        banos=2,
        planta=4,
        ascensor=True,
        barrio="Montecarmelo",
        url="https://example.com/anuncio/MOCK-001",
        tipo="piso",
        exterior=True,
        lat=40.4970,
        lon=-3.7080,
        foto_url="https://picsum.photos/seed/MOCK-001/480/360",
        descripcion="Piso exterior reformado con terraza, garaje y trastero. Urbanización con piscina.",
    ),
    Listing(
        property_code="MOCK-002",
        precio=1_050_000,
        m2=240,
        habitaciones=4,
        banos=3,
        planta=0,
        ascensor=False,
        barrio="Mirasierra",
        url="https://example.com/anuncio/MOCK-002",
        tipo="chalet",
        exterior=True,
        lat=40.4855,
        lon=-3.7205,
        foto_url="https://picsum.photos/seed/MOCK-002/480/360",
        descripcion="Chalet independiente con jardín y piscina privada.",
    ),
    # Fuera de presupuesto hoy. Se guarda igualmente: en la fase 2 baja y entra.
    Listing(
        property_code="MOCK-003",
        precio=1_290_000,
        m2=310,
        habitaciones=5,
        banos=4,
        planta=0,
        ascensor=False,
        barrio="Arroyo del Fresno",
        url="https://example.com/anuncio/MOCK-003",
        tipo="chalet",
        exterior=True,
        lat=40.4930,
        lon=-3.7350,
        foto_url="https://picsum.photos/seed/MOCK-003/480/360",
        descripcion="Chalet de obra nueva a estrenar, parcela de 600 m² con jardín.",
    ),
    # Interior: no pasa los filtros, pero sí entra en la muestra de comparables.
    Listing(
        property_code="MOCK-004",
        precio=610_000,
        m2=115,
        habitaciones=3,
        banos=2,
        planta=2,
        ascensor=True,
        barrio="Las Tablas",
        url="https://example.com/anuncio/MOCK-004",
        tipo="piso",
        exterior=False,
        lat=40.5085,
        lon=-3.6790,
        foto_url="https://picsum.photos/seed/MOCK-004/480/360",
        descripcion="Piso interior muy tranquilo, patio interior, sin ruido de calle.",
    ),
    Listing(
        property_code="MOCK-005",
        precio=880_000,
        m2=180,
        habitaciones=4,
        banos=3,
        planta=0,
        ascensor=False,
        barrio="Sanchinarro",
        url="https://example.com/anuncio/MOCK-005",
        tipo="adosado",
        exterior=True,
        lat=40.4915,
        lon=-3.6595,
        foto_url="https://picsum.photos/seed/MOCK-005/480/360",
        descripcion="Adosado en urbanización cerrada, jardín, garaje para dos coches y trastero.",
    ),
]

_por_codigo = {l.property_code: l for l in _DIA_1}

_DIA_2 = [
    _por_codigo["MOCK-001"],  # sin cambios
    replace(_por_codigo["MOCK-002"], precio=985_000),  # bajada del 6%
    replace(_por_codigo["MOCK-003"], precio=1_090_000),  # bajada: ahora SÍ entra en presupuesto
    replace(_por_codigo["MOCK-004"], precio=625_000),  # subida
    # MOCK-005 ya no aparece: desaparecido (hacen falta 2 ausencias para retirarlo)
    Listing(
        property_code="MOCK-006",
        precio=740_000,
        m2=112,
        habitaciones=3,
        banos=2,
        planta=1,
        ascensor=True,
        barrio="Montecarmelo",
        url="https://example.com/anuncio/MOCK-006",
        tipo="piso",
        exterior=True,
        lat=40.4985,
        lon=-3.7055,
        foto_url="https://picsum.photos/seed/MOCK-006/480/360",
        descripcion="Piso luminoso a estrenar con terraza y plaza de garaje.",
    ),
]

FASES = {1: _DIA_1, 2: _DIA_2}


class MockSource(Source):
    name = "mock"

    def fetch(self) -> list[Listing]:
        fase = int((self.config.get("mock") or {}).get("fase", 1))
        lote = FASES.get(fase, _DIA_1)
        log.info("mock: fase %d, %d anuncios", fase, len(lote))
        return list(lote)
