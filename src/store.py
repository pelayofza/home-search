"""Persistencia en SQLite: anuncios, histórico de precios y eventos.

Este módulo carga con la parte más delicada del sistema. La regla de oro es que
`diff()` NO escribe y `commit()` NO decide: así `--dry-run` puede clasificar un
lote sin tocar nada, y la web puede abrir la BD en modo lectura de verdad.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from src.models import Cambio, Listing, Novedad, Score
from src.scoring import negociacion

log = logging.getLogger(__name__)

ESQUEMA_VERSION = 3

#: Días desde que lo vimos por primera vez, NO desde que se publicó: la API no da
#: la fecha de publicación de forma fiable, y confundirlas sería mentir.
_DIAS_VISTOS = "CAST(julianday('now') - julianday(l.primera_vista) AS INTEGER)"

_N_BAJADAS = (
    "(SELECT COUNT(*) FROM eventos e WHERE e.source = l.source "
    " AND e.property_code = l.property_code AND e.tipo = 'bajada')"
)


def inicio_de_ciclo(dia_corte: int, ahora: datetime) -> datetime:
    """Cuándo empezó el ciclo de facturación que está corriendo ahora mismo."""
    # Se topa en 28 a propósito: si tu corte fuera el 31, en febrero no existe y
    # `replace(day=31)` reventaría. Adelantarlo unos días solo hace el contador
    # más conservador, que es el lado correcto por el que equivocarse.
    dia = max(1, min(28, int(dia_corte)))
    if ahora.day >= dia:
        inicio = ahora.replace(day=dia)
    else:
        # Todavía no hemos llegado al corte de este mes: el ciclo viene del anterior.
        mes_anterior = ahora.replace(day=1) - timedelta(days=1)
        inicio = mes_anterior.replace(day=dia)
    return inicio.replace(hour=0, minute=0, second=0, microsecond=0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source         TEXT NOT NULL,
    property_code  TEXT NOT NULL,
    precio         INTEGER NOT NULL,
    precio_inicial INTEGER NOT NULL,
    m2             INTEGER NOT NULL,
    habitaciones   INTEGER NOT NULL,
    banos          INTEGER NOT NULL,
    planta         INTEGER NOT NULL,
    ascensor       INTEGER NOT NULL,
    barrio         TEXT NOT NULL,
    url            TEXT NOT NULL,
    tipo           TEXT NOT NULL DEFAULT 'piso',
    familia        TEXT NOT NULL DEFAULT 'otro',
    exterior       INTEGER,              -- NULL = el anuncio no lo decía
    lat            REAL,
    lon            REAL,
    foto_url       TEXT,
    descripcion    TEXT NOT NULL DEFAULT '',
    primera_vista  TEXT NOT NULL,
    ultima_vista   TEXT NOT NULL,
    activo         INTEGER NOT NULL DEFAULT 1,
    ausencias      INTEGER NOT NULL DEFAULT 0,  -- fetches seguidos sin verlo
    PRIMARY KEY (source, property_code)
);
CREATE INDEX IF NOT EXISTS ix_listings_comparables ON listings(barrio, familia, activo);

-- Solo se inserta fila cuando el precio CAMBIA (y en la primera vista): el
-- histórico es una lista de escalones, no un log diario.
CREATE TABLE IF NOT EXISTS precios (
    source        TEXT NOT NULL,
    property_code TEXT NOT NULL,
    ts            TEXT NOT NULL,
    precio        INTEGER NOT NULL,
    PRIMARY KEY (source, property_code, ts)
);

CREATE TABLE IF NOT EXISTS eventos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    property_code   TEXT NOT NULL,
    ts              TEXT NOT NULL,
    tipo            TEXT NOT NULL,
    precio_anterior INTEGER,
    precio_nuevo    INTEGER,
    notificado      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_eventos_pendientes ON eventos(notificado, ts);

CREATE TABLE IF NOT EXISTS scores (
    source        TEXT NOT NULL,
    property_code TEXT NOT NULL,
    ts            TEXT NOT NULL,
    config_hash   TEXT NOT NULL,
    total         REAL NOT NULL,
    s_texto       REAL,
    s_precio      REAL,
    s_localizacion REAL,
    detalle       TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source, property_code)
);
CREATE INDEX IF NOT EXISTS ix_scores_total ON scores(total DESC);

-- Tu veredicto sobre cada anuncio. Guarda la nota y el desglose TAL Y COMO
-- ESTABAN cuando lo juzgaste: la nota se recalcula en cada ejecución, así que
-- sin esta foto sería imposible saber luego qué estabas viendo al decidir, que
-- es justamente lo único que sirve para calibrar el scoring.
CREATE TABLE IF NOT EXISTS opiniones (
    source        TEXT NOT NULL,
    property_code TEXT NOT NULL,
    ts            TEXT NOT NULL,
    valoracion    TEXT NOT NULL CHECK (valoracion IN ('interesa', 'descartado')),
    score         REAL,
    detalle       TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source, property_code)
);

-- Cada llamada a la API, para no pasarnos de cuota. Se apunta ANTES de llamar:
-- si se apuntara después, una llamada que revienta a mitad no se contaría y el
-- contador iría por detrás de la realidad, que es justo cuando duele.
CREATE TABLE IF NOT EXISTS api_calls (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source   TEXT NOT NULL,
    ts       TEXT NOT NULL,
    endpoint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_api_calls_ts ON api_calls(source, ts);

-- Foto periódica del mercado: no solo qué comprar, también cuándo.
CREATE TABLE IF NOT EXISTS mercado (
    fecha      TEXT NOT NULL,
    barrio     TEXT NOT NULL,
    familia    TEXT NOT NULL,
    mediana_m2 REAL NOT NULL,
    muestra    INTEGER NOT NULL,
    PRIMARY KEY (fecha, barrio, familia)
);
"""

