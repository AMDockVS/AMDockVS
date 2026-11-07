# Plan: modos `vs` / `htpvs`, constructor de pasos y campaña HPC

Fecha: 2026-09-03
Origen: conversación de revisión de flujo y layout (2026-09-03).
Continúa `plan_normalizacion_y_htp_2026-08-29.md` §4 y §5, que ya fijaron los shards y el
embudo con punto de promoción. **Este plan no los reemplaza: los completa** con lo que allí no
existía — el modo como contrato de proyecto, la costura única de lectura/escritura, el
constructor de pasos, el tope de materialización, el bundle HPC y el diseño de UI.

## Qué añade sobre el plan anterior

| Ya decidido en 2026-08-29 (§4/§5) | Nuevo aquí |
|---|---|
| Shards en disco, `screening_shards` en db, identidad `(shard_id, record_index)` | El **modo** como parámetro inmutable del proyecto |
| Punto de promoción: solo los hits entran en `molecules` | **Tope duro** además del umbral en la promoción |
| Idempotencia por shard (`state='done'` no se recalcula) | `LigandStore`: **una costura**, dos backends, cero `if mode` disperso |
| "Separar el modo HTP" antes que adaptarlo todo | El **constructor de pasos** (pipeline puro), que sirve a los dos modos |
| Ray/HPC aplazado a después de 5.a–5.c | **Bundle HPC sin transporte** como primera entrega útil |
| "La UI de HTP muestra shards y agregados" (5.e) | El diseño concreto de esa UI |
| — | **Se fragmenta en el cluster**, no en local: viaja la fuente cruda, y `screening_dispatches` cubre el hueco hasta que vuelven los shards |

## Regla que gobierna este plan

> **El modo es un contrato de almacenamiento, no una preferencia.**
> Determina dónde viven los ligandos. Por eso es inmutable, y por eso su única consecuencia
> en el código es *qué objeto lee y escribe*, no *qué hace cada función*.

Corolario operativo, que es el criterio de revisión de todo lo de abajo:

> **`if mode == ...` no aparece en ningún sitio.** El modo no se pregunta: cada herramienta
> declara qué store consume (filas o shards) y el contrato lo garantiza la invariante de
> §0-bis. Un `if mode` indica que la costura está mal puesta.

---

## §0 — El modo como parámetro de proyecto  ⛔ superado por §0-bis (2026-09-03)

> Implementado y luego **retirado** el mismo día. Se conserva porque el mecanismo
> genérico que trajo a MolSuite (`projects.app_data`, `AppManifest.project_fields`,
> el diálogo que los pinta) sigue en pie y sigue siendo útil para otras apps; lo que
> se retiró es el uso que AMDock hacía de él. El porqué, en §0-bis.

### Decisión

```python
rt.create_project(name, folder, mode="vs")   # "vs" | "htpvs", default "vs"
rt.mode                                       # property de solo lectura
```

**Sin setter.** Un setter que solo puede llamarse una vez es una trampa: existe, luego alguien
lo llama, y hay que manejar "ya estaba puesto", "se puso con datos dentro", "se puso sin
proyecto activo" — tres errores para algo que no debería poder ocurrir. Como parámetro de
creación, esos estados no existen.

Persistido en el **registro de proyecto de MolSuite**, en una columna `projects.app_data` (JSON).
No en `AMDockConfiguration`: ese es un esquema de *preferencias*, y todo lo que vive ahí lo expone
la UI de ajustes como editable — justo lo que el modo no puede ser. Los proyectos existentes, con
`app_data` vacío, son `"vs"`.

El modo no es el único campo así, y por eso `app_data` es un dict y no una columna `mode`: es la
respuesta genérica a **lo que una app necesita saber de un proyecto antes de que exista**. Quién
pregunta y con qué widget lo declara el propio manifiesto de la app:

```python
# amdockvs/manifest.py
manifest = AppManifest(
    ...,
    project_fields=(
        AppSettingSpec(key="mode", default="vs", value_type="string",
                       choices=("vs", "htpvs"), label="Mode", description="..."),
    ),
)
```

El diálogo *New Project* de `ms_projects` lee `manifest.project_fields` de la app seleccionada y
pinta una fila por spec (combo si hay `choices`, checkbox si es `boolean`, spin si es numérico,
línea si no) — se repinta al cambiar de app en el combo. Las respuestas viajan como
`app_data` hasta `Project.app_data`, y `ProjectCatalogEditor` rellena defaults y **rechaza claves
que la app no declaró**. Solo en modo *create*: son contratos fijos, no preferencias, así que el
diálogo de edición no los ofrece. AMDock no toca la UI de proyectos: declara el campo y lee
`context.app_data["mode"]`.

### Tareas

| # | Archivo | Acción |
|---|---|---|
**En AMDock**

| # | Archivo | Acción |
|---|---|---|
| 0.a | ✅ `vocab.py` | `class ProjectMode` + `PROJECT_MODES` (constantes, no Enum) |
| 0.b | ✅ `manifest.py` | `project_fields` con el spec de `mode` |
| 0.c | ✅ `runtime.py` | `mode` en `create_project` / `create_or_open_project`, validado; viaja como `app_data` |
| 0.d | ✅ `runtime.py` | `on_project_activated` lee `context.app_data` — el **único** sitio que lo hace; ausente → `"vs"` |
| 0.e | ✅ `runtime.py` | `Runtime.mode` como property; sin setter público |

**En MolSuite** (`ms_flow`), el mecanismo genérico que faltaba:

| # | Archivo | Acción |
|---|---|---|
| 0.f | ✅ `core/app_settings.py` | `AppSettingSpec.choices` (un modo es una elección entre dos; sin esto el diálogo pinta un campo de texto) |
| 0.g | ✅ `core/apps.py` | `AppManifest.project_fields`, copiado también en `AppRegistry.register` (reconstruye el manifiesto: lo que no se copia se pierde en silencio) |
| 0.h | ✅ `core/database/master_models.py` + `master.py` | columna `projects.app_data`; migración v6 → v7 con `_ensure_columns` |
| 0.i | ✅ `core/project/repository.py` | `parse_app_data` / `serialize_app_data` (mismo patrón que `tags`; un JSON corrupto a mano no puede tumbar el listado) |
| 0.j | ✅ `context.py`, `manager.py`, `catalog.py`, `_api_impl/`, `runtime.py` | `app_data` de punta a punta, en crear y en cargar |
| 0.k | ✅ `core/project/catalog.py` | `_validated_app_data`: rellena defaults del manifiesto y rechaza claves no declaradas |

**En `ms_components`:**

| # | Archivo | Acción |
|---|---|---|
| 0.l | ✅ `ms_projects/browser.py` | `ProjectFormDialog` pinta `manifest.project_fields`, se repinta al cambiar de app, y devuelve `app_data`; solo en *create* |
| 0.m | ✅ `ms_projects/browser.py`, `widget.py` | los dos caminos de creación pasan `app_data` al backend |

`create_or_open_project` con un `mode` que contradice el guardado lanza `ValueError` en vez de
reinterpretar el proyecto en silencio.

