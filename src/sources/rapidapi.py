"""Conector de Idealista a través de RapidAPI (proveedor: happyendpoint / idealista17).

OJO: esto NO es la API oficial de Idealista. Es un revendedor que raspa el portal
y lo sirve en JSON. Tres consecuencias que conviene tener presentes:

  - La autenticación es por cabeceras de RapidAPI, no OAuth2.
  - El formato de respuesta lo decide el revendedor, no Idealista, y puede cambiar
    sin avisar. Por eso `_a_listing()` es defensivo y los fixtures están grabados
    de una respuesta real.
  - Lo que SÍ es de Idealista es el `propertyCode`: coincide con el número de la
    URL del anuncio. Nuestro histórico de precios se apoya en esa clave, así que
    esto es lo que hace que todo el proyecto funcione. Si algún día el proveedor
    empezara a inventarse los IDs, el histórico se rompería en silencio.

Sobre la cuota. El plan PRO son 15.500 peticiones/mes y 1 por segundo. A 50
anuncios por página, un barrido completo de la zona norte cuesta del orden de
20-40 peticiones: cabe TODOS los días gastando menos del 10% del mes. Por eso
aquí no hay modo "solo novedades" como en el conector oficial: todo barrido es
completo, y así una bajada de precio se detecta al día siguiente en vez de tener
que esperar al barrido semanal.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from src.cuota import Cuota
from src.models import Listing
from src.sources.base import Source

log = logging.getLogger(__name__)

HOST = "idealista17.p.rapidapi.com"

#: Búsqueda circular por lat/lon/radio, no por `location_ids`. Se eligió este
#: endpoint y no `/property-search` porque encaja con el `center` + `radius_km`
#: del config y ahorra tener que resolver y cachear los IDs de los siete barrios.
SEARCH_URL = f"https://{HOST}/property-search-by-coordinates"

MAX_POR_PAGINA = 50  # `result_count` admite 1-50; el defecto del proveedor es 30

#: El plan PRO permite 1 petición por segundo. Sin freno, paginar en bucle cerrado
#: dispara veinte peticiones en dos segundos y el proveedor responde 429.
#:
#: 1,05s parecía "un pelín por encima de 1s", pero en producción daba 429 ya en la
#: segunda página: la ventana la cuenta el reloj del SERVIDOR (desde que recibe la
#: petición, no desde que la enviamos), y con la latencia de red por medio 1,05s se
#: solapaba con la ventana anterior. 1,6s deja margen de sobra y el barrido completo
#: (9 páginas) sigue tardando unos 15s, que no es nada.
INTERVALO_MIN_S = 1.6

#: Un 429 casi nunca es la cuota mensual agotada (tenemos 15.500): es rate-limit
#: transitorio. Se espera y se reintenta antes de darlo por perdido.
MAX_REINTENTOS_429 = 4

#: Red de seguridad, no una optimización. Si un día se cae `max_price` del config,
#: la búsqueda pasa de 30 páginas a 5.571 y te funde el mes en una sola ejecución.
PAGINAS_MAX_DEFECTO = 60

#: `propertyType` de Idealista -> nuestro vocabulario.
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


class RapidApiError(RuntimeError):
    pass


class RateLimitPersistente(RapidApiError):
    """429 que no se va ni tras varios reintentos con espera.

    Se distingue del resto para que `fetch()` pueda cortar el barrido y devolver lo
    que lleva, en vez de tirar el workflow entero: un rate-limit a mitad de la
    paginación no debe perder las páginas que ya sí trajimos.
    """


class ProveedorCaido(RapidApiError):
    """RapidAPI no deja ni llegar a la API. No es culpa de nuestra configuración.

    Comprobado el 2026-07-16: sale el mismo 405 sin mandar clave ninguna, en todas
    las rutas (incluida `/`), con GET y con POST, y también en las otras APIs de
    Idealista de RapidAPI (idealista7 de scraperium, idealista2 de apidojo). Un
    host inventado, en cambio, devuelve 404 "API doesn't exists": o sea, la
    nuestra existe y está deshabilitada del lado del proveedor. El playground de
    RapidAPI falla igual.

    Se distingue del resto de errores para que el cron diario no lo trate como un
    fallo nuestro: mientras dure, no se toca nada y se reintenta al día siguiente.
    En cuanto la reactiven, todo vuelve a funcionar solo.
    """


class IdealistaRapidApiSource(Source):
    #: Mismo nombre que el conector oficial A PROPÓSITO: los anuncios son los
    #: mismos y el `property_code` también, así que si algún día te aprueban la
    #: API oficial, el histórico continúa en vez de empezar de cero.
    name = "idealista"

    def __init__(self, config: dict[str, Any], cuota: Cuota) -> None:
        super().__init__(config)
        self.cuota = cuota
        self.api = config.get("api") or {}
        self._ultima_peticion: float | None = None

    # --- autenticación ------------------------------------------------------

    def _cabeceras(self) -> dict[str, str]:
        key = os.environ.get("RAPIDAPI_KEY")
        if not key:
            raise RapidApiError(
                "falta RAPIDAPI_KEY en el .env (la tienes en tu panel de RapidAPI)"
            )
        return {"X-RapidAPI-Key": key, "X-RapidAPI-Host": HOST}

    # --- búsqueda -----------------------------------------------------------

    def _parametros(self, pagina: int) -> dict[str, Any]:
        b = self.api.get("busqueda") or {}
        lat, lon = _centro(b.get("center", "40.4950,-3.7000"))
        params: dict[str, Any] = {
            "country": "es",  # obligatorio
            "search_type": "for_sale",
            "property_type": "homes",
            # El defecto del proveedor es `en`: sin esto las descripciones llegan
            # en inglés y las palabras clave de scoring (que están en español) no
            # casan con nada. No daría error: la nota de texto se quedaría clavada
            # en la base para todos los anuncios y no lo notarías.
            "language": b.get("idioma", "es"),
            "latitude": lat,
            "longitude": lon,
            "radius_km": b.get("radius_km", 5),
            "result_count": MAX_POR_PAGINA,
            "page": pagina,
            # Ascendente por fecha de publicación. No es capricho: con `newest` un
            # anuncio publicado a mitad de la paginación empuja a todos los demás
            # una posición y nos saltaríamos uno. Con `oldest` lo nuevo cae al
            # final y las páginas que ya hemos pedido no se mueven.
            "sort_order": "oldest",
        }
        # A propósito MÁS ANCHOS que los filtros de config.yaml: hay que guardar el
        # chalet de 1,3 M para detectar el día que baje a 1,05 M y entre en
        # presupuesto, y para que la mediana del barrio no salga sesgada por
        # nuestros propios criterios. Por lo mismo NO se filtra aquí por `exterior`
        # ni por barrio, aunque el proveedor deje: eso se hace en casa.
        for clave, destino in (
            ("precio_max", "max_price"),
            ("precio_min", "min_price"),
            ("m2_min", "min_size"),
            ("habitaciones_min", "min_rooms"),
        ):
            if (valor := b.get(clave)) is not None:
                params[destino] = valor
        return params

    def _frenar(self) -> None:
        """Espera lo que haga falta para no pasar de 1 petición por segundo."""
        if self._ultima_peticion is not None:
            espera = INTERVALO_MIN_S - (time.monotonic() - self._ultima_peticion)
            if espera > 0:
                time.sleep(espera)
        self._ultima_peticion = time.monotonic()

    def _pagina(self, pagina: int) -> dict[str, Any]:
        # La cuota se apunta UNA vez por página, no por intento: los reintentos por
        # rate-limit son ruido de transporte, no consumo lógico de la página.
        self.cuota.consumir(f"property-search?page={pagina}")

        for intento in range(1, MAX_REINTENTOS_429 + 1):
            self._frenar()
            r = requests.get(
                SEARCH_URL,
                headers=self._cabeceras(),
                params=self._parametros(pagina),
                timeout=60,
            )
            if r.status_code == 405 and "disabled" in r.text.lower():
                raise ProveedorCaido(
                    "RapidAPI responde 405 'The API provider has disabled request access'. "
                    "No es un fallo de configuración tuyo: pasa igual sin mandar clave, en "
                    "todas las rutas y con cualquier método, y también en las otras APIs de "
                    "Idealista de RapidAPI. Está apagada del lado del proveedor. No hay nada "
                    "que tocar aquí: en cuanto la reactiven, esto vuelve a funcionar solo."
                )
            if r.status_code == 429:
                if intento == MAX_REINTENTOS_429:
                    raise RateLimitPersistente(
                        f"429 en la página {pagina} tras {MAX_REINTENTOS_429} intentos. "
                        "Casi seguro rate-limit (tienes de sobra en la cuota mensual). "
                        "Si se repite, sube INTERVALO_MIN_S."
                    )
                espera = _espera_tras_429(r, intento)
                log.warning(
                    "429 en la página %d (intento %d/%d): espero %.1fs y reintento",
                    pagina, intento, MAX_REINTENTOS_429, espera,
                )
                time.sleep(espera)
                continue

            r.raise_for_status()
            cuerpo = r.json()
            if not cuerpo.get("success"):
                # Devuelve HTTP 200 con success:false. Sin este control, el barrido
                # seguiría como si nada y daría por desaparecido todo el catálogo.
                raise RapidApiError(f"el proveedor ha devuelto success=false: {cuerpo}")
            return cuerpo.get("data") or {}

        raise AssertionError("inalcanzable: el bucle sale por return o por raise")

    def fetch(self) -> list[Listing]:
        primera = self._pagina(1)
        total_paginas = int(primera.get("totalPages") or 1)
        tope = int(self.api.get("paginas_max", PAGINAS_MAX_DEFECTO))

        log.info(
            "idealista (rapidapi): %s resultados en %s páginas",
            primera.get("total"),
            total_paginas,
        )

        if total_paginas > tope:
            log.warning(
                "la búsqueda devuelve %d páginas y el tope es %d: me quedo con las "
                "primeras. Esto casi siempre significa que la búsqueda es demasiado "
                "ancha (¿se ha caído max_price o m2_min de config.yaml?), no que la "
                "zona tenga tanto piso.",
                total_paginas,
                tope,
            )
            total_paginas = tope

        elementos = list(_anuncios(primera))

        for pagina in range(2, total_paginas + 1):
            if self.cuota.disponibles < 1:
                # Mejor un barrido incompleto que quedarse sin cuota a mitad de mes.
                log.warning(
                    "cuota agotada en la página %d de %d: devuelvo lo que llevo.",
                    pagina,
                    total_paginas,
                )
                break
            try:
                elementos.extend(_anuncios(self._pagina(pagina)))
            except RateLimitPersistente as e:
                # A mitad del barrido, un rate-limit que no cede no debe tirar el
                # workflow y perder lo ya traído. Se corta y se devuelve lo que hay:
                # el guardarraíl del store (umbral de fetch sospechoso) evita que un
                # barrido parcial marque medio catálogo como desaparecido.
                log.warning("%s. Corto el barrido y devuelvo las %d páginas que llevo.", e, pagina - 1)
                break

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
                exterior=e.get("exterior"),  # None si el anuncio no lo declara
                lat=e.get("latitude"),
                lon=e.get("longitude"),
                foto_url=e.get("thumbnail"),
                descripcion=e.get("description") or "",
            )
        except (KeyError, TypeError, ValueError) as err:
            # Un anuncio mal formado no debe tumbar el barrido entero: nos ha
            # costado cuota traer esa página y el resto de anuncios son válidos.
            log.warning(
                "anuncio descartado por datos incompletos (%s): %s", err, e.get("propertyCode")
            )
            return None


def _espera_tras_429(r: requests.Response, intento: int) -> float:
    """Cuánto esperar tras un 429: lo que diga el servidor, o un backoff creciente."""
    cabecera = r.headers.get("Retry-After")
    if cabecera:
        try:
            return min(30.0, float(cabecera))  # topado: no queremos colgarnos minutos
        except ValueError:
            pass
    return min(30.0, 2.0 * intento)  # 2s, 4s, 6s...


def _anuncios(data: dict[str, Any]) -> list[dict[str, Any]]:
    """La lista de anuncios dentro de `data`.

    `/property-search-by-coordinates` la llama `listings`; otros endpoints del
    mismo proveedor (y la API oficial) la llaman `elementList`. Se aceptan las dos
    para no atarse a un endpoint concreto y no romper si cambian de uno a otro.
    """
    return list(data.get("listings") or data.get("elementList") or [])


def _centro(texto: str) -> tuple[float, float]:
    """'40.4950,-3.7000' -> (40.495, -3.7). El config lo guarda junto; la API los quiere sueltos."""
    lat, _, lon = str(texto).partition(",")
    return float(lat.strip()), float(lon.strip())


def _tipo(e: dict[str, Any]) -> str:
    """Este proveedor manda el tipo en `propertyType`, sin el `detailedType` del oficial.

    Se mira igualmente `detailedType` por si lo añaden: el coste es cero y el día
    que aparezca distinguirá adosados de chalets sin tocar nada.
    """
    detalle = e.get("detailedType") or {}
    return (
        _TIPOS.get(detalle.get("subTypology"))
        or _TIPOS.get(detalle.get("typology"))
        or _TIPOS.get(e.get("propertyType"))
        or "piso"
    )


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
