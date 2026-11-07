# Plan: unificación del acceso a datos (ms_flow + AMDock)

Fecha: 2026-08-27
Origen: revisión de fugas de memoria en `../src/amdockvs` y `ms_flow`.

## Principio rector

> **`InputSource` planifica. `DataBridge` lee. Una sola puerta a la db.**

- `InputSource` = generador con estado. Responde *"¿cómo parto el trabajo total en N chunks?"*.
  Es lógica de dominio (producto cruzado receptor×ligando, recorrido de archivos, presupuesto de bytes).
- `DataBridge` = resolutor sin estado. Responde *"dado un spec, dame los datos"*.
  Es el **único** que abre la db o el filesystem.

Hoy `InputSource` hace las dos cosas (trocea *y* lee vía `db_stream`), por eso `DataBridge`
está construido pero sin uso real en AMDock (0 `InputSpec` producidos en todo `AMDockVS/src`).
Esa es la duplicación a eliminar: **no una capa, sino que `InputSource` lea**.

Regla derivada, no negociable:

> **Nada se materializa en Python si la db lo resuelve.**
> **AMDock no escribe SQL ni SQLAlchemy: declara `QuerySpec`. Solo ms_flow traduce.**

---

## Fase 1 — Arreglar memoria y unificar capas

No toca la frontera A/B. AMDock sigue alimentando jobs por la ruta actual.
Todos los hallazgos de memoria quedan cerrados aquí, sin apostar a una ruta sin ejercitar.

### 1.1 ms_flow · una sola fábrica de engine y un solo compilador de SQL

Bug real, no estética: `core/database/base.py:42 create_sqlite_engine()` aplica
`timeout=30.0`, `busy_timeout=30000`, `foreign_keys=ON`, WAL, `synchronous=NORMAL`.
Ninguna ruta de `core/data/backends.py` lo hace.

| Tarea | Archivo | Acción |
|---|---|---|
| 1.1.a | `core/data/backends.py:681-730` | **Borrar** el fallback de escritura con `sqlite3` crudo. Solo alcanzable con `db_role != "project"`, y `db_role` es `"project"` en el 100% de los call sites de ambos repos. Código muerto por alcanzabilidad. |
| 1.1.b | `core/data/backends.py:378, 523, 639, 654` | Sustituir `create_engine(f"sqlite:///{db_path}")` por `create_sqlite_engine()`, **cacheado por path**. Hoy se crea un engine (y su pool) por llamada. |
| 1.1.c | `core/data/backends.py:570-601` | `SQLiteBackend.read` conserva firma y contrato; cambia solo por dentro: compone un `QuerySpec` y delega en `ProjectStore`. Elimina `sqlite3.connect` (busy_timeout 5s por defecto y `foreign_keys=OFF`) y el `SELECT` armado a mano. |
| 1.1.d | `core/data/contracts.py:39` | Borrar `compile_db_filters` una vez 1.1.c la deja huérfana (único uso: `backends.py:583`). |
| 1.1.e | `core/data/backends.py` | Ruta de lectura por lotes: `read()` con `batch_size` usa `ProjectStore.stream_rows` en vez de `fetchall()` (`:579`, `:600`). |

Infra de abstracción — qué se queda y por qué:

- **Se queda** el registry por *kind* (`inline`/`bytes`/`file`/`db`/`graph`). No es abstracción
  multi-motor: es multi-**fuente**, y es exactamente lo que necesita HTP para que un shard en
  disco entre por la misma puerta que una tabla.
- **Se va** lo multi-motor: nunca existió (no hay postgres, ni `db_url`, ni dialect switching).
  Concretamente, los roles `db_role` `executor`/`custom` no los usa nadie → 1.1.a.
- `DataBridge(input_backends=..., output_backends=...)`: parámetros de inyección que nadie pasa.
  Son 4 líneas; se dejan, borrarlos no paga.

**Check:** un test que abra dos conexiones concurrentes al mismo `project.db` vía backend y
verifique `PRAGMA foreign_keys == 1` y `busy_timeout == 30000`.