**Check:** `test/test_project_mode.py` — 4 pruebas, pasan: ida y vuelta `htpvs`, proyecto sin `mode`
guardado → `"vs"`, modo desconocido rechazado, y `create_or_open_project` que se niega a
reinterpretar. Suite de AMDock: 299 pasan. Suite de `ms_flow`: 331 pasan (dos aserciones de versión
de esquema actualizadas a 7). La migración corrió sobre `~/.molsuite/projects.db` real: la columna
está y los proyectos viejos leen `vs`.

---

## §0-bis — El modo se deriva de los datos, se decide en la importación ✅ hecho (2026-09-03)

### Por qué se retiró §0

La bandera de proyecto solo compraba **una** cosa: la garantía de que un proyecto no acaba con
dos bibliotecas de cribado (filas *y* shards), porque ningún scope puede nombrar las dos. Pero
esa garantía no necesita una bandera: **`screening_shards` no vacío ya la contesta**, y la
contesta sin pedirle al usuario una decisión de almacenamiento *antes de que exista un solo
dato*, que es cuando menos información tiene para tomarla.

Lo que sí hacía falta era mover la decisión al momento en que es informada — la importación,
donde se sabe cuántos gigabytes entran — y convertir la garantía en una comprobación real.

### La invariante

> **Una biblioteca de cribado por proyecto, en un solo store.**
> Las moléculas curadas (receptores, ligandos de referencia, un set de entrenamiento QSAR) son
> siempre filas y conviven con una biblioteca en shards sin ambigüedad. La biblioteca de
> cribado — `is_ligand`, `usage_class='general'`, `small_molecule` — es filas **o** shards.

Eso es exactamente lo que evita que un scope tenga que unir db y archivos, y lo que hace que
las salidas nunca haya que manejarlas por separado.

### Tareas

| # | Archivo | Acción |
|---|---|---|
| 0bis.a | ✅ `molecules/store.py` | `has_shards` / `library_kind` (derivación), `general_ligand_count`, `check_library_target` (la invariante, comprobada) |
| 0bis.b | ✅ `runtime.py` | fuera `mode=` de `create_project`/`create_or_open_project`, `_validate_mode`, `_project_mode`; `rt.mode` = `library_kind(project_db)`; `+ rt.general_ligand_count()` para la UI |
| 0bis.c | ✅ `manifest.py` | sin `project_fields` |
| 0bis.d | ✅ `io/api.py` | `check_library_target` en `load_ligands` (solo ligandos generales small-molecule) y en `shard_ligands` |
| 0bis.e | ✅ `io/jobs.py` | `SHARD_SUGGEST_BYTES` (200 MB, `AMDOCK_SHARD_SUGGEST_BYTES`) — el umbral con el que la UI sugiere |
| 0bis.f | ✅ `ui/tools/import_workspace.py` | casilla *"Screening library: keep on disk as shards"*, sugerencia automática por tamaño, aviso duro si se insiste en filas, casilla bloqueada si el proyecto ya tiene biblioteca, y ruteo a `shard_ligands` solo del grupo small-molecule |
| 0bis.g | ✅ `ui/shell/job_feedback.py`, `view_coordinator.py` | la insignia se refresca al acabar cada job (una importación de shards convierte el proyecto en campaña) y su tooltip dice de dónde sale |

### Decisiones tomadas al implementar

- **`ProjectMode` sigue existiendo** como vocabulario ("vs"/"htpvs" son las etiquetas de la
  insignia), pero ya no se persiste ni se valida en ningún sitio: es el valor de retorno de
  `library_kind`, no un estado.
- **`store_from_config` siempre devuelve filas.** Las herramientas de fila leen filas; las de
  shard construyen su `ShardStore` explícitamente. No hay despacho por modo. `ligand_store()`,
  que sí lo hacía, se borró (no tenía llamantes).
- **El aviso por tamaño mide bytes en disco, no registros** (`ponytail`: un `stat()` por
  archivo frente a contar moléculas). El techo: un `.smi` de 200 MB son ~4 M de moléculas y un
  `.sdf` de 200 MB unas 60 k — el umbral acierta en el caso que importa (evitar millones de
  filas) y en el otro solo sugiere de más.
- **La UI no toca `project_db`.** Pregunta `rt.mode` y `rt.general_ligand_count()`; el test de
  frontera `test_ui_api_boundary.py` lo obliga.

**Check:** `test/test_project_mode.py` — 6 pruebas: la derivación (`vs` → `htpvs` al aparecer un
shard), que sobrevive a cerrar/abrir sin persistir nada, que `check_library_target` rechaza la
segunda biblioteca, y dos de UI (el diálogo rutea small-molecule a `shard_ligands` y proteína a
`load_ligands`; la casilla queda bloqueada si el proyecto ya está en shards). Suite completa:
**327 pasan, 2 se saltan**.

---

## §1 — Constructor de pasos (`chemistry/pipeline.py`) ✅

### La idea

> *"me gusta la idea de tener una especie de constructor de procesos configurando pasos."*

Un módulo puro nuevo, sin db ni rutas, que compone los núcleos que ya existen:

```python
LIGAND_STEPS = {                 # nombre -> callable(mols, **params) -> mols
    "standardize": ..., "protonate": ..., "generate_3d": ...,
    "minimize": ..., "conformers": ...,
}

def run_pipeline(mols: list, steps: list[tuple[str, dict]]) -> list:
    for name, params in steps:
        mols = LIGAND_STEPS[name](mols, **params)
    return mols
```

### Restricción no negociable: lote → lote

**No** molécula → molécula. Sale del código actual: `chemistry/service.py:176-190` hace una
pre-pasada de lote para `protonate`, porque Dimorphite y OpenBabel operan sobre un conjunto,
no sobre una molécula. Un pipeline per-molécula rompería la protonación.

### Por qué sirve a los dos modos

- `htpvs`: los pasos van fusionados en un job porque el usuario no busca granularidad.
- `vs`: el auto-encadenado (generar 3D → preparar sin viaje a Build) pasa a ser **un** job en
  lugar de N encadenados.

Que sirva a los dos sin condicional es la señal de que la abstracción está en el sitio correcto.

### Tareas

| # | Archivo | Acción |
|---|---|---|
| 1.a | ✅ `chemistry/pipeline.py` (nuevo) | `LIGAND_STEPS` + `run_pipeline` + `normalize_steps` + `kwargs_for`. Puro: sin `Path`, sin db, sin `output_dir` |
| 1.b | ✅ `chemistry/service.py` | `transform_ligand_rows(operations=...)` acepta lista de pasos o un `str` suelto; carga el lote entero, llama a `run_pipeline` una vez y escribe **un** artefacto por ligando |
| 1.c | ✅ `chemistry/jobs.py`, `chemistry/api.py` | `LigandChemistryJobParams.operation: str \| list`; nuevo `runtime.chemistry.run_ligand_pipeline([...])`; `protonate_ligands()` etc. intactos |
| 1.d | ✅ `docking/programs.py`, `docking/api.py` | `requires_ligand_3d: bool = True` en `DockingProgramSpec`; el gate lo lee (= tareas 5.a y 5.b) |
| 1.e | ✅ `constants.py`, `chemistry/jobs.py`, `docking/preparation_jobs.py`, `io/jobs.py` | `OUTPUT_FLUSH_EVERY` (env `AMDOCK_OUTPUT_FLUSH_EVERY`, def. 16) sustituye a `output_flush_every=1` y absorbe `IMPORT_OUTPUT_FLUSH_EVERY` |

### Decisiones tomadas al implementar

