"""Orquestador: busca anuncios, los guarda, los puntúa y avisa por email.

El orden importa. Primero se persiste TODO lo que devuelve la fuente (sin
filtrar), y solo después se decide qué se notifica:

  - Un anuncio de 1.150.000 € que baja a 1.050.000 € solo se puede detectar como
    BAJADA si lo guardamos cuando aún estaba fuera de presupuesto.
  - La mediana de €/m² del barrio no sirve de nada si se calcula sobre una
    muestra recortada por nuestros propios criterios.

Uso:
    python main.py --dry-run       # no escribe ni envía; solo cuenta qué haría
    python main.py                 # ejecución real
    python main.py --rescore       # recalcula notas sin tocar la red
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from src import filters, notify, scoring
from src.cuota import Cuota, CuotaAgotada
from src.models import Cambio
from src.scoring import calibracion
from src.sources import Source
from src.sources.idealista import IdealistaSource
from src.sources.mock import MockSource
from src.sources.rapidapi import IdealistaRapidApiSource, ProveedorCaido
from src.store import Store

log = logging.getLogger("home-search")

SOURCES = ("mock", "idealista", "idealista-oficial")


def crear_source(nombre: str, config: dict, cuota: Cuota) -> Source:
    """El mock no gasta cuota; Idealista sí, y por eso recibe el guardarraíl.

    `idealista` es RapidAPI, que es lo que tenemos contratado. `idealista-oficial`
    es el conector de la API oficial de Idealista: está escrito pero nunca se ha
    llegado a ejecutar, porque el acceso no es de autoservicio y no nos lo han
    aprobado. Los dos guardan bajo el mismo nombre de fuente ("idealista") a
    propósito: son los mismos anuncios con el mismo `property_code`, así que el
    histórico continuaría sin cortarse si algún día se cambia de uno a otro.
    """
    if nombre == "idealista":
        return IdealistaRapidApiSource(config, cuota)
    if nombre == "idealista-oficial":
        return IdealistaSource(config, cuota)
    return MockSource(config)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Busca vivienda en Madrid y avisa por email.")
    p.add_argument("--config", default="config.yaml", help="ruta al config.yaml")
    p.add_argument("--source", default="mock", choices=sorted(SOURCES), help="de dónde bajar")
    p.add_argument(
        "--completo",
        action="store_true",
        help="solo para --source idealista-oficial: barrido completo en vez de solo las "
        "novedades de la semana. Con RapidAPI se ignora, porque ahí la cuota da de sobra "
        "para barrer entero todos los días y siempre se hace",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="enseña qué haría, sin escribir en la BD ni enviar email",
    )
    p.add_argument(
        "--rescore",
        action="store_true",
        help="recalcula las notas de lo ya guardado y sale (no toca la red)",
    )
    p.add_argument(
        "--calibrar",
        action="store_true",
        help="compara lo que has marcado como interesante con lo que has descartado",
    )
    p.add_argument("--cuota", action="store_true", help="cuántas peticiones te quedan este mes")
    p.add_argument(
        "--importar",
        metavar="RUTA",
        help="trae la BD que ha generado el cron de GitHub, conservando tus valoraciones",
    )
    p.add_argument(
        "--purgar",
        metavar="FUENTE",
        help="borra TODO lo de una fuente (p. ej. `--purgar mock` al pasar a producción)",
    )
    p.add_argument("--no-email", action="store_true", help="busca y guarda, pero no envía")
    p.add_argument(
        "--force-send",
        action="store_true",
        help="reenvía todo lo que pase los filtros, ignorando qué se notificó ya",
    )
    p.add_argument(
        "--mock-fase",
        type=int,
        default=1,
        help="qué lote devuelve el mock (2 = un día después, con bajadas)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="logs de debug")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    config = filters.load_config(args.config)
    db_path = (config.get("almacenamiento") or {}).get("db_path", "data/listings.db")
    criterios = config.get("filtros") or {}

    if args.importar:
        with Store(db_path) as store:
            store.importar(args.importar)
        return 0

    if args.purgar:
        with Store(db_path) as store:
            store.purgar(args.purgar)
        return 0

    if args.calibrar:
        with Store(db_path) as store:
            print(calibracion.informe(calibracion.analizar(store.opiniones())))
        return 0

    if args.cuota:
        with Store(db_path) as store:
            print(Cuota(store, args.source, config).informe())
        return 0

    if args.rescore:
        with Store(db_path) as store:
            n = scoring.puntuar_todos(store, args.source, config)
            log.info("recalculadas %d notas", n)
        return 0

    config.setdefault("mock", {})["fase"] = args.mock_fase

    with Store(db_path) as store:
        cuota = Cuota(store, args.source, config)
        source = crear_source(args.source, config, cuota)

        crudos = (
            source.fetch(solo_recientes=not args.completo)
            if isinstance(source, IdealistaSource)
            else source.fetch()
        )
        log.info("%s: %d anuncios recibidos", source.name, len(crudos))

        novedades = store.diff(source.name, crudos)

        # Un barrido de solo novedades no ha mirado el catálogo entero: dar por
        # retirado lo que no ha salido sería absurdo, porque nunca iba a salir.
        # Solo aplica al conector oficial: RapidAPI barre siempre entero, y ahí
        # una ausencia sí significa que el anuncio ya no está.
        if isinstance(source, IdealistaSource) and not args.completo:
            novedades = [n for n in novedades if n.cambio is not Cambio.DESAPARECIDO]

        if args.dry_run:
            _resumen_dry_run(novedades, criterios)
            return 0

        # Se guarda ANTES de enviar. Si el SMTP falla, el histórico ya está a
        # salvo y el evento sigue pendiente: se reintenta mañana.
        store.commit(source.name, novedades)
        scoring.puntuar_todos(store, source.name, config)
        store.snapshot_mercado()

        if args.no_email:
            log.info("--no-email: guardado sin notificar")
            return 0

        if args.force_send:
            todos = store.eventos_pendientes()
            notify.send([n for n in todos if filters.matches(n.listing, criterios)], config)
            return 0

        pendientes = store.eventos_pendientes(tipos=[Cambio.NUEVO, Cambio.BAJADA])
        notificables = [
            n
            for n in pendientes
            if filters.matches(n.listing, criterios) and notify.sobre_el_umbral(n, config)
        ]
        log.info(
            "%d eventos pendientes, %d se notifican", len(pendientes), len(notificables)
        )

        notify.send(notificables, config)
        # Se marcan TODOS los pendientes, también los que no pasan los filtros:
        # si no, se acumularían para siempre y los reevaluaríamos cada día.
        store.marcar_notificados([n.evento_id for n in pendientes if n.evento_id])

    return 0


def _resumen_dry_run(novedades: list, criterios: dict) -> None:
    resumen: dict[str, int] = {}
    for n in novedades:
        resumen[n.cambio.value] = resumen.get(n.cambio.value, 0) + 1
    log.info("[dry-run] cambios detectados: %s", resumen or "ninguno")

    interesantes = [
        n
        for n in novedades
        if n.cambio in (Cambio.NUEVO, Cambio.BAJADA) and filters.matches(n.listing, criterios)
    ]
    log.info("[dry-run] pasarían los filtros y se notificarían: %d", len(interesantes))
    if interesantes:
        log.info(
            "[dry-run] email aproximado (SIN notas ni margen de negociación: en seco no se "
            "puede puntuar lo que todavía no está guardado, y el umbral min_score no se "
            "aplica):\n%s",
            notify.render_text(interesantes),
        )
    log.info("[dry-run] no se ha escrito nada en la BD")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()

    try:
        return run(args)
    except ProveedorCaido as e:
        # Código propio para que el cron lo distinga de un fallo nuestro y no te
        # mande un aviso de error cada mañana por algo que no puedes arreglar.
        # No se ha escrito nada: mañana se reintenta y, cuando vuelva, sigue solo.
        log.warning("la API del proveedor está caída: %s", e)
        return 4
    except CuotaAgotada as e:
        log.error("cuota agotada: %s", e)
        return 3
    except notify.ConfigError as e:
        log.error("configuración incompleta: %s", e)
        return 2
    except Exception:
        log.exception("la ejecución ha fallado")
        return 1


if __name__ == "__main__":
    sys.exit(main())