#: Si un fetch trae menos de esta fracción de los anuncios activos, algo ha ido
#: mal (rate limit, paginación a medias) y NO se marca nada como desaparecido.
UMBRAL_FETCH_SOSPECHOSO = 0.5

#: Ausencias consecutivas antes de dar un anuncio por retirado.
AUSENCIAS_PARA_RETIRAR = 2


class EsquemaObsoleto(RuntimeError):
    """La BD es de una versión anterior y tiene datos que no sé migrar."""


class Store:
    def __init__(self, db_path: str | Path = "data/listings.db", *, readonly: bool = False) -> None:
        self.db_path = Path(db_path)
        self.readonly = readonly

        # Siempre por URI, también al escribir: sin `uri=True`, un ATTACH con
        # 'file:...' se toma como nombre de fichero literal y falla. Lo necesita
        # importar().
        if readonly:
            # Que el modo lectura sea una garantía del driver, no una promesa mía.
            self.conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        else:
            if self.db_path.parent != Path(""):
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=rwc", uri=True)

        self.conn.row_factory = sqlite3.Row
        if readonly:
            self._exigir_esquema_al_dia()
        else:
            # WAL: la web puede leer mientras el cron escribe.
            self.conn.execute("PRAGMA journal_mode = WAL")
            self._migrar()

    # --- ciclo de vida ------------------------------------------------------

    def _exigir_esquema_al_dia(self) -> None:
        """Una conexión de lectura no puede migrar: al menos que lo diga claro.

        Sin esto, abrir la web tras actualizar el código falla con un
        `no such table: opiniones` que no le dice nada a nadie.
        """
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < ESQUEMA_VERSION:
            raise EsquemaObsoleto(
                f"{self.db_path} tiene el esquema v{version} y el código espera "
                f"v{ESQUEMA_VERSION}. Ejecuta `python main.py --rescore` (o cualquier "
                "ejecución normal) para migrarla: la web abre la BD en solo lectura "
                "y no puede hacerlo ella."
            )

    def _migrar(self) -> None:
        """Solo sirve para cambios aditivos: todo el esquema es IF NOT EXISTS.

        El día que haya que ALTERar o renombrar una columna, esto se queda corto
        y habrá que pasar a migraciones numeradas de verdad.
        """
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == ESQUEMA_VERSION:
            return

        if version == 0 and self._tabla_existe("listings"):
            # BD de las primeras pruebas, anterior al versionado.
            filas = self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            if filas:
                raise EsquemaObsoleto(
                    f"{self.db_path} tiene {filas} anuncios con el esquema antiguo. "
                    "Bórrala o expórtala a mano; no hay migración automática."
                )
            log.warning("BD antigua vacía en %s: la recreo", self.db_path)
            self.conn.executescript(
                "DROP TABLE IF EXISTS listings; DROP TABLE IF EXISTS precios;"
            )

        self.conn.executescript(_SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {ESQUEMA_VERSION}")
        self.conn.commit()

    def _tabla_existe(self, nombre: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nombre,)
        ).fetchone()
        return row is not None

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # --- consultas simples --------------------------------------------------

    def is_known(self, source: str, property_code: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM listings WHERE source = ? AND property_code = ?",
                (source, property_code),
            ).fetchone()
            is not None
        )

    def count(self, *, solo_activos: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM listings"
        if solo_activos:
            sql += " WHERE activo = 1"
        return self.conn.execute(sql).fetchone()[0]

    def historico(self, source: str, property_code: str) -> list[tuple[str, int]]:
        """Los escalones de precio, del más antiguo al más reciente."""
        return [
            (r["ts"], r["precio"])
            for r in self.conn.execute(
                "SELECT ts, precio FROM precios "
                "WHERE source = ? AND property_code = ? ORDER BY ts",
                (source, property_code),
            )
        ]

    # --- el núcleo: clasificar (solo lee) y confirmar (solo escribe) ---------

    def diff(self, source: str, listings: Sequence[Listing]) -> list[Novedad]:
        """Clasifica un lote contra lo que hay en la BD. No escribe nada."""
        vistos: dict[str, Listing] = {}
        for l in listings:  # dedup dentro del propio lote
            vistos.setdefault(l.property_code, l)

        conocidos = {
            r["property_code"]: r
            for r in self.conn.execute(
                "SELECT property_code, precio, activo FROM listings WHERE source = ?",
                (source,),
            )
        }

        novedades: list[Novedad] = []
        for code, listing in vistos.items():
            fila = conocidos.get(code)
            if fila is None:
                novedades.append(Novedad(listing, Cambio.NUEVO))
            elif not fila["activo"]:
                novedades.append(Novedad(listing, Cambio.REAPARECIDO, fila["precio"]))
            elif listing.precio < fila["precio"]:
                novedades.append(Novedad(listing, Cambio.BAJADA, fila["precio"]))
            elif listing.precio > fila["precio"]:
                novedades.append(Novedad(listing, Cambio.SUBIDA, fila["precio"]))
            else:
                novedades.append(Novedad(listing, Cambio.IGUAL, fila["precio"]))

        novedades.extend(self._desaparecidos(source, vistos, conocidos))
        resumen = {c.value: 0 for c in Cambio}
        for n in novedades:
            resumen[n.cambio.value] += 1
        log.info("diff: %s", ", ".join(f"{k}={v}" for k, v in resumen.items() if v))
        return novedades

    def _desaparecidos(
        self,
        source: str,
        vistos: Mapping[str, Listing],
        conocidos: Mapping[str, sqlite3.Row],
    ) -> list[Novedad]:
        """Anuncios activos que ya no vienen en el lote.

        Un fetch a medias (rate limit, paginación rota) marcaría media BD como
        retirada y llenaría el email de ruido, así que si el lote es
        sospechosamente pequeño no se concluye nada.
        """
        activos = [c for c, r in conocidos.items() if r["activo"]]
        if not activos:
            return []

        if len(vistos) < UMBRAL_FETCH_SOSPECHOSO * len(activos):
            log.warning(
                "fetch sospechoso (%d anuncios frente a %d activos): "
                "no marco desaparecidos en esta ronda",
                len(vistos),
                len(activos),
            )
            return []

        return [
            Novedad(self._listing_de_bd(source, code), Cambio.DESAPARECIDO)
            for code in activos
            if code not in vistos
        ]

    def _listing_de_bd(self, source: str, property_code: str) -> Listing:
        r = self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND property_code = ?",
            (source, property_code),
        ).fetchone()
        return Listing(
            property_code=r["property_code"],
            precio=r["precio"],
            m2=r["m2"],
            habitaciones=r["habitaciones"],
            banos=r["banos"],
            planta=r["planta"],
            ascensor=bool(r["ascensor"]),
            barrio=r["barrio"],
            url=r["url"],
            tipo=r["tipo"],
            exterior=None if r["exterior"] is None else bool(r["exterior"]),
            lat=r["lat"],
            lon=r["lon"],
            foto_url=r["foto_url"],
            descripcion=r["descripcion"],
            fecha_vista=date.fromisoformat(r["ultima_vista"][:10]),
        )

    def commit(
        self,
        source: str,
        novedades: Sequence[Novedad],
        *,
        ahora: datetime | None = None,
    ) -> None:
        """Persiste el lote ya clasificado: anuncios, escalones de precio y eventos.

        El timestamp lleva microsegundos a propósito: con precisión de segundos,
        dos ejecuciones seguidas chocan con la clave primaria de `precios` y el
        INSERT OR IGNORE se traga una bajada real sin decir nada.
        """
        ts = (ahora or datetime.now()).isoformat(timespec="microseconds")

        with self.conn:
            for n in novedades:
                if n.cambio is Cambio.DESAPARECIDO:
                    self._registrar_ausencia(source, n, ts)
                    continue

                self._upsert_listing(source, n.listing, ts)

                cambio_de_precio = (
                    n.precio_anterior is not None and n.precio_anterior != n.listing.precio
                )
                if n.cambio is Cambio.NUEVO or cambio_de_precio:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO precios (source, property_code, ts, precio) "
                        "VALUES (?, ?, ?, ?)",
                        (source, n.listing.property_code, ts, n.listing.precio),
                    )

                if n.cambio is not Cambio.IGUAL:
                    self._registrar_evento(source, n, ts)

    def _upsert_listing(self, source: str, l: Listing, ts: str) -> None:
        self.conn.execute(
            """
            INSERT INTO listings (
                source, property_code, precio, precio_inicial, m2, habitaciones, banos,
                planta, ascensor, barrio, url, tipo, familia, exterior, lat, lon,
                foto_url, descripcion, primera_vista, ultima_vista, activo, ausencias
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
            ON CONFLICT(source, property_code) DO UPDATE SET
                precio       = excluded.precio,
                m2           = excluded.m2,
                habitaciones = excluded.habitaciones,
                banos        = excluded.banos,
                planta       = excluded.planta,
                ascensor     = excluded.ascensor,
                barrio       = excluded.barrio,
                url          = excluded.url,
                tipo         = excluded.tipo,
                familia      = excluded.familia,
                exterior     = excluded.exterior,
                lat          = COALESCE(excluded.lat, listings.lat),
                lon          = COALESCE(excluded.lon, listings.lon),
                foto_url     = excluded.foto_url,
                descripcion  = excluded.descripcion,
                ultima_vista = excluded.ultima_vista,
                activo       = 1,
                ausencias    = 0
            """,
            (
                source,
                l.property_code,
                l.precio,
                l.precio,  # precio_inicial: solo cuenta en el INSERT
                l.m2,
                l.habitaciones,
                l.banos,
                l.planta,
                int(l.ascensor),
                l.barrio,
                l.url,
                l.tipo,
                l.familia,
                None if l.exterior is None else int(l.exterior),
                l.lat,
                l.lon,
                l.foto_url,
                l.descripcion,
                ts,
                ts,
            ),
        )

    def _registrar_ausencia(self, source: str, n: Novedad, ts: str) -> None:
        code = n.listing.property_code
        fila = self.conn.execute(
            "SELECT ausencias FROM listings WHERE source = ? AND property_code = ?",
            (source, code),
        ).fetchone()
        ausencias = (fila["ausencias"] if fila else 0) + 1

        if ausencias < AUSENCIAS_PARA_RETIRAR:
            # Todavía puede ser un hipo de la API, no una venta.
            self.conn.execute(
                "UPDATE listings SET ausencias = ? WHERE source = ? AND property_code = ?",
                (ausencias, source, code),
            )
            return

        cur = self.conn.execute(
            "UPDATE listings SET ausencias = ?, activo = 0 "
            "WHERE source = ? AND property_code = ? AND activo = 1",
            (ausencias, source, code),
        )
        if cur.rowcount:  # solo emitimos el evento la primera vez que lo damos por retirado
            self._registrar_evento(source, n, ts)

    def _registrar_evento(self, source: str, n: Novedad, ts: str) -> None:
        self.conn.execute(
            "INSERT INTO eventos (source, property_code, ts, tipo, precio_anterior, precio_nuevo) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                source,
                n.listing.property_code,
                ts,
                n.cambio.value,
                n.precio_anterior,
                n.listing.precio,
            ),
        )

    def sync(self, source: str, listings: Sequence[Listing]) -> list[Novedad]:
        """Azúcar: diff() + commit()."""
        novedades = self.diff(source, listings)
        self.commit(source, novedades)
        return novedades

    def filter_new(self, source: str, listings: Sequence[Listing]) -> list[Listing]:
        """Solo los anuncios que no habíamos visto nunca. No escribe."""
        return [n.listing for n in self.diff(source, listings) if n.cambio is Cambio.NUEVO]

    # --- notificación idempotente -------------------------------------------

    def eventos_pendientes(self, *, tipos: Sequence[str] | None = None) -> list[Novedad]:
        """Eventos aún sin notificar. Si el email falla, siguen aquí mañana."""
        sql = (
            "SELECT e.id, e.source, e.property_code, e.tipo, e.precio_anterior "
            "FROM eventos e WHERE e.notificado = 0"
        )
        params: list[Any] = []
        if tipos:
            sql += f" AND e.tipo IN ({','.join('?' * len(tipos))})"
            params += list(tipos)
        sql += " ORDER BY e.ts"

        novedades = []
        for r in self.conn.execute(sql, params).fetchall():
            ficha = self.ficha(r["source"], r["property_code"])
            novedades.append(
                Novedad(
                    listing=self._listing_de_bd(r["source"], r["property_code"]),
                    cambio=Cambio(r["tipo"]),
                    precio_anterior=r["precio_anterior"],
                    score=self.score_de(r["source"], r["property_code"]),
                    evento_id=r["id"],
                    dias_vistos=ficha["dias_vistos"] if ficha else None,
                    bajadas=(ficha["bajadas"] if ficha else 0) or 0,
                    negociacion=ficha["negociacion"] if ficha else None,
                )
            )
        return novedades

    def marcar_notificados(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE eventos SET notificado = 1 WHERE id = ?", [(i,) for i in ids]
            )

    # --- comparables --------------------------------------------------------

    def mediana_precio_m2(
        self,
        barrio: str,
        familia: str,
        *,
        source: str | None = None,
        excluir: str | None = None,
        min_muestra: int = 8,
        dias: int = 180,
        rango_valido: tuple[int, int] = (1500, 12000),
    ) -> tuple[float | None, int]:
        """(mediana €/m², tamaño de muestra). Mediana None si no hay muestra suficiente.

        Se acota por `source` a propósito: los cinco anuncios inventados del mock
        no pueden entrar en la mediana con la que se juzga un piso real. Se excluye
        también el propio anuncio (con n pequeño se autopuntuaría hacia 50), se
        recortan los outliers por MAD y se descartan los €/m² imposibles, que
        suelen ser parseos rotos (una parcela contada como superficie construida).
        """
        desde = (datetime.now() - timedelta(days=dias)).isoformat(timespec="seconds")
        sql = (
            "SELECT property_code, precio, m2 FROM listings "
            "WHERE barrio = ? AND familia = ? AND ultima_vista >= ? AND m2 BETWEEN 30 AND 600"
        )
        params: list[Any] = [barrio, familia, desde]
        if source:
            sql += " AND source = ?"
            params.append(source)
        filas = self.conn.execute(sql, params).fetchall()

        lo, hi = rango_valido
        muestra = [
            r["precio"] / r["m2"]
            for r in filas
            if r["property_code"] != excluir and lo <= r["precio"] / r["m2"] <= hi
        ]
        muestra = _recortar_outliers(muestra)

        if len(muestra) < min_muestra:
            return None, len(muestra)
        return statistics.median(muestra), len(muestra)

    # --- derivados: geo y scores --------------------------------------------

    def guardar_scores(
        self, source: str, scores: Mapping[str, Score], config_hash: str
    ) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO scores (source, property_code, ts, config_hash, total,
                                    s_texto, s_precio, s_localizacion, detalle)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, property_code) DO UPDATE SET
                    ts = excluded.ts, config_hash = excluded.config_hash,
                    total = excluded.total, s_texto = excluded.s_texto,
                    s_precio = excluded.s_precio, s_localizacion = excluded.s_localizacion,
                    detalle = excluded.detalle
                """,
                [
                    (
                        source,
                        code,
                        ts,
                        config_hash,
                        s.total,
                        s.texto,
                        s.precio,
                        s.localizacion,
                        json.dumps(s.detalle, ensure_ascii=False),
                    )
                    for code, s in scores.items()
                ],
            )

    def score_de(self, source: str, property_code: str) -> Score | None:
        r = self.conn.execute(
            "SELECT * FROM scores WHERE source = ? AND property_code = ?",
            (source, property_code),
        ).fetchone()
        if r is None:
            return None
        return Score(
            total=r["total"],
            texto=r["s_texto"],
            precio=r["s_precio"],
            localizacion=r["s_localizacion"],
            detalle=json.loads(r["detalle"]),
        )

    def activos(self, source: str) -> list[Listing]:
        codes = [
            r["property_code"]
            for r in self.conn.execute(
                "SELECT property_code FROM listings WHERE source = ? AND activo = 1",
                (source,),
            )
        ]
        return [self._listing_de_bd(source, c) for c in codes]

    def purgar(self, source: str) -> dict[str, int]:
        """Borra todo lo de una fuente. Para limpiar el mock al pasar a producción."""
        borradas = {}
        with self.conn:
            for tabla in ("listings", "precios", "eventos", "scores", "opiniones", "api_calls"):
                cur = self.conn.execute(f"DELETE FROM {tabla} WHERE source = ?", (source,))
                borradas[tabla] = cur.rowcount
        log.warning(
            "purgada la fuente %s: %s",
            source,
            ", ".join(f"{t}={n}" for t, n in borradas.items() if n),
        )
        return borradas

    # --- sincronización con la BD que genera GitHub Actions -----------------

    #: GitHub es el único que busca, así que es el dueño de estas tablas.
    #: `opiniones` NO está: esa es tuya, la creas votando en la web local, y una
    #: importación jamás debe pisártela.
    TABLAS_REMOTAS = ("listings", "precios", "eventos", "scores", "mercado", "api_calls")

    def importar(self, ruta: str | Path) -> dict[str, int]:
        """Trae la BD que ha generado el cron de GitHub, conservando tus votos."""
        ruta = Path(ruta)
        if not ruta.exists():
            raise FileNotFoundError(f"no encuentro {ruta}")

        version = sqlite3.connect(f"file:{ruta.as_posix()}?mode=ro", uri=True)
        try:
            remota = version.execute("PRAGMA user_version").fetchone()[0]
        finally:
            version.close()

        if remota != ESQUEMA_VERSION:
            raise EsquemaObsoleto(
                f"{ruta} tiene el esquema v{remota} y este código espera v{ESQUEMA_VERSION}. "
                "Actualiza el código (o el workflow) antes de importar."
            )

        importadas: dict[str, int] = {}
        # ATTACH y DETACH van FUERA de la transacción: SQLite no deja soltar una
        # base de datos con una transacción abierta encima.
        self.conn.execute(f"ATTACH DATABASE 'file:{ruta.as_posix()}?mode=ro' AS remoto")
        try:
            with self.conn:
                for tabla in self.TABLAS_REMOTAS:
                    # Reemplazo completo, no fusión: GitHub es la única fuente de
                    # verdad de estas tablas, así que intentar mezclarlas solo
                    # crearía conflictos imaginarios.
                    self.conn.execute(f"DELETE FROM {tabla}")
                    cur = self.conn.execute(f"INSERT INTO {tabla} SELECT * FROM remoto.{tabla}")
                    importadas[tabla] = cur.rowcount
        finally:
            self.conn.execute("DETACH DATABASE remoto")

        votos = self.conn.execute("SELECT COUNT(*) FROM opiniones").fetchone()[0]
        log.info(
            "importado de %s: %s. Tus %d valoraciones siguen intactas.",
            ruta,
            ", ".join(f"{t}={n}" for t, n in importadas.items()),
            votos,
        )
        return importadas

    # --- cuota de API -------------------------------------------------------

    def apuntar_llamada(self, source: str, endpoint: str) -> None:
        """Apunta una llamada. Llámalo ANTES de hacerla, no después.

        Si se apuntara después, una llamada que revienta a medias (timeout tras
        haber consumido cuota en el servidor) no se contaría, y el contador iría
        por detrás de la realidad justo el día que más importa.
        """
        with self.conn:
            self.conn.execute(
                "INSERT INTO api_calls (source, ts, endpoint) VALUES (?, ?, ?)",
                (source, datetime.now().isoformat(timespec="seconds"), endpoint),
            )

    def llamadas_del_mes(self, source: str, mes: str | None = None) -> int:
        mes = mes or datetime.now().strftime("%Y-%m")
        return self.conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE source = ? AND ts LIKE ?",
            (source, f"{mes}%"),
        ).fetchone()[0]

    def llamadas_del_ciclo(
        self, source: str, dia_corte: int = 1, ahora: datetime | None = None
    ) -> int:
        """Llamadas desde que empezó el ciclo de facturación en curso.

        No es lo mismo que el mes natural, y la diferencia es la que te cuesta
        dinero: RapidAPI reinicia tu contador el día que te suscribiste, no el 1.
        Contando por mes natural con un corte el día 20 podrías gastar la cuota
        entera del 20 al 31 (mes A) y otra vez del 1 al 19 (mes B) — el doble
        dentro del mismo ciclo de facturación, y ahí llega el recargo.
        """
        inicio = inicio_de_ciclo(dia_corte, ahora or datetime.now())
        return self.conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE source = ? AND ts >= ?",
            (source, inicio.isoformat(timespec="seconds")),
        ).fetchone()[0]

    def consumo_por_mes(self, source: str) -> list[tuple[str, int]]:
        return [
            (r[0], r[1])
            for r in self.conn.execute(
                "SELECT substr(ts, 1, 7) AS mes, COUNT(*) FROM api_calls "
                "WHERE source = ? GROUP BY mes ORDER BY mes DESC",
                (source,),
            )
        ]

    # --- mercado ------------------------------------------------------------

    def snapshot_mercado(self, min_muestra: int = 8) -> int:
        """Congela la mediana de €/m² de cada (barrio, familia). Una foto al día."""
        hoy = datetime.now().date().isoformat()
        pares = self.conn.execute(
            "SELECT DISTINCT barrio, familia FROM listings WHERE activo = 1"
        ).fetchall()

        filas = []
        for barrio, familia in pares:
            mediana, n = self.mediana_precio_m2(barrio, familia, min_muestra=min_muestra)
            if mediana is not None:
                filas.append((hoy, barrio, familia, mediana, n))

        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO mercado (fecha, barrio, familia, mediana_m2, muestra) "
                "VALUES (?, ?, ?, ?, ?)",
                filas,
            )
        return len(filas)

    def serie_mercado(self, familia: str = "piso") -> dict[str, list[tuple[str, float]]]:
        """{barrio: [(fecha, mediana €/m²), ...]} para dibujar la evolución."""
        series: dict[str, list[tuple[str, float]]] = {}
        for r in self.conn.execute(
            "SELECT barrio, fecha, mediana_m2 FROM mercado WHERE familia = ? "
            "ORDER BY barrio, fecha",
            (familia,),
        ):
            series.setdefault(r["barrio"], []).append((r["fecha"], r["mediana_m2"]))
        return series

    # --- opiniones ----------------------------------------------------------

    def opinar(self, source: str, property_code: str, valoracion: str) -> None:
        """Guarda tu veredicto, con una foto de la nota que estabas viendo.

        La nota se recalcula en cada ejecución. Si no congelásemos aquí el score
        y su desglose, dentro de un mes sabríamos que descartaste un anuncio pero
        no qué te enseñaba el sistema cuando lo hiciste, que es lo único con lo
        que se puede calibrar nada.
        """
        if valoracion not in ("interesa", "descartado"):
            raise ValueError(f"valoración desconocida: {valoracion!r}")

        score = self.score_de(source, property_code)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO opiniones (source, property_code, ts, valoracion, score, detalle)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, property_code) DO UPDATE SET
                    ts = excluded.ts, valoracion = excluded.valoracion,
                    score = excluded.score, detalle = excluded.detalle
                """,
                (
                    source,
                    property_code,
                    datetime.now().isoformat(timespec="seconds"),
                    valoracion,
                    score.total if score else None,
                    json.dumps(score.detalle, ensure_ascii=False) if score else "{}",
                ),
            )

    def borrar_opinion(self, source: str, property_code: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM opiniones WHERE source = ? AND property_code = ?",
                (source, property_code),
            )

    def opiniones(self) -> list[dict[str, Any]]:
        """Todo lo valorado, con la foto del score de aquel momento."""
        filas = []
        for r in self.conn.execute(
            "SELECT o.*, l.barrio, l.precio, l.m2, l.tipo, l.descripcion "
            "FROM opiniones o JOIN listings l "
            "  ON l.source = o.source AND l.property_code = o.property_code"
        ):
            fila = dict(r)
            fila["detalle"] = json.loads(fila["detalle"] or "{}")
            filas.append(fila)
        return filas

    # --- lectura para la web ------------------------------------------------

    def buscar(
        self,
        *,
        barrio: str | None = None,
        min_score: float | None = None,
        solo_bajadas: bool = False,
        solo_activos: bool = True,
        ocultar_descartados: bool = True,
        orden: str = "score",
        limite: int = 200,
    ) -> list[dict[str, Any]]:
        ordenes = {
            "score": "COALESCE(s.total, -1) DESC",
            "precio": "l.precio ASC",
            "precio_m2": "(CAST(l.precio AS REAL) / l.m2) ASC",
            "m2": "l.m2 DESC",
            "reciente": "l.primera_vista DESC",
            "bajada": "(l.precio - l.precio_inicial) ASC",
        }
        sql = [
            "SELECT l.*, s.total AS score, s.detalle AS score_detalle,",
            "       o.valoracion AS opinion,",
            "       (l.precio - l.precio_inicial) AS variacion,",
            f"       {_DIAS_VISTOS} AS dias_vistos,",
            f"       {_N_BAJADAS} AS bajadas",
            "FROM listings l",
            "LEFT JOIN scores s ON s.source = l.source AND s.property_code = l.property_code",
            "LEFT JOIN opiniones o ON o.source = l.source AND o.property_code = l.property_code",
            "WHERE 1 = 1",
        ]
        params: list[Any] = []
        if solo_activos:
            sql.append("AND l.activo = 1")
        if ocultar_descartados:
            sql.append("AND COALESCE(o.valoracion, '') != 'descartado'")
        if barrio:
            sql.append("AND l.barrio = ?")
            params.append(barrio)
        if min_score is not None:
            sql.append("AND s.total >= ?")
            params.append(min_score)
        if solo_bajadas:
            sql.append("AND l.precio < l.precio_inicial")
        sql.append(f"ORDER BY {ordenes.get(orden, ordenes['score'])} LIMIT ?")
        params.append(limite)

        return [dict(r) for r in self.conn.execute(" ".join(sql), params)]

    def ficha(self, source: str, property_code: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT l.*, s.total AS score, s.s_texto, s.s_precio, s.s_localizacion, "
            "       s.detalle AS score_detalle, o.valoracion AS opinion, "
            "       (l.precio - l.precio_inicial) AS variacion, "
            f"      {_DIAS_VISTOS} AS dias_vistos, {_N_BAJADAS} AS bajadas "
            "FROM listings l "
            "LEFT JOIN scores s ON s.source = l.source AND s.property_code = l.property_code "
            "LEFT JOIN opiniones o ON o.source = l.source AND o.property_code = l.property_code "
            "WHERE l.source = ? AND l.property_code = ?",
            (source, property_code),
        ).fetchone()
        if r is None:
            return None
        ficha = dict(r)
        ficha["historico"] = self.historico(source, property_code)
        ficha["score_detalle"] = json.loads(ficha["score_detalle"] or "{}")
        ficha["negociacion"] = self._negociacion(ficha)
        return ficha

    def _negociacion(self, fila: dict[str, Any]) -> dict[str, Any]:
        desviacion = ((fila.get("score_detalle") or {}).get("precio") or {}).get("desviacion_pct")
        acumulada = (
            100 * (fila["precio"] - fila["precio_inicial"]) / fila["precio_inicial"]
            if fila["precio_inicial"]
            else 0.0
        )
        estimacion = negociacion.estimar(
            dias_vistos=fila.get("dias_vistos"),
            bajadas=fila.get("bajadas") or 0,
            bajada_acumulada_pct=acumulada,
            desviacion_vs_mediana=desviacion,
        )
        estimacion["objetivo"] = negociacion.objetivo(fila["precio"], estimacion["margen_pct"])
        return estimacion

    def barrios(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT barrio FROM listings WHERE activo = 1 ORDER BY barrio"
            )
        ]


def _recortar_outliers(valores: list[float], k: float = 3.0) -> list[float]:
    """Descarta lo que se aleje más de k desviaciones robustas (MAD) de la mediana.

    Con la desviación típica no serviría de nada: el propio outlier la infla y
    acaba tapándose a sí mismo.
    """
    if len(valores) < 4:
        return valores
    med = statistics.median(valores)
    mad = statistics.median([abs(v - med) for v in valores])
    if mad == 0:
        return valores
    limite = k * 1.4826 * mad
    return [v for v in valores if abs(v - med) <= limite]