- **La posición es la identidad.** `run_pipeline` devuelve una lista de la misma longitud y
  orden que la de entrada; una molécula que falla deja su **excepción en su hueco** y los pasos
  siguientes la dejan pasar. Sin eso el llamador no puede volver a casar resultados con filas, y
  una molécula rota tiraría el lote entero.
- **Cada paso toma solo los parámetros que declara** (`kwargs_for`, por firma). Eso borró el
  `if/elif` de ~200 líneas que en `service.py` elegía claves a mano por operación. Un parámetro
  desconocido se ignora, igual que antes.
- **Un artefacto, no N.** standardize+protonate+3D escribía tres ficheros por ligando (un job
  cada uno); ahora escribe uno. El nombre y los `model_rows` los decide el **último** paso; el
  estado (`has_hs`/`is_minimized`) se pliega sobre todos.

**Checks (en `test/test_chemistry_pipeline.py`, `test/test_docking_preconditions.py`):**
`run_pipeline` == las tres llamadas encadenadas a mano; el hueco de una molécula rota se
conserva; un lote multi-paso escribe un fichero por ligando; una fila ilegible no tumba a las
demás; un spec con `requires_ligand_3d=False` da `ready` sin 3D.

---

## §2 — `LigandStore`, solo con `DbStore` ✅ hecho (2026-09-03)

### La costura

Las diferencias entre modos, enumeradas una a una, son todas la misma pregunta:

| Etapa | Qué cambia realmente |
|---|---|
| Importación | dónde escribo (filas / shards) |
| Preprocesamiento | de dónde leo (scope db / lista de ficheros) |
| Diversidad | de dónde leo (lotes db / shards) |
| Preparación | de dónde leo |
| Docking | de dónde leo |
| Análisis | cuándo escribo (siempre / al pasar el umbral) |

Seis viñetas, **una** costura. Y como los receptores se materializan en los dos modos, la
costura es **solo de ligandos**.

```python
class LigandStore(Protocol):
    def iter_rows(self, spec: QuerySpec, *, batch_size: int = 128) -> Iterator[dict]
    def count(self, spec: QuerySpec) -> int
    def materialize(self, rows) -> list[dict]   # no-op en DbStore: ya está materializado
```

`ingest()` se quedó fuera: la importación no escribe por una llamada, escribe por los
`graph_sink` de ms_flow. No hay costura invocable que envolver hasta que §3 la necesite.

`materialize()` como no-op en `vs` es lo que permite que el código de análisis sea idéntico en
los dos modos.

### Esta fase NO introduce `ShardStore`

Deliberado. Si entran los dos backends a la vez y algo se rompe, no se sabe cuál fue. Con solo
`DbStore` —que envuelve lo que ya existe, sin comportamiento nuevo— el criterio de éxito es
trivial y mecánico.

### Tareas

| # | Archivo | Acción |
|---|---|---|
| 2.a ✅ | `molecules/store.py` (nuevo) | `LigandStore` (Protocol) + `DbStore` (= `db_pages`/`db_count` sobre el `scope_spec` del tool) + `ligand_store(project_db, mode=)` + `store_from_config(config)` + `as_store(source)` |
| 2.b ✅ | `runtime.py::on_project_activated` | **La única ramificación por modo del código.** Instancia el store y lo expone como `runtime.ligands_store`; se anula en `close_project` |
| 2.c ✅ | `chemistry/`, `selection/`, `qsar/` | `iter_ligand_rows`, `scope_molecule_rows`/`scope_molecule_count` y `_iter_descriptor_batches` leen del store; los tres jobs resuelven el suyo con `store_from_config(config_map)` |

### Decisiones tomadas al implementar

- **El modo llega al worker gratis.** `ms_flow/_api_impl/job_api.py` ya inyecta `project_context`
  en el config de todo job, y §0 le puso `app_data`. `store_from_config` lo lee de ahí: ningún
  parámetro de job transporta el modo.
- **`as_store(source)`: se acepta un store o un `project_db` a secas.** Los feeds se llaman
  también desde la UI (`selection/api.py` para el preview inline), que no tiene config. Un
  `project_db` significa `vs`, que es exactamente lo que ya hacía. Cero cambios en esos sitios.
- **Fuera de la costura, a propósito:** `docking/repository.py` (`iter_entity_rows`,
  `count_entity_rows`, `_iter_entity_ids`) y `MoleculesAPI.stream/stream_ids/count` siguen en
  `db_pages`. Son genéricos sobre ligandos **y** receptores e hidratan por lote contra la db; y
  en `htpvs` el docking no lee la librería local, porque §6 le manda un bundle. Meterlos ahora
  sería costura sin usuario.
- **`ligand_store` no rompe todavía con `htpvs`.** Devuelve `DbStore` en los dos modos, porque
  hoy nada fragmenta: la importación sigue escribiendo filas. Que reventara al abrir el proyecto
  haría inusable el selector de modo de §0 hasta que llegue §3. La rama existe para que §3
  cambie **una** línea.

**Check:** `test/test_ligand_store.py` — un `FakeStore` sin base de datos detrás alimenta los
tres feeds, y un `project_db` pelado sigue funcionando. La suite entera pasa sin cambios (315
tests): en modo `vs` esta fase no es observable.

---

## §3 — `ShardStore` y el punto donde se fragmenta ✅ hecho (2026-09-03)

Implementa sobre lo ya decidido en §4/§5 del plan anterior: shards con offsets, tabla
`screening_shards`, identidad `(shard_id, record_index)`, idempotencia por `state`.

### Dónde se parte la librería

**No siempre en local.** Mover un fichero de 40 GB por la red cuesta menos que mover los 4.000
shards en que se parte: una conexión contra cuatro mil. Así que en una campaña con HPC lo que
viaja es la **fuente cruda**, y el `ShardStore` remoto la fragmenta allí, junto a los CPUs que van
a leerla.

De eso se sigue lo que de verdad importa para la UI: **el número de shards no se conoce antes de
despachar.** No se puede preconstruir la tabla, ni con filas en `pending`, ni con un conteo
estimado. `screening_shards` empieza vacía y se puebla con la retroalimentación.

Dos orígenes, un mismo esquema:

| Campaña | Quién fragmenta | Cuándo aparecen las filas de `screening_shards` |
|---|---|---|
| `htpvs` local | el `ShardStore` local, en el import | al importar |
| `htpvs` + HPC | el bundle, en el cluster | al ingerir la retroalimentación |

### La tabla de envíos

Lo que sí existe desde el momento de despachar es **lo que mandaste**. Eso va en su propia tabla,
`screening_dispatches`, y **no** dentro de `screening_shards`: son cosas distintas por los tres
criterios que importan.

| | Envíos | Shards |
|---|---|---|
| cardinalidad | decenas | miles |
| vida | la campaña acaba | el inventario queda |
| pregunta que responde | ¿dónde va mi trabajo? | ¿qué tengo? |

Meterlas en una tabla obliga a filas padre con `n_shards` y `n_records` nulos — columnas nulas en
el inventario para ahorrarse un widget. No se hace.

Una fila por fuente despachada: `path`, `bytes`, `submitted_at`, `state`
(`uploaded`/`running`/`returned`/`failed`), `n_shards_returned`. Es tabla de base de datos y no
estado en memoria porque es lo que permite **reanudar una ingesta a medias**.

