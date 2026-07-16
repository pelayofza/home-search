"""Guardarraíl de cuota: impide que un bug de paginación te funda el mes.

Pon el límite de tu plan en `config.yaml` (`api.cuota_mensual`) y este módulo se
encarga de que no te lo saltes. El corte es duro y además hay una reserva que no
se toca ni queriendo, porque quedarse a cero a mitad de ciclo te deja ciego hasta
que renueve.

QUÉ NO PUEDE HACER ESTO, y conviene tenerlo claro si lo que te preocupa es que te
cobren de más:

  - Solo cuenta las llamadas que ve, y solo ve las de SU base de datos. El cron de
    GitHub y tu portátil tienen bases distintas, así que lo que gastes en local no
    lo sabe GitHub ni al revés. Con un barrido diario de ~30 peticiones sobre un
    plan de 15.500 el margen es enorme, pero la garantía no es matemática.
  - No sabe lo que dice el contador de RapidAPI, que es el que factura.

El único límite de verdad infranqueable está en el panel de RapidAPI, no aquí:
esto es un cinturón, no un contrato.
"""

from __future__ import annotations

import logging
from datetime import datetime
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
        self.dia_corte: int = int(api.get("dia_corte", 1))

    @property
    def gastadas(self) -> int:
        """Por ciclo de facturación, no por mes natural.

        RapidAPI reinicia tu contador el día que te suscribiste. Si contáramos por
        mes natural y tu corte fuera el 20, podrías gastar la cuota entera del 20
        al 31 y otra vez del 1 al 19: el doble dentro del mismo ciclo, que es
        exactamente el recargo que queremos evitar.
        """
        return self.store.llamadas_del_ciclo(self.source, self.dia_corte)

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
                f"quedan {self.disponibles} peticiones de {self.limite} en este ciclo "
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
        from src.store import inicio_de_ciclo  # local: evita un import circular

        desde = inicio_de_ciclo(self.dia_corte, datetime.now()).date()
        lineas = [
            f"Cuota de {self.source}: {self.gastadas} de {self.limite} usadas en este ciclo.",
            f"El ciclo empezó el {desde} (api.dia_corte = {self.dia_corte}).",
            f"Disponibles: {self.disponibles} (más {self.reserva} de reserva intocable).",
            "",
            "Por mes natural, para comparar con el panel de RapidAPI:",
        ]
        for mes, n in self.store.consumo_por_mes(self.source)[:6]:
            aviso = "  ⚠ te pasaste" if self.limite and n > self.limite else ""
            lineas.append(f"  {mes}: {n}{aviso}")
        return "\n".join(lineas)
