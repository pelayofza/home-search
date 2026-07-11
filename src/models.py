"""Modelo de datos común a todos los conectores."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

#: Tipos de vivienda en los que "la planta" significa algo. Un chalet siempre
#: está a pie de calle, así que el filtro de planta mínima no debe aplicársele.
TIPOS_CON_PLANTA = frozenset({"piso", "atico", "duplex", "estudio"})

#: Para comparar precios hay que agrupar. El €/m² de un chalet con parcela y el
#: de un piso no son la misma magnitud, pero nunca habrá muestra suficiente de
#: áticos en Arroyo del Fresno: la familia es el punto medio útil.
FAMILIAS = {
    "piso": "piso",
    "atico": "piso",
    "duplex": "piso",
    "estudio": "piso",
    "chalet": "casa",
    "adosado": "casa",
    "pareado": "casa",
}


class Cambio(StrEnum):
    """Qué le ha pasado a un anuncio desde la última vez que lo vimos."""

    NUEVO = "nuevo"
    BAJADA = "bajada"
    SUBIDA = "subida"
    IGUAL = "igual"
    DESAPARECIDO = "desaparecido"
    REAPARECIDO = "reaparecido"


@dataclass(frozen=True, slots=True)
class Listing:
    """Un anuncio de venta de vivienda, normalizado."""

    property_code: str
    precio: int
    m2: int
    habitaciones: int
    banos: int
    planta: int  # 0 = bajo; negativo = sótano/semisótano
    ascensor: bool
    barrio: str
    url: str
    tipo: str = "piso"  # piso | atico | duplex | estudio | chalet | adosado | pareado
    exterior: bool | None = None  # None = el anuncio no lo dice
    lat: float | None = None
    lon: float | None = None
    foto_url: str | None = None
    descripcion: str = ""
    fecha_vista: date = field(default_factory=lambda: datetime.now().date())

    @property
    def precio_m2(self) -> int:
        return round(self.precio / self.m2) if self.m2 else 0

    @property
    def tiene_planta(self) -> bool:
        return self.tipo in TIPOS_CON_PLANTA

    @property
    def familia(self) -> str:
        return FAMILIAS.get(self.tipo, "otro")


@dataclass(frozen=True, slots=True)
class Score:
    """Nota de 0 a 100 y su desglose.

    Un subscore a None significa "no se ha podido calcular" (p. ej. sin
    coordenadas), no "cero puntos": los pesos se renormalizan sobre las
    dimensiones disponibles para no hundir injustamente el anuncio.
    """

    total: float
    texto: float | None = None
    precio: float | None = None
    localizacion: float | None = None
    detalle: dict[str, Any] = field(default_factory=dict)

    @property
    def dimensiones(self) -> int:
        return sum(
            1 for s in (self.texto, self.precio, self.localizacion) if s is not None
        )


@dataclass(frozen=True, slots=True)
class Novedad:
    """La unidad que viaja por el pipeline: un anuncio y qué le ha pasado."""

    listing: Listing
    cambio: Cambio
    precio_anterior: int | None = None
    score: Score | None = None
    evento_id: int | None = None
    #: Días desde que NOSOTROS lo vimos por primera vez. No es la fecha de
    #: publicación (la API no la da fiable), y confundirlas sería mentir: un
    #: anuncio de hace un año que descubrimos ayer marcaría "1 día".
    dias_vistos: int | None = None
    bajadas: int = 0
    negociacion: dict[str, Any] | None = None

    @property
    def delta(self) -> int | None:
        if self.precio_anterior is None:
            return None
        return self.listing.precio - self.precio_anterior

    @property
    def delta_pct(self) -> float | None:
        if not self.precio_anterior:
            return None
        return 100 * (self.listing.precio - self.precio_anterior) / self.precio_anterior