### El `ShardStore`

| Método | Implementación |
|---|---|
| `ingest` | Parte los ficheros en shards de N moléculas aplicando el prefiltro al vuelo. **No toca `molecules`.** Registra una fila por shard en `screening_shards` |
| `iter_batches` | Un shard por lote; feed = `db_pages` sobre `screening_shards` con `state='pending'` |
| `count` | `db_count` sobre `screening_shards`, sumando `n_records`. Sin abrir ficheros |
| `materialize` | Promoción: inserta filas en `molecules` por el importador normal (5.d del plan anterior) |

Más el adaptador hermano de `transform_ligand_rows`, sin `entity_id`, sin `extra_data`, sin
updates de grafo:

```python
def transform_ligand_shard(shard_in: Path, shard_out: Path, steps) -> None:
    write_sdf(shard_out, run_pipeline(read_sdf(shard_in), steps))
```

Un shard entra, un shard sale, 1 fichero/CPU. Lo corre igual el job local que el script del
cluster: es la misma función pura, y esa es toda la razón de que exista.

### Tareas

| # | Archivo | Acción |
|---|---|---|
| 3.a | `models/` + repository | Tabla `screening_shards` con `QuerySpec` (tarea 5.a del plan anterior) |
| 3.b | `models/` + repository | Tabla `screening_dispatches` con `QuerySpec` |
| 3.c | `molecules/store.py` | `ShardStore` con los cuatro métodos |
| 3.d | `chemistry/shards.py` (nuevo) | `transform_ligand_shard` sobre `run_pipeline` |
| 3.e | `io/jobs.py` | En `htpvs` local, el import no materializa: escribe shards y registra filas de shard |
| 3.f | — | Idempotencia: un shard `done` no se recalcula (tarea 5.c del plan anterior) |

Todas hechas. `models/screening.py` (3.a/3.b), `molecules/store.py::ShardStore` +
`shard_scope_spec` (3.c), `chemistry/shards.py::transform_ligand_shard` (3.d), `io/shards.py` +
`shard_ligands_job` + `LoaderAPI.shard_ligands` (3.e), y el par
`shard_scope_spec(state='pending')` + upsert sobre `(source, shard_index)` (3.f).

**Check:** proyecto `htpvs` local, 3 SDF de entrada, pipeline de 3 pasos → verificar shards de
salida, conteos y que `molecules` sigue vacía. Y en el camino HPC: tras despachar,
`screening_dispatches` tiene 3 filas y `screening_shards` **cero** — el vacío es correcto.

La mitad local del check pasa en `test/e2e/test_htpvs_shard_pipeline.py`: 3 SDF × 20 moléculas con
`shard_size=8` → 9 shards en disco, `record_count() == 60`, `molecules` vacía; luego
`run_shard_pipeline(["standardize", "generate_3d", "minimize"])` deja las 9 filas en `done` con los
ficheros bajo `standardize+generate_3d+minimize/`, y una segunda pasada no encuentra nada
`pending`. La mitad HPC depende de §6.

### Decisiones tomadas al implementar

- **El import ya fragmentaba.** `stream_import_payload_batches` parte el fichero en *tramos de
  bytes* y el worker parsea; para shardear solo cambia el worker: en vez de convertir el tramo en
  filas, lo vuelve a escribir a fichero. El shard es una copia de bytes salvo que haya prefiltro
  activo — esa es la única razón por la que un shard llega a parsear moléculas.
- **`ingest` no es un método del store.** El import escribe por `table_sink`, no por una llamada,
  así que el Protocol de §2 no lo lista; el shardeo es un job (`shard_ligands_job`), no un método.
- **`record_count` suma en Python**, no en SQL: `ms_flow.query` no tiene `db_sum` y el inventario
  son miles de filas, no millones. Techo anotado con `ponytail:` en el propio método.
- **`ligand_store()` sigue devolviendo `DbStore` en los dos modos.** Cambiarlo a `ShardStore` es
  el interruptor de §4: hasta que exista la materialización con tope, un proyecto `htpvs` que
  devolviera `ShardStore` no podría ni abrir la vista de Ligands.
- **`ShardStore.materialize` lanzaba `NotImplementedError`** apuntando a §4, en vez de existir a
  medias; §4 lo implementó (compuerta con umbral y tope).
- **`screening_dispatches` existe pero nadie la escribe** hasta §6. La tabla es de §3 porque el
  esquema es de §3; el productor es de §6.
- **La UNIQUE `(source, shard_index)` es obligatoria**, no decorativa: sin ella el `write_mode
  ="upsert"` del sink falla en SQLite ("ON CONFLICT clause does not match any ... constraint").
- `RESOURCE_SHARDS` = `data/shards` se resuelve del manifiesto contra la raíz del proyecto, así
  que los proyectos ya creados también lo tienen.

---

## §3-bis — El shard es un contenedor `.mshard`, no un tramo de bytes ✅ hecho (2026-09-04)

§3 dejó el shard como *copia de un tramo de bytes* del fichero original, con su extensión
(`_0000.sdf`). Funciona, pero no resuelve el problema por el que existen los shards.

### Qué problema resuelven de verdad

No es SMILES. Un `.smi` de un millón de líneas ya es un fichero compacto y barato de mover. El
problema son los formatos **no apilables**: PDB y sobre todo PDBQT, donde cada molécula tiende a
ser un fichero. Cientos de miles de ficheros de 3 KB es lo que mata un filesystem de HPC: inodos,
`rsync`, cuota. El shard es entonces **un contenedor fiable, compacto y de acceso indexado** —
no un fichero que deba seguir siendo legible por RDKit u Open Babel. No conserva la identidad del
formato de origen y no debe intentarlo.

### El contenedor

`ms_flow.core.data.shard` (`.mshard`): cabecera de 58 B + registros de ancho variable + tabla de
offsets + huella BLAKE2b-256 al pie; lectura por `mmap` con vistas sin copia; commit atómico
(`.tmp` + `os.replace` + `fsync` del directorio).

Lo que importa aguas arriba:

- **Direccionado por id lógico**, no por posición: `base_id .. base_id+slot_count-1`. Un registro
  descartado por el prefiltro deja un **hueco** (`NOT_PRESENT`), no renumera a los siguientes. Los
  ids de entrada sobreviven a toda la cadena de transformaciones.
- **El formato vive en la cabecera** (`PayloadKind`), no en el nombre. Se acabó el `_SUFFIX` por
  extensión de origen: todo shard es `.mshard`.
- **Tope de registros**: `MAX_RECORDS = 65535` por shard, verificado al construir el escritor.

### Tamaño: registros primero, bytes como tope

Un lote de trabajo escribe **un** shard: hay exactamente un escritor por fichero y no existe el
problema de escritura concurrente. Para que un lote no se derrame en dos ficheros, el corte
primario tiene que ser el que define el lote — **el número de registros**.

| Knob | Valor | Papel |
|---|---|---|
| `DEFAULT_SHARD_SIZE` | 1 000 | corte primario para SDF/PDB/PDBQT |
| `DEFAULT_SMILES_SHARD_SIZE` | `MAX_RECORDS` | SMILES no tiene el problema; que llene el shard |
| `SHARD_MAX_BYTES` | 256 MiB | tope de seguridad, no criterio de diseño |