### 1.2 ms_flow · extender `QuerySpec` (mínimamente) + `db_count`

`QuerySpec` es un **adaptador**: forma declarativa, serializable a `payload_json`, traducible.
No es un ORM y no debe crecer hacia uno. Se le añade **un** operador y **un** ejecutor.

| Tarea | Archivo | Acción |
|---|---|---|
| 1.2.a | `query.py` | Operadores `__in_subquery` / `__not_in_subquery`, cuyo valor es **otro `QuerySpec`**. `compile()` pasa a ser recursivo (~15 líneas). Serializable, sin SQL crudo, sin SQLAlchemy en AMDock. |
| 1.2.b | `query.py` | `db_count(spec)`. Mismo spec, tercera salida junto a `db_rows` / `db_stream`. **No** se añaden agregados a la gramática. |
| 1.2.c | `query.py` | `QuerySpec.from_select(stmt)` como válvula **interna de ms_flow**: congela `str(stmt)` + params compilados en el `query=`/`params` que ya existe. Es la garantía de que 1.2.a sea el último operador que se añade. |
| 1.2.d | `query.py` | Renombrar la familia `project_*` → algo que no colisione con "project" del registro de ms_flow (p. ej. `rows()`/`stream()`/`count()` en un namespace `ms_flow.query`). Cosmético; hacerlo ahora que hay pocos call sites. |

**Check:** `assert` sobre `QuerySpec(...).compile()` con un `__not_in_subquery` anidado
comparando SQL y params esperados.

### 1.3 amdock · matar los sets de Python

Los tres sets tienen **la misma forma**: `id IN (SELECT ... FROM otra WHERE ...)`.
Existen solo porque `QuerySpec` era mono-tabla. Con 1.2.a desaparecen los tres.

| Tarea | Archivo | Acción |
|---|---|---|
| 1.3.a | `docking/repository.py:525-555` | `existing_result_pairs()` → borrar. Sustituir por: por receptor, `ligand.id __not_in_subquery(DockingResult.ligand_molecule_id WHERE receptor_id=R)`. Una columna, cero Python. |
| 1.3.b | `docking/service.py:327-370` | `iter_docking_batches_from_rows`: quitar `if (receptor_id, ligand_id) in skip_pairs`. El skip lo hace la db. |
| 1.3.c | `docking/jobs.py:328` | Quitar `skip_pairs` del closure del generador. Es la única estructura que escalaba con el total en vez de con el batch. |
| 1.3.d | `docking/jobs.py:79-104` | `count_pending_docking_pairs`: fuera `set(entity_ids(...))` de ligandos y receptores. Se resuelve con `db_count(spec)` (1.2.b). |
| 1.3.e | `docking/repository.py:196` | `_iter_entity_ids`: quitar el post-filtrado en Python contra `allowed` y `prepared`. Ambos son subqueries. |
| 1.3.f | `docking/api.py:495-540` | `check_required`: hoy construye 4 colecciones del tamaño de la biblioteca para reportar 4 números. Reescribir con `db_count` + muestreo, siguiendo el patrón que ya usa `check_ligand_preparation_required` (`docking/api.py:300`, `MISSING_SAMPLE_SIZE`). |
| 1.3.g | `docking/repository.py:78` | `_augment_molecule_row` lanza 2 queries por fila (N+1). Resolver con subquery/join en el spec. |

**Check:** con 50 ligandos × 2 receptores y 30 pares ya dockeados, `count_pending_docking_pairs`
devuelve 70 y el generador emite 70 pares — sin construir ninguna colección de tamaño biblioteca.

### 1.4 amdock · disolver `queries.py` en repositories por herramienta

`queries.py` es una capa global compartida (18 métodos: 14 de docking, 4 de molecules).
Es lo contrario de lo que se quiere: tocar docking obliga a tocar un módulo global.

