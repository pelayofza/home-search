# home-search

Busca anuncios de compra de vivienda en Madrid, los puntúa, avisa por email de
las novedades y de las **bajadas de precio**, y los enseña en un panel web.

## Puesta en marcha

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env          # y rellena SMTP_*
python -m src.geo.pois --refresh   # descarga los POIs de OpenStreetMap (una vez)
```

## Uso

```bash
python main.py --dry-run      # qué haría, sin escribir ni enviar
python main.py --rescore      # recalcula las notas sin tocar la red
python main.py --calibrar     # qué distingue lo que te gusta de lo que descartas
python main.py --cuota        # cuántas peticiones a la API te quedan este mes

uvicorn src.web.app:app --reload    # panel en http://127.0.0.1:8000
pytest -q
```

## En GitHub, sin tener el ordenador encendido

El cron vive en `.github/workflows/buscar.yml` y corre solo: novedades de lunes a
sábado, barrido completo los domingos. La web sigue siendo local (`uvicorn`), pero
eso ya no obliga a tener el ordenador encendido para *buscar*.

**Reparto de propiedad, y es lo importante de entender:** GitHub es el dueño de
los anuncios, precios, notas y cuota. **Tu máquina es la dueña de tus
valoraciones.** Están en el mismo fichero SQLite, así que si te bajaras la BD de
GitHub a lo bruto te borrarías los votos. Por eso hay una importación que respeta
lo tuyo, y por eso la BD **no** se commitea a `main`: vive en una rama aparte.

### Puesta en marcha (una vez)

1. Sube el repositorio a GitHub (privado, si prefieres).
2. En *Settings → Secrets and variables → Actions*, crea estos secretos. Los
   valores están en tu `.env`, que **nunca** se sube (está en `.gitignore`):

   `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASSWORD` `EMAIL_FROM` `EMAIL_TO`
   `IDEALISTA_API_KEY` `IDEALISTA_API_SECRET`

3. Asegúrate de que `data/pois.json` está commiteado (el `.gitignore` lo permite
   a propósito): sin él, GitHub no puede puntuar la localización.
4. Lánzalo a mano una vez desde la pestaña *Actions* para comprobar que va.

### Traerte los resultados para ver la web

```bash
git fetch origin datos
git show origin/datos:listings.db > data/remoto.db
python main.py --importar data/remoto.db     # NO toca tus valoraciones
uvicorn src.web.app:app --reload
```

La BD se guarda en una **rama huérfana de un solo commit** (`datos`), que se
reescribe entera en cada ejecución. Un commit diario de un binario haría crecer el
repositorio sin parar, y no hace falta: el histórico de precios ya está *dentro*
de la base de datos, que es donde tiene sentido.

## La cuota manda: dos ritmos, no uno

La cuota de la API de Idealista **no está publicada y no es de autoservicio**: te
la comunican al aprobarte el acceso. Ponla en `config.yaml` (`api.cuota_mensual`)
en cuanto la sepas — el 100 que hay ahí es una suposición. Lo único confirmado es
el tope de **50 resultados por página**.

Un barrido completo de la zona norte cuesta del orden de 4 a 8 páginas. Hacerlo a
diario serían 150–270 peticiones al mes, así que **el barrido diario completo no
cabe**. La salida es partir el trabajo, porque las dos señales no necesitan la
misma frecuencia:

```cron
# Diario: solo lo publicado esta semana. 1-2 páginas.
0 8 * * *   cd /ruta/home-search && .venv/bin/python main.py --source idealista