1 000 y no 10 000 por **granularidad de fallo**: lo que se reintenta tras un crash es un shard
entero.

El feed del import corta por `bytes OR registros`, lo primero que llegue; los valores del llamante
deciden cuál manda. La importación a filas sigue cortando por bytes (`ramp` de arranque incluido);
la de shards pasa `chunk_bytes=SHARD_MAX_BYTES, ramp=False` para que mande el conteo. `ramp` se
propaga hasta `estimate_import_chunks` porque el `total_chunks` declarado es un **suelo**: si no
coincide con el feed real, el job no termina nunca.

### Tareas

| # | Archivo | Acción |
|---|---|---|
| 3bis.a | `ms_flow/core/data/shard.py` | Contenedor + `tests/test_shard_container.py` (9 tests) |
| 3bis.b | `io/parsers/readers.py` | `iter_record_span` / `read_record_span`: leer N registros, en streaming |
| 3bis.c | `io/loaders.py` | Corte por bytes **o** registros; `estimate_record_chunks(..., ramp=)` |
| 3bis.d | `io/shards.py` | Escribe `.mshard` (huecos donde el prefiltro descarta — **sustituido por §3-ter**) |
| 3bis.e | `chemistry/shards.py` | `read_shard`/`write_shard`; `transform_ligand_shard` preserva `base_id`/`slot_count` |
| 3bis.f | `io/api.py` + `io/jobs.py` | `shard_size` por formato, clamp a `MAX_RECORDS`, `total_chunks` sin `ramp` |

**Check:** `test/e2e/test_htpvs_shard_pipeline.py` lee ahora **por el contenedor**
(`read_shard`), no con `SDMolSupplier` — con la lectura anterior el test pasaba por accidente,
porque el parser de SDF se resincroniza en el primer `$$$$` y se saltaba la cabecera binaria.
Verifica que los ids de salida son un subconjunto de los de entrada.

### Decisiones tomadas al implementar

- **Los proyectos ya shardeados hay que reimportarlos.** No hay migración: los shards viejos son
  tramos SDF crudos. `p1` incluido.
- **No se anota por qué se cerró un shard** (`closed_by="bytes"`). Se contempló y se descartó: el
  tope de bytes es una red de seguridad, y una metadata que nadie lee es peso muerto. Se añade el
  día que alguien pregunte por qué un shard trae 300 registros.
- **`original_size` se queda en el registro** aunque hoy `Codec.NONE` lo hace igual a
  `stored_size`: está en el formato en disco, quitarlo cuesta una v2.

---

## §3-ter — La cola de shards: los workers filtran, el padre corta ✅ hecho (2026-09-04)

### El problema que cierra

Con el worker escribiendo su propio shard, el tamaño del shard lo decidía el *span* del fichero,
no los supervivientes: con un prefiltro activo un shard de 1000 huecos traía 880 registros, y con
13 spans salían 13 shards desiguales. Empaquetar denso en el worker es imposible: para saber en
qué id empieza mi shard necesito cuántos sobrevivieron en los anteriores, que es justo lo que
ningún worker sabe.

### El reparto

- **Worker** (`filter_ligand_span`): parsea su span sólo si hay prefiltro, aplica el filtro y
  devuelve los supervivientes en crudo `[(source_index, raw), ...]`. Sin estado, sin disco.
- **Padre** (`ShardQueueWriter`): una cola única, en el hilo de resultados del executor. Acumula
  por `(fichero, formato)`, corta cada `shard_size` registros, escribe el `.mshard` y su fila de
  `screening_shards`. Sustituye al `output_spec` del job (`result_handler_factory=`): un shard es
  fichero **y** fila, y sólo este lado del proceso decide qué registro va con cuál.

Lo que se gana: exactamente `shard_size` registros por shard (sólo el último de cada fichero es
parcial), ids correlativos en toda la importación y un único escritor de disco. Lo que se paga:
cada registro superviviente viaja de vuelta del worker al padre.

### No es idempotente, por decisión

Un span reintentado se vuelve a filtrar y aterriza donde esté la cola en ese momento, así que los
límites de shard cambian entre ejecuciones y una reimportación **añade** en vez de hacer upsert
sobre `(source, shard_index)`. Acordado: una importación fallida se relanza desde cero, no se
reanuda. Reanudar exigiría que la cola persistiera recibos `span -> shard`, que es exactamente el
registro por chunk que MF dejó de llevar (ver `mf-domain-idempotency-contract`).

### Tareas

| # | Archivo | Acción |
|---|---|---|
| 3ter.a | `io/shards.py` | `write_ligand_shard` → `filter_ligand_span` + `ShardQueueWriter` |
| 3ter.b | `io/jobs.py` | `result_handler_factory=shard_queue_writer`; el chunk pasa a ser unidad de parseo |
| 3ter.c | `io/api.py` | `result_handler_kwargs`: `project_db`, `shard_dir`, `shard_size` |

**Check:** `test/test_shard_queue.py` — trozos de 3/6/1 registros producen shards de 4/4/2 con
ids `0..9`; los `source_index` originales sobreviven en `metadata["source_indices"]` aunque los
ids se cierren; dos ficheros nunca comparten shard.

---

## §4 — Sink de materialización con tope ✅ hecho (2026-09-03)

### El riesgo que cierra

Del planteamiento original: *"aquellos que pasen de <-8.5 kcal/mol ... aquí no hay control del
número final"*. Un receptor que puntúa generoso, o una librería sesgada, y se materializan
millones de filas en SQLite a la hora 30 de una corrida, sin nadie mirando y sin forma de
pararlo salvo matar el proceso.

### La regla

> **Umbral Y tope, lo que ate primero.** El umbral es el criterio científico; el tope es lo que
> hace que la campaña termine.

Es una variante de los tres sinks que ya existen: predicado + contador. La materialización es
incremental — se promueve conforme se acumula, no al final.

### Qué se trae de vuelta

La línea no es "ligero vs completo" sino **qué es irrecuperable**:

| | Contenido | Por qué |
|---|---|---|
| **ligero** (defecto) | geometría de las poses + puntuaciones + SMILES | Lo que no se puede recomputar, más la identidad que hace recomputable el resto |
| **completo** | lo anterior + pdbqt preparado, confórmeros, descriptores | Solo cuando recomputar en local es más lento que transferir |

El SMILES no es opcional aunque parezca "solo un identificador más". Una fila en `ligands` con
únicamente el `id` no se dibuja en 2D, no se busca, no se exporta y no se deduplica — es decir,
no sirve para lo único que justifica `htpvs`, que es mirar los hits. Con el SMILES (~60 bytes)
la fila es renderizable, y descriptores, huellas y 3D salen en local en segundos para 1.000
moléculas.

Por eso el defecto es ligero: "completo" solo gana cuando transferir sale más barato que
recomputar, y con un top-N eso casi nunca pasa. Va como flag en la llamada de ingesta, sin modo
ni UI propios hasta que un caso real lo pida.

Y el tope importa más en modo completo: con umbral y sin tope, una diana generosa vuelve a llenar
la base de datos que `htpvs` existe para mantener vacía — y ahí, además, con artefactos.

### Tareas