| Tarea | Acción |
|---|---|
| 1.4.a | Los 14 métodos de docking bajan a `docking/repository.py` (ya existe). |
| 1.4.b | Los 4 de molecules (`list_ligands`, `list_receptors`, `get_ligand_table_stats`, `get_receptor_table_stats`) van a un `molecules/repository.py` nuevo. Los dos `*_table_stats` **se conservan** (sin callers hoy, pero previstos) y se reescriben con `db_count` en vez de leer la tabla entera (`queries.py:436`, `:476`). |
| 1.4.c | Borrar `_safe_project_rows` (`queries.py:225`): su `query=` crudo es justo el atajo que 1.2.a/1.2.c sustituyen, y el `except` que devuelve `[]` ante "no such table" enmascara errores. |
| 1.4.d | Borrar `queries.py` y `project_queries()` (13 call sites, 12 desde `docking/api.py`). |
| 1.4.e | `docking/*`: eliminar `session.exec(select(...))` directo. No es la puerta equivocada por sí mismo, pero docking hoy usa **tres** a la vez (ORM + `db_stream` + `db_rows`). Queda una: el repository construye `QuerySpec`, `api.py` no sabe de queries. |

Regla que queda: **una herramienta = un repository = todos sus `QuerySpec`**.

### 1.5 UI · una sola tabla, dos modos de navegación

Hoy hay cuatro mecanismos de paginación: `ms_table` (offset), `lazy_table` (offset),
`workspace` (offset + append manual) y `page_after` (keyset, en ms_flow, sin usar).

| Tarea | Archivo | Acción |
|---|---|---|
| 1.5.a | `ms_components/ms_table/table_config.py` (`ColumnDef`) | Permitir que una columna lleve una **expresión SQLAlchemy** además de un nombre. `QueryBuilder` ya construye con SQLAlchemy, así que la acepta sin cambio estructural. Esto es lo único que le faltaba a `SmartTableView` para columnas `JSON_EXTRACT`. |
| 1.5.b | `amdock/ui/lazy_table.py` | **Borrar** (191 líneas). Su única razón de existir la cubre 1.5.a. |
| 1.5.c | `ms_components/ms_table/table_config.py` | Flags en `TableConfig` para ocultar filtros/búsqueda (tabla "simple"). No hace falta un componente nuevo. |
| 1.5.d | `amdock/ui/workspace.py` | Las tres tablas de resultados (receptores, ligandos y poses) → `SmartTableView` con config simple. Mata `self._ligand_rows.extend(rows)` (acumulación no acotada) y el `resizeColumnsToContents()` sobre todas las filas cargadas (escroleo cuadrático). |
| 1.5.e | `ms_components/ms_table/query_builder.py:167-168` | Navegación: **offset** para `TableLoadMode.PAGINATED` (hace falta saltar a la página N), **keyset** para `INFINITE` (solo avanza). El keyset ya existe: `ms_flow/core/database/project.py:510 page_after(cursor_field, after, limit)`. Cursor compuesto `(sort_key, id)` para orden no único. |
| 1.5.f | `ms_components/ms_table/query_builder.py` | `QueryBuilder.to_query_spec()`. Es el caso que lo justifica: la selección **"filtered"** tiene que llegar al job. Sin esto, o manda una lista de ids materializada (el problema que estamos matando) o no se puede pasar. |

**Check:** cargar 5.000 filas en modo `INFINITE` y verificar que el modelo mantiene ≤ 2 páginas
en memoria y que el SQL emitido no contiene `OFFSET`.

### 1.6 ms_flow · sink de archivos (hallazgo #4)

`core/executor/result_handlers.py:208` mantiene `self._file_history: list[StagedOutput]`, al que
se hace append por cada chunk `FileOutputSpec` (`:401`) sin podar hasta `close()`; y `_file_payload()`
(`:412`) hace `list(self._file_history)` y **reconstruye el archivo entero en cada `write_batch`**
→ memoria O(N) y IO O(N²). Latente: AMDock hoy solo usa sinks de db.

Dos modos, según lo que definiste:

- `append`: se abre el archivo una vez y cada batch se anexa. Sin historia en memoria.
- `per_batch`: un archivo por batch. Solo se retiene el manifiesto de rutas.

En ambos, el valor puede ser **una lista de archivos** (una salida que escribe dos archivos con
dos informaciones distintas; o un batch que devuelve varios archivos individuales).