# Domingos: barrido completo. Es la ÚNICA forma de ver bajadas de precio, porque
# un piso publicado hace tres meses que baja hoy no sale en el filtro de novedades.
0 9 * * 0   cd /ruta/home-search && .venv/bin/python main.py --source idealista --completo
```

Eso son unas 88 peticiones al mes: novedades cada día, bajadas cada semana. El
guardarraíl (`src/cuota.py`) corta en seco antes de pasarse y reserva unas cuantas
peticiones intocables, porque quedarse a cero a mitad de mes te deja ciego hasta
el día 1.

`--mock-fase 2` hace que el mock devuelva "el día siguiente" (un anuncio baja de
precio, otro sube, uno desaparece y aparece uno nuevo). Es la única forma de
probar el histórico sin esperar días.

## Decisiones que conviene conocer antes de tocar nada

**Se guarda todo, se filtra al final.** Los criterios de `config.yaml` deciden
qué se *notifica*, no qué se *guarda*. Si filtrásemos antes de persistir, un
chalet de 1.290.000 € que baja a 1.090.000 € aparecería como novedad en vez de
como bajada — y la mediana de €/m² del barrio se calcularía sobre una muestra
recortada por nuestros propios criterios, que es justo la que no sirve para
saber si un precio es bueno.

**Se guarda antes de enviar.** Si el SMTP falla, el histórico ya está a salvo y
el evento sigue pendiente (`eventos.notificado = 0`): se reintenta mañana.

**La nota se recalcula entera en cada ejecución.** Depende de tres cosas que
cambian solas: el precio del anuncio, la mediana del barrio (que crece con cada
búsqueda) y los POIs. Cachearla invita a que se quede obsoleta en silencio.

**Un descuento enorme no es un chollo.** Un €/m² un 50% por debajo de la mediana
casi nunca es una ganga: es un bajo, un interior o una ruina. Por eso
`saturacion_pct` topa lo que puede sumar el descuento; sin ese tope, el ranking
se llena de basura barata.

**Los parques son polígonos, no puntos.** Medir al centroide del parque miente
por defecto; medir a su rectángulo envolvente miente por exceso (el Canal Bajo
es una franja diagonal cuyo rectángulo cubre kilómetros de calles). Se mide
contra el polígono real.

**La web escribe una sola cosa: tus valoraciones.** Todo lo demás lo abre en modo
solo lectura, garantizado por el driver de SQLite (`mode=ro`), no por buena fe.
El endpoint de valoración pide explícitamente una dependencia distinta
(`get_escritor`), así que si alguien añade un endpoint que muta anuncios o
precios, se ve en la revisión.

**Valorar no cambia la nota, y es a propósito.** Con veinte valoraciones no hay
estadística que valga, y ajustar los pesos automáticamente sobre esa muestra los
sobreajustaría a tus primeras impresiones. Lo que sí se guarda es la nota y su
desglose **tal y como estaban cuando juzgaste** el anuncio — sin esa foto, como
la nota se recalcula cada día, dentro de un mes sabrías que descartaste algo pero
no qué te estaba enseñando el sistema al hacerlo. `--calibrar` compara los dos
grupos y te dice si el scoring está capturando tu criterio o no; los pesos los
tocas tú.

**Sin coordenadas, los pesos se renormalizan.** Un anuncio sin lat/lon no puntúa
0 en localización: se reparte su peso entre las otras dimensiones. El precio a
pagar es que un 78 con tres dimensiones y un 78 con dos no son el mismo 78, así
que la ficha web siempre dice con cuántas se ha calculado.

**El margen de negociación es una heurística, no una predicción.** No hay datos
públicos de precio de cierre, así que nadie puede decirte cuánto vas a rebajar de
verdad. Lo que hace `src/scoring/negociacion.py` es agregar las tres señales que
mira un comprador con experiencia (cuánto lleva sin venderse, cuántas veces ya ha
bajado, cuánto se sale del precio de su barrio) y enseñarlas con su razonamiento
a la vista.

**"Días en mercado" son días desde que lo vimos NOSOTROS.** La API no da la fecha
de publicación de forma fiable, y llamarlo "días publicado" sería mentir: un
anuncio de hace un año que descubrimos ayer marcaría "1 día". Por eso en la web
pone "lo vemos publicado desde hace X días", que es lo único que sabemos.

## Estado

- Conector de Idealista: **escrito y testeado contra respuestas grabadas**
  (`tests/fixtures/`), pero nunca ejecutado contra la API real. Falta que lleguen
  las claves y comprobar que la respuesta de verdad coincide con la fixture.
- Detección de anuncios republicados con otro código: **pendiente**. Hoy, un piso
  retirado y vuelto a subir llega como novedad y su histórico de precios se
  resetea sin que nos enteremos.