| # | Archivo | Acción |
|---|---|---|
| 4.a | `htp/screening.py` | Sink con `predicate` (umbral sobre la métrica) y `cap` (top-N duro) |
| 4.b | `docking/api.py` | El run en `htpvs` escribe por ese sink |
| 4.c | `htp/bundle.py` | Flag `payload="light"\|"full"` en la ingesta; `light` por defecto |
| 4.d | UI/config | El tope es obligatorio en el formulario de campaña, no opcional |

**Check:** sink con predicado que deja pasar todo y `cap=10` → exactamente 10 filas
materializadas. Y una ingesta `light` deja filas con SMILES y pose, sin descriptores.

Ambas mitades pasan en `test/test_hit_materialization.py`, incluida la parte incremental: dos
llamadas de 40 filas con la misma `HitGate(cap=10)` materializan 10 en total, no 10 por llamada.

### Decisiones tomadas al implementar

- **La compuerta es pura y vive en `htp/materialize.py`** (`HitGate`), separada de quien escribe
  (`ingest_hits`). El tope es estado — cuenta —, así que la misma instancia se pasa a todas las
  ingestas de una campaña; es lo que hace que el tope sea del *run* y no de la llamada.
- **No es un `output_spec`.** Los sinks de MF son declarativos y no admiten un predicado del
  usuario, y aquí no hace falta: por construcción llegan como mucho `cap` filas, y la ingesta
  corre en el proceso que recibe, no en un worker. `ponytail:` anotado en `ingest_hits`.
- **`cap` primeros que pasan el umbral, no top-N ordenado.** Un top-N real exige ordenar la
  corrida entera, que es justamente el "acumular todo" que §4 existe para evitar. Las filas
  materializadas son ≤ `cap`: ordénalas después.
- **En docking la compuerta se parte en dos** porque el sink no es un sitio donde enchufar nada:
  el *umbral* va en el worker (`DockingVinaJobSpec.run_chunk`, sin estado, las no-hits ni
  vuelven) y el *tope* va en el feed (`build_chunks` cuenta las filas ya escritas de la corrida y
  deja de repartir trabajo). El tope del feed es aproximado por los chunks en vuelo — acota la
  campaña; el techo exacto es la compuerta de ingesta.
- **`docking.run(hit_cap=...)` exige `hit_threshold`**: un tope sin umbral solo trunca la
  librería en el orden del feed, que no es un criterio.
- **Las no-hits no dejan fila**, así que un re-run con `skip_existing` las vuelve a dockear. Con
  la campaña acotada no importa; si reanudar una corrida capada llega a importar, hace falta una
  tabla de "ya cribadas". Anotado en el código.
- **4.c: el flag `payload` está en `ingest_hits` / `ShardStore(payload=...)`, no en
  `htp/bundle.py`**, que es de §6 y aún no existe. `light` (defecto) = SMILES + pose + score;
  `full` añade descriptores a las columnas y confórmeros a `molecule_models`.
- **4.d está a medias, y a propósito**: el tope es obligatorio donde hoy se puede hacer cumplir —
  `HitGate` revienta con `cap<=0` y `ShardStore.materialize` sin `gate` — pero la pantalla de
  campaña no existe todavía, así que el campo del formulario es UI pendiente.

---

## §5 — Precondiciones declarativas (habilita DiffDock / AF3)

Pequeño pero bloquea a los demás: hoy el gate `has_3d` está cableado en
`docking/api.py:328-345`, incondicional, ignorando el spec del programa. Eso hace imposible
DiffDock (le basta el SMILES) y AF3 (sin preparación de receptor).

`DockingProgramSpec` (`docking/programs.py:10-36`) ya tiene `receptor_types`, `ligand_types`,
`experiment_kinds`, `requires_binding_site`, `requires_ligand_preparation`,
`requires_receptor_preparation` y `supports()`. Falta un campo y quitar el cableado.

### Tareas

| # | Archivo | Acción |
|---|---|---|
| 5.a | ✅ `docking/programs.py` | `requires_ligand_3d: bool = True` (hecho en §1) |
| 5.b | ✅ `docking/api.py` | El gate lee el spec en lugar de asumir `has_3d` (hecho en §1) |
| 5.c | `docking/programs.py` | Registrar los forks de Vina (QuickVina2, Q-Vina-W, GWOVina, DVina/DVinaRF20): mismo `preparation_engine="ad4"`, son datos |
| 5.d | `docking/programs.py` + `protocol_editor.py:152` | `crossdocking` en `experiment_kinds` y en el combo |

**Check:** un spec con `requires_ligand_3d=False` permite `prepare`/`run` sobre ligandos sin 3D.

---

## §6 — Bundle HPC (sin transporte)

### Lo que se entrega

Un directorio autocontenido: **las fuentes crudas** + config + script de submit del job array. El
usuario lo copia al cluster. La ingesta lee un directorio de resultados que el usuario trae de
vuelta.

Fuentes, no shards (§3): mover un fichero grande cuesta menos que mover los miles en que se parte,
así que **el script fragmenta en el cluster** como primera etapa, y el job array se dimensiona
después, con el número de shards ya conocido allí. El bundle no puede declarar cuántas tareas
tiene: las cuenta el propio script.

**Sin SSH, sin credenciales, sin polling remoto.** Eso es esencialmente lo que hace VirtualFlow,
y deja estable el formato de bundle, que es la parte que de verdad cuesta acertar. Si más
adelante el copy-paste molesta, el transporte se añade encima de un formato ya probado.

### Por qué no se reutiliza el sistema de jobs para la pata remota

`AMDOCKVS_LOCAL_EXECUTORS = ("thread", "compute")` (`constants.py:95`) y **cada job la declara
explícitamente** — `supported_executors=AMDOCKVS_LOCAL_EXECUTORS` aparece en ~20 sitios
(`docking/jobs.py:668,790,884,936`, `chemistry/jobs.py`, `io/jobs.py`…). Esos jobs asumen que
corren en proceso y contra la db del proyecto; la ejecución remota no hace ninguna de las dos.

La campaña **sale** del sistema de jobs para la pata remota y **vuelve a entrar** en el ingest
del top-N, que sí es un job local normal contra la db. Dos puntos de contacto definidos, cero
cambios en las ~20 declaraciones existentes.

### Tareas

| # | Archivo | Acción |
|---|---|---|
| 6.a | `htp/bundle.py` (nuevo) | Generar directorio: fuentes, `config.json`, script de job array que fragmenta y luego dockea |
| 6.b | `htp/bundle.py` | Registrar una fila en `screening_dispatches` por fuente al generar el bundle |
| 6.c | `htp/bundle.py` | Ingesta desde directorio de resultados: puebla `screening_shards`, cierra el envío y escribe por el sink de §4 |
| 6.d | — | Documentar el formato de bundle (es el contrato que hay que estabilizar) |

**Check:** bundle generado sobre 3 fuentes, ejecutado a mano en local con el script (que produce N
shards que el bundle no conocía), resultados ingeridos, `screening_shards` poblada con esos N y
materialización respetando umbral y tope.

---

## §6-bis — Ejecutor HPC de verdad (diseño, sin implementar · 2026-09-05)

§6 evita el transporte a propósito (bundle + copy-paste). Esto es lo otro que se pidió: **elegir
HPC como ejecutor**, o para toda la campaña o solo para el docking.

### Lo que MF ya trae