### 1.7 ms_flow · retención en `executor.db` (hallazgo #6)

`ExecutorJobChunk` y `ExecutorJobEvent` no tienen política de retención → crecimiento indefinido
en disco. Purga por antigüedad/job terminado, configurable. Es disco, no RAM: prioridad baja
dentro de la Fase 1, pero cierra el hallazgo.

---

## Fase 2 — `InputSource` emite specs (habilita HTP)

Solo cuando HTP sea real. En este punto `DataBridge` estrena con el motor ya arreglado por 1.1
en vez de con `sqlite3` pelado.

Objetivo:

```
InputSource  →  compone specs  →  DataBridge  →  ÚNICA puerta a la db
 (lógica)         (referencias)     (lectura)
```

| Tarea | Acción |
|---|---|
| 2.1 | `InputSource` deja de llamar `db_stream`. Cuando necesita saber dónde cortar, compone un spec de **una sola columna** y se lo pide al bridge: `DbInputSpec(fields=["id"], where={"id__gt": last}, limit=N)`. Streaming, memoria constante sea cual sea el total. Keyset, no OFFSET — misma primitiva que 1.5.e. |
| 2.2 | Nuevos métodos en `InputSource`: `count()` → `None` por defecto, sobrescrito por los hijos que puedan (`ProjectQueryInput` → `db_count`; fuente HTP de archivos → `None` o el conteo del índice). Y `filter(predicate)` genérico, aplicado dentro de `iter_chunks`, perezoso. |
| 2.3 | `docking/jobs.py:568 DockingPairsInput` y `io/jobs.py:182 FileInput` emiten chunks con **referencias**, no filas. El payload en `payload_json` deja de ser una segunda copia de la biblioteca. La lógica de troceo (producto cruzado, parámetros por par, presupuesto de bytes) se queda donde está: es lógica de dominio, no cabe en un spec. |
| 2.4 | `amdock/molecules/stream.py`: `MoleculeStream` **se borra** — es una tercera copia de `InputSource`. `MoleculeRow` se queda en AMDock, pero el envoltorio se hace **del lado del consumidor** (la función del job), no en el feed: todo lo que entra en `payload_json` debe ser plano. Cero coste de envoltorio en la ruta de feed; sin conflicto con la postura de `bulk_insert`/SQLAlchemy Core. Quitar además el `dict(data)` copy de `MoleculeRow.__init__`. |
| 2.5 | Fuente HTP de archivos: shards en disco + índice ligero (`.idx` sidecar con offsets). Cumple el mismo protocolo `InputSource`; ningún método de docking/prep/selection se entera. |
| 2.6 | Resume: `cursor_position` (`core/executor/services/feeding.py:247`) es un **contador de chunks**, no un cursor de datos — al reanudar una fuente diferida se re-itera y se descartan N chunks. Con keyset se persiste la última clave emitida y se arranca de ahí. |

### 2.7 Identidad de molécula (clave hash / id)

Dos identidades separadas, es la única forma que funciona para HTP:

- **Hash de contenido** (InChIKey o hash canónico) = **identidad**. Estable, se calcula sin db,
  viaja con la molécula desde el shard, es la clave del `MoleculeRow` mientras vive en archivos.
- **`id` de la db** = **localidad**. Solo existe si la molécula se promueve a `project.db`;
  la fila guarda el hash como columna indexada.

Así el `__not_in_subquery` funciona igual sobre `id` (flujo normal) que sobre el hash
(flujo HTP promovido), y un hit que se promueve se reconcilia sin re-parsear nada.

---

## Orden y dependencias

```
1.1 (engine + compilador)  ──┐
1.2 (QuerySpec + count)    ──┼──> 1.3 (sets de Python)  ──> 1.4 (repositories)
                             └──> 1.5.f (to_query_spec)

1.5.a ──> 1.5.b ──> 1.5.d          1.5.c ──> 1.5.d          1.5.e (independiente)

1.6, 1.7  independientes

Fase 1 completa ──> Fase 2
```

