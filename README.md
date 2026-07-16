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

El cron vive en `.github/workflows/buscar.yml` y corre solo: un barrido completo
cada mañana. La web sigue siendo local (`uvicorn`), pero eso ya no obliga a tener
el ordenador encendido para *buscar*.

**Reparto de propiedad, y es lo importante de entender:** GitHub es el dueño de
los anuncios, precios, notas y cuota. **Tu máquina es la dueña de tus
valoraciones.** Están en el mismo fichero SQLite, así que si te bajaras la BD de
GitHub a lo bruto te borrarías los votos. Por eso hay una importación que respeta
lo tuyo, y por eso la BD **no** se commitea a `main`: vive en una rama aparte.

### ⚠️ El proveedor tiene la API apagada (comprobado el 16-07-2026)

Todo está montado y apuntando a la API real, pero **RapidAPI no deja llegar a
ella**: devuelve `405 The API provider has disabled request access to the API`.

No es un fallo de configuración, y está comprobado:

- Sale igual **sin mandar clave ninguna**, en **todas** las rutas (incluida `/`),
  con GET y con POST. La pasarela rechaza antes de mirar quién eres.
- Un host inventado devuelve `404 API doesn't exists`, así que la nuestra existe
  y lo que está apagado es el acceso.
- Pasa lo mismo en las **otras** APIs de Idealista de RapidAPI (`idealista7` de
  scraperium, `idealista2` de apidojo). No es de esta suscripción.
- El playground de RapidAPI falla con el mismo mensaje.

**No hay nada que tocar para cuando vuelva.** El cron corre cada mañana contra la
API real; mientras esté caída detecta el 405, sale con código **4**, no escribe
nada y el job **termina en verde con un aviso** (si no, tendrías un email de error
cada mañana por algo que no depende de ti). El día que la reactiven, el barrido
del día siguiente funciona solo.

Para ver el circuito entero sin gastar API —buscar, puntuar, mandar el email,
empujar la BD a la rama `datos`—, lánzalo a mano desde *Actions → Buscar vivienda
→ Run workflow* con **fuente = `mock`**.

### Puesta en marcha (una vez)

1. Sube el repositorio a GitHub (privado, si prefieres).
2. En *Settings → Secrets and variables → Actions*, crea estos secretos. Los
   valores están en tu `.env`, que **nunca** se sube (está en `.gitignore`):

   `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASSWORD` `EMAIL_FROM` `EMAIL_TO`

   Y `RAPIDAPI_KEY`, que es la que usa el conector real (con `mock` no se usa).

3. Asegúrate de que `data/pois.json` está commiteado (el `.gitignore` lo permite
   a propósito): sin él, GitHub no puede puntuar la localización.
4. Pon tu **día de corte** en `config.yaml` (`api.dia_corte`): el día del mes en
   que RapidAPI reinicia tu contador, que es el día en que te suscribiste, **no el
   1**. Está explicado abajo y equivocarse aquí es lo único de todo esto que puede
   costarte dinero.
5. Comprueba el circuito a mano desde *Actions → Buscar vivienda → Run workflow*
   con **fuente = `mock`**. Lánzalo con `mock_fase: 1` y luego con `2`: verás
   llegar el email de bajadas de precio, que es el circuito completo.

A partir de ahí el cron ya corre solo contra la API real cada mañana. No hay
ningún paso pendiente para "pasar a producción": en cuanto el proveedor reactive
la API, empieza a funcionar sin tocar nada.

### Cuando lleguen los primeros datos reales

Borra los datos de mentira, que si no se quedarían mezclados con los reales:

```bash
python main.py --purgar mock
```

Las medianas de €/m² ya están acotadas por fuente, así que el mock no las
contamina aunque se te olvide; pero seguiría apareciendo en la web.

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

## De dónde salen los anuncios: RapidAPI, no la API oficial

El conector real es `src/sources/rapidapi.py`, contra el proveedor **happyendpoint
(`idealista17`)** de RapidAPI. Conviene saber sobre qué está construido esto: **no
es la API oficial de Idealista**, es un revendedor que raspa el portal y lo sirve
en JSON. El formato de respuesta lo decide él y puede cambiar sin avisar. La API
oficial no es de autoservicio y no tenemos acceso aprobado.