`ExecutorRegistry.register_hpc(name, submit_command, poll_command, cancel_command, shared_fs,
command_context, command_env, python_executable)` monta un `HPCCommandExecutorAdapter`: por cada
chunk escribe un directorio de trabajo con `manifest.json` + `submit.sh` (que ejecuta
`python -m ms_flow.core.executor.hpc_runtime`), lanza `submit_command`, sondea con `poll_command`
y lee `result.json` / `status.json`. `submit_job(..., executor_name="hpc")` ya valida contra
`job.supported_executors`. Es decir: **el despacho por job existe y no hay que escribirlo**.

Dos límites que hay que aceptar de entrada:

1. **El staging es `shutil.copy2`.** MF no tiene SSH ni rsync: da por hecho que el directorio de
   trabajo remoto se ve desde aquí (NFS, sshfs, lo que sea). Si no se monta, el transporte se
   mete en la plantilla del comando — que es shell libre: `rsync -a {job_dir} host:… && ssh host
   sbatch …`. Funciona, pero entonces el bundle de §6 es el camino honesto.
2. **El `PYTHONPATH` remoto es el `sys.path` local.** El cluster necesita el mismo entorno visible
   en las mismas rutas, o pasar `python_executable` + `command_env` apuntando al suyo.

### Lo que falta de nuestro lado

| # | Archivo | Acción |
|---|---|---|
| 6bis.a | `constants.py` + jobs de shard | Añadir `"hpc"` a `supported_executors` **solo** en los jobs fichero→fichero (`shard_chemistry_job`, `prepare_ligand_shards_job`, y el docking de shards de §4). Los jobs de filas no: tocan `project.db`, que allá no existe |
| 6bis.b | `configuration.py` | Bloque `hpc:` (host, comandos submit/poll/cancel, raíz remota, partición, módulos, python) por el patrón de reconciliación de config |
| 6bis.c | `ui/…/hpc_dialog.py` (nuevo) | Diálogo "Configurar HPC": campos, **Probar conexión** (lanza `poll_command` contra un job inexistente y espera un fallo *limpio*), guardar |
| 6bis.d | Import + Docking | Botón "Configurar HPC…" en los dos sitios, y un `executor_name` por campaña que por defecto es local |
| 6bis.e | `runtime` | Registrar el ejecutor al abrir proyecto si hay config |

**Por qué la preparación sobre shards era el requisito previo:** un job solo puede irse al cluster
si su entrada y su salida son ficheros. `prepare_ligand_shards_job` lee un `.mshard` y escribe otro
— nada más. Ese es exactamente el trozo de la campaña que puede viajar, y "todo en local menos el
docking" es entonces un `executor_name` distinto en un job, no una rama nueva.

**El regreso sigue siendo local.** El top-N vuelve como ficheros y entra por el ingest de §4
(`molecules` + `docking_results`), que es un job local normal contra la db. Dos puntos de contacto,
igual que en §6.

---

## Diseño de UI

### El principio

> **En `htpvs`, una molécula se gana su fila puntuando bien.**

La tabla de ligandos está vacía durante toda la campaña; la de Docking Results se va llenando.
No es una carencia que disimular: es lo que el modo significa, y se explica en una frase.

### Qué pasa con Build

El **panel** desaparece; los **widgets de configuración** de `build.py` (~754 líneas) se
convierten en los editores de paso del constructor. No se tira nada, se reubica.

```
Pipeline de preprocesamiento              [+ Añadir paso ▾]
┌──────────────────────────────────────────────┐
│ ⠿  1. Standardize      neutralize, fragmento  │  ⚙  ✕
│ ⠿  2. Protonate        Dimorphite-DL · pH 7,4 │  ⚙  ✕
│ ⠿  3. Generate 3D      ETKDG                  │  ⚙  ✕
└──────────────────────────────────────────────┘
1 job · ~14 min estimados sobre 1.243.891 moléculas
```

Arrastrar reordena; `⚙` abre el groupbox que ya existe, ahora como diálogo del paso.

**Es un componente, no una pantalla.** En `vs` el mismo constructor aparece detrás del "Fix ▾"
del checklist de requisitos, mostrando exactamente qué se va a ejecutar. Un componente, dos
consumidores — que es lo que justifica que exista.

### Pantalla de campaña (`htpvs`, zona central)

Mismo esqueleto de 4 pasos que el wizard de `vs`; la consistencia es gratis.

| Paso | Contenido | Reutiliza |
|---|---|---|
| 1 · Fuente | Ficheros/directorios + prefiltro + tamaño de shard. Muestra lo que se sabe sin abrir nada: *"3 ficheros · 37,3 GB"*. El conteo de shards **no** se estima: en HPC lo decide el cluster (§3) | `ImportFilterCriteriaForm` (`filter.py:298`) |
| 2 · Preprocesamiento | El constructor de pasos. **Puede quedar vacío** (DiffDock desde SMILES) | widgets de `build.py` |
| 3 · Diana y programa | Receptor del proyecto (preparado en local con las herramientas normales), grid, programa + parámetros | `protocol_editor.py`, `grid_box.py` |
| 4 · Salida y lanzamiento | Umbral, **tope obligatorio**, destino (local / bundle) → *Generar bundle* | nuevo, pequeño |

### Las tres zonas en `htpvs`

| Zona | Qué cambia |
|---|---|
| Barra superior (sustantivos) | Se antepone la **insignia de modo** y se añade **Shards**. Receptors, Binding Sites, Docking Results: igual. Ligands pasa a llamarse **Hits**. Molecules y Activity: **ocultas** |
| Rail izquierdo (verbos) | Filter y Build desaparecen (viven dentro de la campaña). Quedan Pocket Detection y Diversity: ambos funcionan, los receptores están materializados y la diversidad lee shards |
| Centro | La campaña y, después, el **monitor de campaña**: shards hechos/total, throughput, hits materializados, errores. Sustituye a la tabla como "la cosa que miras" |
| Zona auxiliar | La tabla de **envíos**, reclamada por la pestaña Shards |
| Derecha | PyMOL sirve igual para receptor y grid; al final, para los hits materializados |

**La insignia ya está** (2026-09-03): `QLabel` con `objectName="mode_badge"` al principio de
`build_catalog_toolbar`, antes de Molecules, seguida de un separador. Texto de
`vocab.PROJECT_MODE_BADGES` (`VS` / `HTP-VS`), tooltip con el nombre largo, y una sola línea de
estilo separa los dos modos: `palette(text)` en `vs`, `palette(highlight)` en `htpvs`. Se
refresca desde `_sync_window_title`, que es el único sitio que corre tanto al arrancar como al
abrir un proyecto después. Check: `test/test_mode_badge.py`.

**La insignia va como etiqueta, no como acción.** En `build_catalog_toolbar` toda la barra son
`QAction` *checkables* donde marcado = pestaña abierta; un indicador de modo no abre nada y nunca
se desmarca. Un `bar.addWidget(QLabel)` antes del primer bucle ocupa la misma posición sin
prometer un toggle que no existe.

Y se muestra **en los dos modos**. Si solo apareciera en `htpvs`, su ausencia sería ambigua: no
distingues "proyecto vs" de "la insignia no se pintó". Que `vs` se vea callado y `htpvs` con
acento es una línea de estilo, no una rama.