Camino crítico: **1.2 → 1.3**. Es lo que cierra los hallazgos de RAM (#1, #2, #3)
y lo que quita las comparaciones de listas enormes en Python.

## Hallazgos cubiertos

| # | Hallazgo | Tarea |
|---|---|---|
| 1 | `skip_pairs` + sets de ids en `docking/jobs.py` | 1.3.a–c |
| 2 | `check_required`: 4 colecciones para 4 números | 1.3.f |
| 3 | `*_table_stats` leen la tabla entera para agregar | 1.4.b |
| 4 | Sink de archivos O(N) memoria / O(N²) IO | 1.6 |
| 5 | Acumulación no acotada en `workspace.py` | 1.5.d |
| 6 | `executor.db` sin retención | 1.7 |
| — | `sqlite3` crudo sin pragmas (bug de concurrencia + FK off) | 1.1 |
| — | Dos puertas de acceso a db | Fase 2 |

---

## Estado Fase 1 — 2026-08-28

| Tarea | Estado | Nota |
|---|---|---|
| 1.1 | hecho | pragmas + FK en el acceso sqlite |
| 1.2.a–b | hecho | `__in_subquery` / `__not_in_subquery`, rutas JSON declarativas, `db_count` |
| 1.2.c–d | **no hecho** | `QuerySpec.from_select` sin consumidor; renombrar la familia `project_*` es churn sin ganancia |
| 1.3 | hecho salvo redocking | el cribado es `NOT IN (subconsulta)`; redocking sigue con `existing_result_pairs` (acotado por el set de complejos, y `DockingResult` no tiene columna `complex_id` con la que armar la subconsulta) |
| 1.4 | hecho | `queries.py` (960 líneas) borrado → `docking/repository.py` + `molecules/repository.py`. `list_ligands` / `list_receptors` / `molecule_source_properties` se borraron en vez de moverse: filtraban por `primary_role`, que no es columna de `MoleculeRecord` — estaban muertas y rotas. Los `*_table_stats` se reescribieron agregando en SQL |
| 1.5.a | hecho | `ColumnDef.expr` (expresión SQLAlchemy); filtrable y ordenable |
| 1.5.b | hecho | `lazy_table.py` borrado |
| 1.5.c | hecho | `TableConfig.show_filters` / `show_search` |
| 1.5.d | hecho | `By Receptor`, `Ligands` y `Poses` son `SmartTableView`. Ligands usa `pose_rank=1` como fila raíz y un subquery correlacionado acotado para `pose_count`; ya no hay lista/append manual en memoria. |
| 1.5.e | hecho | Keyset en `INFINITE` (cursor compuesto `(orden, pk)`) y ventana real de 2 páginas que reemplaza las anteriores. La carga normal no hace `COUNT`; pide una fila extra como centinela. `OFFSET` queda sólo como fallback para saltar hacia una zona cuyo cursor ya no está en la ventana. |
| 1.5.f | hecho | `QueryBuilder.to_query_spec()` → dict de kwargs (la tabla no depende de ms_flow); levanta si el estado no es expresable |
| 1.6 | hecho | `FileOutputSpec.append` y `.per_batch`. Lo de "el valor puede ser una lista de archivos" no se hizo: sin consumidor no hay contrato de nombres |
| 1.7 | hecho | `ExecutorStore.purge_finished_jobs(older_than_days=30)`, se corre al abrir proyecto; expuesta como `purge_job_history` |

---

## Estado Fase 2 — 2026-08-28

| Tarea | Estado | Qué se hizo / por qué se desvió |
|---|---|---|
| 2.1 | Hecho, por otra puerta | `ms_flow.query.db_pages(source, spec, key="id", page_size=N)`: keyset (`WHERE key > última ORDER BY key LIMIT n`), una lectura corta por página. **No va por `DataBridge`**: tras 1.1 `ProjectStore.read_rows` y `SQLiteBackend.read` compilan por el mismo `compile_select` sobre el mismo `get_sqlite_engine`, y `read_rows` ya abre y cierra sesión por llamada. Meter un `DataBridge`+`DataContext` en el config del chunk-source era fontanería sin diferencia de comportamiento con SQLite como único motor. Adoptado por `ProjectTableInput`, `ProjectQueryInput`, `iter_entity_rows`, `_iter_entity_ids` y el alcance de selección. Sin clave utilizable (SQL crudo, `offset`, orden ajeno) cae a `db_stream`. |
| — | Bug encontrado de paso | `compile_select` emitía `OFFSET ?` sin `LIMIT`, que SQLite rechaza. Ahora emite `LIMIT -1 OFFSET ?`. |
| 2.2 | `filter()` sí, `count()` no | `InputSource.filter(predicate)` genérico y perezoso, aplicado dentro de `iter_chunks` (y también en el `iter_chunks` propio de `SimpleInputSource`). `count()` **no se añadió**: nadie lo consume — `total_chunks` no se alimenta de la fuente, y declararlo de más deja el job sin completar nunca. El único sitio que contaba recorriendo (`sum(1 for _ in ...)` en `selection/api.py`) pasó a `scope_molecule_count`, que es un `COUNT`. |
| 2.3 | Hecho (import); docking ya cumplía | `DockingPairsInput` ya emitía referencias (`build_docking_pair`: ids, rutas, números). La segunda copia de la biblioteca estaba en el feed de import: cada chunk llevaba ~4 MB de texto `raw` en `payload_json`, o sea un SDF de 10 GB metía 10 GB en payloads/spool. Ahora el chunk lleva un **rango de bytes** (`span_offset`, `span_end`, `span_first_index`, `span_count`) y el worker relee ahí sus registros (`iter_record_spans` / `read_record_span` en `io/parsers/readers.py`). El payload baja de ~4 MB a ~200 B. `records`/`entries` siguen valiendo para formatos sin separador (un PDB suelto) y para payloads antiguos. |
| 2.4 | Hecho, y de más | `MoleculeStream` borrado. También `MoleculeRow`, `from_rows` y `db_molecules`: al mover el envoltorio al consumidor se quedaron sin un solo uso, y `db_molecules` era un alias exacto de `iter_entity_rows`. El módulo `amdock/molecules/stream.py` desaparece entero. El troceo es `itertools.batched` y el filtrado el `filter` de serie — escalera, peldaño 2. De paso, la pertenencia a un set en el alcance de selección era un `set` de Python con todos los ids; ahora es `id__in_subquery`. |
| 2.5 | No construido | Sigue sin consumidor y el propio plan lo condiciona a "solo cuando HTP sea real". Lo que sí quedó hecho es su primitiva: `iter_record_spans` **es** el índice de offsets del sidecar `.idx`, y `read_record_span` el lector de shard. Cuando HTP exista, esto es la mitad del trabajo. |
| 2.6 | No aplica como está escrito | La premisa no se cumple: nada en ms_flow reanuda un feed entre procesos. `JobFeed` sólo se crea en `submit`, en memoria, y `restore_chunk_source` se llama siempre con `cursor_position=0` — no hay ningún sitio donde se descarten N chunks. Persistir una clave de datos aquí sería construir la función de reanudación, no arreglarla. Se deja `cursor_position` como el contador que es. |
| 2.7 | Ya estaba en el esquema | Las dos identidades ya existen: el InChIKey vive en `molecule_representations` con `Index("idx_repr_lookup", repr_type, value)` — columna hash indexada — y `molecules.id` es la localidad. `__not_in_subquery` funciona hoy sobre cualquiera de las dos. No hay nada que construir hasta que las moléculas vivan en shards (2.5). |

Comprobaciones nuevas: `ms_flow/tests/test_project_pages.py` (páginas == cursor, una lectura corta por
página, límite/filtros, y las caídas a `db_stream`), `test/test_import_record_spans.py` (el rango
releído rinde exactamente los registros de antes; los chunks son contiguos y no llevan la biblioteca) y
`test/test_preparation_stream.py` (sustituye a `test_molecule_stream.py`: lo que queda por proteger es
que el consumidor trocee perezosamente y salte lo ya preparado).
