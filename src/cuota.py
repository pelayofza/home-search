"""Guardarraíl de cuota: impide que un bug de paginación te funda el mes.

La cuota de la API de Idealista no es de autoservicio: te la comunican al
aprobarte el acceso. Ponla en `config.yaml` (`api.cuota_mensual`) y este módulo
se encarga de que no te la saltes. Si te quedas sin cuota, te quedas ciego hasta
el día 1 del mes siguiente, así que el corte es duro y hay una reserva que no se
toca ni queriendo.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.store import Store

log = logging.getLogger(__name__)


class CuotaAgotada(RuntimeError):
    """No quedan peticiones este mes. No es un error: es el guardarraíl haciendo su trabajo."""


class Cuota:
    def __init__(self, store: Store, source: str, config: dict[str, Any]) -> None:
        api = config.get("api") or {}
        self.store = store
        self.source = source
        self.limite: int | None = api.get("cuota_mensual")
        self.reserva: int = int(api.get("reserva", 0))

    @property
    def gastadas(self) -> int:
        return self.store.llamadas_del_mes(self.source)

    @property
    def disponibles(self) -> int:
        """Lo que queda descontando la reserva. Infinito si no hay límite configurado."""
        if self.limite is None:
            return 10**9
        return max(0, self.limite - self.reserva - self.gastadas)

    def exigir(self, n: int = 1) -> None:
        """Lanza CuotaAgotada si no caben n llamadas más."""
        if self.disponibles < n:
            raise CuotaAgotada(
                f"quedan {self.disponibles} peticiones de {self.limite} este mes "
                f"(reserva intocable: {self.reserva}). Pedías {n}. "
                "Ajusta la cadencia en el cron o sube api.cuota_mensual si te han ampliado el plan."
            )

    def consumir(self, endpoint: str) -> None:
        """Apunta una llamada. Hazlo justo ANTES de lanzarla."""
        self.exigir(1)
        self.store.apuntar_llamada(self.source, endpoint)

    def informe(self) -> str:
        if self.limite is None:
            return (
                f"{self.gastadas} peticiones este mes. No hay cuota configurada "
                "(api.cuota_mensual): sin límite y sin red."
            )
        lineas = [
            f"Cuota de {self.source}: {self.gastadas} de {self.limite} usadas este mes.",
            f"Disponibles: {self.disponibles} (más {self.reserva} de reserva intocable).",
            "",
        ]
        for mes, n in self.store.consumo_por_mes(self.source)[:6]:
            aviso = "  ⚠ te pasaste" if self.limite and n > self.limite else ""
            lineas.append(f"  {mes}: {n}{aviso}")
        return "\n".join(lineas)