**Ligands → Hits.** En `htpvs` la pestaña está casi siempre vacía hasta la promoción, y eso es
correcto — es la tabla de hits. Pero la etiqueta miente: ahí "Ligands" significa "lo que puntuó lo
bastante bien para ganarse una fila". El renombrado explica el vacío mejor que ninguna insignia.
Es filtro base, no tabla nueva: `MoleculeWidget` no tiene `default_filters` y Ligands/Receptors ya
son esa misma tabla filtrada (`ligands.py:69-71`).

### Shards y envíos: dos tablas, cero mecanismo nuevo

`screening_shards` empieza vacía y solo se puebla con la retroalimentación del cluster (§3). Eso
está bien **siempre que haya quien explique el vacío**, y ese es el trabajo de la tabla de envíos:

```
┌─ Shards ──────────────────────────────────────────────┐
│                                                        │
│         Fragmentando en el cluster.                    │
│         Aún no hay shards que mostrar.                 │
│                                                        │
├─ Envíos ──────────────────────────────────────────────┤
│ zinc_in_stock_01.smi   12,4 GB   running    — shards  │
│ zinc_in_stock_02.smi   11,8 GB   running    — shards  │
│ zinc_in_stock_03.smi   13,1 GB   returned  1.204      │
└────────────────────────────────────────────────────────┘
```

Doce filas diciendo qué subiste y en qué estado convierten un vacío ambiguo en uno informativo.

No hace falta registrar nada nuevo para colocarlo: la zona auxiliar ya tiene este contrato — una
vista central reclama el hueco exponiendo `aux_panel()`, y el panel *pertenece a la tabla que
estás mirando* y se devuelve al salir de la pestaña (`auxiliary_panel.py:20-23`). Es el mismo
patrón que Docking Results con su "Selected Result". `ShardsWidget.aux_panel()` devuelve la tabla
de envíos y ya está: sin registro nuevo, sin toggle nuevo.

*Seguimiento que no va en la v1:* seleccionar un envío y acotar los shards a los suyos. Es
`push_scope`, que ya existe, pero invierte la dirección habitual (centro → aux) y no hace falta
hasta que haya varias campañas vivas a la vez.

### Lo que ya está cableado (2026-09-04 / 05)

**La barra de catálogo depende de los datos, no del modo** (`ui/shell/view_coordinator.py`,
`sync_catalog_availability`). Una tabla significa siempre lo mismo: **no se renombra ninguna**.

| | proyecto normal | librería shardeada |
|---|---|---|
| Molecules / Activity | visible | visible (nada se oculta) |
| Ligands | visible | visible, **gris** mientras no haya ninguna fila de ligando |
| Shards | ausente | visible |

Dos acciones independientes, no una renombrada: los ligandos de un HTP no encajan ni en
`molecules` ni en `ligands` — por eso existe `screening_shards`. Ligands sigue siendo lo que
siempre fue (cocristales de referencia para redocking/QSAR, y los hits promocionados cuando se
materializan) y se enciende sola en cuanto existe una de esas dos cosas; `ligand_row_count`
(`molecules/store.py`) es esa cuenta.

**El estudio de docking distingue las dos preguntas** (`tools/docking/scope_panel.py`): *qué*
ligandos los decide el experimento (docking → librería general, redocking → cocristales de
referencia) y *dónde* viven lo decide el proyecto (filas en `vs`, shards en `htpvs`). Solo la
combinación htpvs+docking apunta el paso a Shards; cambiar el experimento a redocking cambia la
pestaña bajo el usuario (`_focus_ligand_view`, solo mientras está en el paso de ligandos).
`_selected_ligand_ids` devuelve `[]` con scope shardeado — los ids de shard no son ids de
molécula. Run Docking todavía se niega: dockear shards enteros es la campaña (§6).

**Detalle de shard** (`ui/catalog/details.py::show_shard`): la fila más lo que declara la
cabecera del `.mshard` (`_shard_header`: `record_count`, `base_id`, `slot_count`, `metadata`).
Solo metadatos, por decisión — listar los registros sería el paseo por la librería que `htpvs`
existe para evitar. Sin PyMOL y sin rejilla: `AuxiliaryPanelController` limpia las dos para el
kind `"shard"`.

**Preparación sobre shards** (`docking/shards.py` + `PrepareLigandShardsJobSpec`). Mismo botón,
misma lista de programas; el trabajo es un shard, no una fila:

- un `.mshard` de moléculas entra, un `.mshard` de kind `PDBQT` sale, **en la carpeta hermana**
  `prepared_<engine>/` — misma forma que la etapa de química, así una campaña se lee como una
  cadena de carpetas;
- el texto PDBQT va **dentro** del shard, en el **mismo id** que la molécula de origen: ni un
  fichero por ligando ni una fila de `EngineState`. Si algún paso futuro solo sabe escribir un
  fichero, `result_text()` lo lee y lo borra — nada queda al lado del shard;
- el 3D se genera **solo donde falta** (`_with_3d`): una librería SMILES no tiene coordenadas y
  Meeko las exige, pero re-embeber todo tiraría el confórmero de la etapa de química;
- la fila de `screening_shards` se hace upsert con `path` → shard preparado e `input_format` →
  `pdbqt`. Eso **es** el flag de "ya preparado": el feed salta esos shards salvo `force`, y para
  re-preparar se vuelve al origen por `metadata["prepared_from"]`;
- una molécula que no se puede preparar deja un hueco en su id, no renumera.

Comprobación: `test/test_mode_badge.py`, `test/test_docking_ligand_scope.py`,
`test/test_shard_details.py`, `test/test_shard_preparation.py`.

### Selección del modo

Radio en la creación de proyecto, con la consecuencia escrita:

```
○ Cribado virtual (VS)
  Las moléculas viven en el proyecto. Puedes filtrarlas, inspeccionarlas y
  reprocesarlas en cualquier momento. Hasta ~1M.

○ Alto rendimiento (HTP-VS)
  Las moléculas viajan como shards y nunca entran en el proyecto; solo vuelven
  los mejores resultados. Pensado para HPC. No podrás inspeccionar ligandos
  individuales hasta que la campaña termine.

  Esta elección no se puede cambiar después.
```

Como es inmutable, es **identidad**: la insignia de la barra de datos y el nombre del proyecto en
la barra de estado. No aparece como interruptor en ningún otro sitio.

---

## Orden de ejecución

**§0 → §1 → §2, y parar a mirar.** Con eso está arreglado el auto-encadenado, el constructor de
pasos funciona en modo normal y el store queda ordenado — todo verificable con lo que ya hay, y
todo útil aunque HTP no se haga nunca.

§5 puede entrar en paralelo con §1 (es independiente y pequeño).

§3, §4 y §6 son la campaña propiamente dicha. Para entonces ya se sabrá si el constructor de
pasos se siente como se quería, antes de haber apostado nada por HTP.

## Lo que NO se hace en este plan

- **Transporte remoto** (SSH, credenciales, polling). §6 entrega bundle + ingesta manual.
- **Reparto multinodo con Ray.** Sigue aplazado igual que en el plan anterior: falta operación
  (sistema de ficheros compartido), no arquitectura.
- **Esquema de `molecules`.** No se toca, por la misma razón de §5 del plan anterior: el embudo
  hace que nunca vea 10⁸ filas.
- **El rediseño de layout de modo `vs`** (docking al centro, rail de 5→3, accesorios del visor
  plegados). Es independiente de todo lo anterior y va en su propio documento.
