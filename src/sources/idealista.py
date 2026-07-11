"""Conector de la API de Idealista.

Autenticación OAuth2 (client_credentials con Basic auth) y búsqueda paginada.
Cada llamada pasa por el guardarraíl de cuota: si no quedan peticiones, revienta
antes de salir a la red, no después.

IMPORTANTE sobre la cuota. La API no es de autoservicio: te asignan un límite al
aprobarte el acceso, y no está publicado en ninguna parte. Pon el tuyo en
`config.yaml` (`api.cuota_mensual`) en cuanto te lo digan. Lo único confirmado es
que devuelve como mucho 50 resultados por página.

La estrategia de barrido está pensada para gastar poco:

  - `sinceDate=W` a diario: solo lo publicado esta semana. Son 1 o 2 páginas.
  - barrido completo, semanal: es la ÚNICA forma de ver bajadas de precio, porque
    un piso publicado hace tres meses que baja hoy no aparece en el filtro de
    novedades.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta
from typing import Any

import requests

from src.cuota import Cuota
from src.models import Listing
from src.sources.base import Source

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.idealista.com/oauth/token"
SEARCH_URL = "https://api.idealista.com/3.5/es/search"

MAX_POR_PAGINA = 50  # tope de la API; pedir más no sirve de nada

#: propertyType + detailedType de Idealista -> nuestro vocabulario.
_TIPOS = {
    "flat": "piso",
    "penthouse": "atico",
    "duplex": "duplex",
    "studio": "estudio",
    "chalet": "chalet",
    "countryHouse": "chalet",
    "semidetachedHouse": "adosado",
    "terracedHouse": "adosado",
    "independantHouse": "chalet",
}


class IdealistaError(RuntimeError):
    pass


class IdealistaSource(Source):
    name = "idealista"

    def __init__(self, config: dict[str, Any], cuota: Cuota) -> None:
        super().__init__(config)
        self.cuota = cuota
        self.api = config.get("api") or {}
        self._token: str | None = None
        self._token_expira: datetime | None = None

    # --- autenticación ------------------------------------------------------

    def _credenciales(self) -> tuple[str, str]:
        key = os.environ.get("IDEALISTA_API_KEY")
        secret = os.environ.get("IDEALISTA_API_SECRET")
        if not key or not secret:
            raise IdealistaError(
                "faltan IDEALISTA_API_KEY / IDEALISTA_API_SECRET en el .env"
            )
        return key, secret

    def token(self) -> str:
        """Token OAuth2, cacheado en memoria mientras dure.

        Pedir un token en cada página multiplicaría por dos el gasto si el
        proveedor cuenta también estas llamadas, y no sabemos si lo hace.
        """
        if self._token and self._token_expira and datetime.now() < self._token_expira:
            return self._token

        key, secret = self._credenciales()
        basic = base64.b64encode(f"{key}:{secret}".encode()).decode()

        self.cuota.consumir("oauth/token")
        r = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": "read"},
            timeout=30,
        )
        r.raise_for_status()
        datos = r.json()

        self._token = datos["access_token"]
        # Un minuto de colchón: un token que caduca a mitad de la paginación
        # obligaría a repetir la página, y eso cuesta cuota.
        self._token_expira = datetime.now() + timedelta(seconds=int(datos["expires_in"]) - 60)
        return self._token

    # --- búsqueda -----------------------------------------------------------

    def _parametros(self, pagina: int, solo_recientes: bool) -> dict[str, Any]:
        b = self.api.get("busqueda") or {}
        params: dict[str, Any] = {
            "operation": "sale",
            "propertyType": "homes",
            "center": b.get("center", "40.4950,-3.7000"),  # zona norte
            "distance": b.get("distance", 5000),
            "maxItems": MAX_POR_PAGINA,
            "numPage": pagina,
            "order": "publicationDate",
            "sort": "desc",
        }
        # A propósito MÁS ANCHOS que los filtros de config.yaml: hay que guardar
        # el piso de 1,3 M para detectar el día que baje a 1,05 M y entre en
        # presupuesto, y para que la mediana del barrio no salga sesgada.
        for clave, destino in (
            ("precio_max", "maxPrice"),
            ("precio_min", "minPrice"),
            ("m2_min", "minSize"),
            ("habitaciones_min", "minRooms"),
        ):
            if (valor := b.get(clave)) is not None:
                params[destino] = valor

        if solo_recientes:
            params["sinceDate"] = "W"  # publicado en la última semana
        return params

    def _pagina(self, pagina: int, solo_recientes: bool) -> dict[str, Any]:
        self.cuota.consumir(f"search?numPage={pagina}")
        r = requests.post(
            SEARCH_URL,
            headers={"Authorization": f"Bearer {self.token()}"},
            data=self._parametros(pagina, solo_recientes),
            timeout=60,
        )
        if r.status_code == 429:
            raise IdealistaError(
                "la API responde 429: has agotado la cuota o vas demasiado rápido. "
                "Revisa api.cuota_mensual en config.yaml, puede que sea más alta que la real."
            )
        r.raise_for_status()
        return r.json()

    def fetch(self, solo_recientes: bool = False) -> list[Listing]:
        primera = self._pagina(1, solo_recientes)
        total_paginas = int(primera.get("totalPages", 1))

        log.info(
            "idealista: %s resultados en %s páginas (%s)",
            primera.get("total"),
            total_paginas,
            "solo esta semana" if solo_recientes else "barrido completo",
        )

        elementos = list(primera.get("elementList") or [])

        for pagina in range(2, total_paginas + 1):
            if self.cuota.disponibles < 1:
                # Mejor un barrido incompleto que quedarse sin cuota a mitad de mes.
                log.warning(
                    "cuota agotada en la página %d de %d: devuelvo lo que llevo. "
                    "Los anuncios que falten NO se marcarán como desaparecidos.",
                    pagina,
                    total_paginas,
                )
                break
            elementos.extend(self._pagina(pagina, solo_recientes).get("elementList") or [])

        return [l for e in elementos if (l := self._a_listing(e))]

    # --- mapeo --------------------------------------------------------------

    def _a_listing(self, e: dict[str, Any]) -> Listing | None:
        try:
            return Listing(
                property_code=str(e["propertyCode"]),
                precio=int(e["price"]),
                m2=int(e["size"]),
                habitaciones=int(e.get("rooms") or 0),
                banos=int(e.get("bathrooms") or 0),
                planta=_planta(e.get("floor")),
                ascensor=bool(e.get("hasLift", False)),
                barrio=e.get("neighborhood") or e.get("district") or e.get("municipality") or "",
                url=e.get("url", ""),
                tipo=_tipo(e),
                exterior=e.get("exterior"),  # la API lo da como bool; None si no lo dice
                lat=e.get("latitude"),
                lon=e.get("longitude"),
                foto_url=e.get("thumbnail"),
                descripcion=e.get("description") or "",
            )
        except (KeyError, TypeError, ValueError) as err:
            # Un anuncio mal formado no debe tumbar el barrido entero: nos ha
            # costado cuota traerlo y el resto de la página es válido.
            log.warning("anuncio descartado por datos incompletos (%s): %s", err, e.get("propertyCode"))
            return None


def _tipo(e: dict[str, Any]) -> str:
    detallado = (e.get("detailedType") or {}).get("typology")
    subtipo = (e.get("detailedType") or {}).get("subTypology")
    return _TIPOS.get(subtipo) or _TIPOS.get(detallado) or _TIPOS.get(e.get("propertyType")) or "piso"


def _planta(floor: Any) -> int:
    """'bj' -> 0, 'ss'/'st' -> -1, '3' -> 3. Lo desconocido, planta baja."""
    if floor is None:
        return 0
    texto = str(floor).strip().lower()
    if texto in ("bj", "bajo"):
        return 0
    if texto in ("ss", "st", "sotano", "semi-sotano"):
        return -1
    if texto == "en":  # entreplanta
        return 0
    try:
        return int(texto)
    except ValueError:
        return 0