Lo que hace que el diseño se sostenga es que el `propertyCode` que devuelve **sí
es el de Idealista** (coincide con el número de la URL del anuncio). De esa clave
cuelga el histórico de precios entero: si el proveedor empezara a inventarse los
IDs, cada día entraría todo como nuevo y el histórico dejaría de construirse *en
silencio*, sin ningún error. Hay un test que lo vigila.

`src/sources/idealista.py` es el conector de la API oficial. Está escrito pero
**nunca se ha ejecutado**, y sus fixtures están grabados contra un formato que no
es el que recibimos. Se conserva por si algún día llega el acceso; se usa con
`--source idealista-oficial`. Los dos guardan bajo el mismo nombre de fuente
(`idealista`) a propósito: son los mismos anuncios con el mismo código, así que el
histórico continuaría sin cortarse al cambiar de uno a otro.

### La cuota ya no manda: un solo barrido, diario y completo

El plan PRO son **15.500 peticiones al mes y 1 por segundo**. A 50 anuncios por
página (`result_count`, cuyo defecto son 30), un barrido completo de la zona norte
cuesta del orden de 20 a 40 peticiones: **cabe entero todos los días gastando
menos del 10% del mes**.

Esto sustituye a la estrategia de dos ritmos que hubo antes (novedades a diario,
catálogo completo los domingos), que existía porque se dio por supuesta una cuota
de 100 al mes. Además de sobrar complejidad, aquello tenía un problema de fondo:
**las bajadas de precio solo se ven en un barrido completo**, porque un piso
publicado hace tres meses que baja hoy no sale en el filtro de novedades. Con un
barrido semanal podías enterarte de una bajada con seis días de retraso. Ahora se
detectan al día siguiente, que es la razón de ser de todo esto.

El guardarraíl (`src/cuota.py`) sigue: corta en seco antes de pasarse y reserva
peticiones intocables, porque quedarse a cero a mitad de ciclo te deja ciego hasta
que renueve. Con `reserva: 500`, el corte efectivo son 15.000 y no 15.500: esos
500 son el margen. Y hay un tope de páginas por barrido (`api.paginas_max`) que no
es una optimización sino una red de seguridad: si un día se cae `precio_max` del
`config.yaml`, la búsqueda pasa de ~30 páginas a 5.571 y te funde el ciclo en una
sola ejecución.

### El ciclo de facturación no es el mes natural

**`api.dia_corte` es lo único de todo esto que puede costarte dinero si está mal.**
RapidAPI reinicia tu contador el día que te suscribiste, no el 1. Si te suscribiste
un día 20 y contáramos por mes natural, podrías gastar la cuota entera del 20 al 31
y **otra vez** del 1 al 19: el doble dentro del mismo ciclo de facturación, y ahí
llega el recargo. Por eso `Cuota.gastadas` cuenta desde el inicio del ciclo real.

Y hay que ser honesto con lo que este guardarraíl **no** puede prometer:

- Solo cuenta lo que ve, y solo ve **su** base de datos. El cron de GitHub y tu
  portátil tienen bases distintas: lo que gastes en local no lo sabe GitHub.
- No sabe lo que dice el contador de RapidAPI, que es el que factura.

Con ~30 peticiones diarias sobre un plan de 15.500 el margen es enorme, pero **la
única garantía de verdad contra el cobro está en el panel de RapidAPI**, quitando
el *overage*. Esto es un cinturón, no un contrato.

### Dos trampas de esta API que ya están resueltas

**El idioma.** El parámetro `language` viene por defecto en `en`, así que las
descripciones llegan **en inglés**. Las palabras clave de `scoring.texto` están en
español: contra texto en inglés no casa ninguna, y la nota de texto se quedaría
clavada en 50 para todos los anuncios sin dar ningún error. `config.yaml` fuerza
`idioma: es`.

**El orden de paginación.** Se pide `sort_order: oldest`, no `newest`. Con
`newest`, un anuncio publicado a mitad de la paginación empuja a todos los demás
una posición y nos saltaríamos uno. Con `oldest` lo nuevo cae al final y las
páginas que ya hemos pedido no se mueven.

Y una decisión deliberada: **no se filtra por `exterior` ni por barrio en la API**,
aunque el proveedor deje. Pedirle al servidor solo lo que cumple nuestros
criterios sesgaría la mediana de €/m² con nuestros propios gustos y dejaría sin
guardar el chalet de 1,3 M, que es justo el que queremos vigilar por si baja.

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
