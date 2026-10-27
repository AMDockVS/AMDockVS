# Plan: normalización de jobs + modo HTP

Fecha: 2026-08-29
Origen: continuación de `plan_acceso_datos_2026-08-27.md` (Fases 1 y 2 cerradas) y de la revisión
de memoria del 2026-08-28/29.

## Estado de partida (lo ya cerrado, no repetir)

| Hecho | Dónde |
|---|---|
| Subconsultas en **los dos** compiladores de filtros de ms_flow | `sqlite_utils.compile_filters`, `project._model_filter_expressions` vía `subquery_clause()` |
| `NOT IN` NULL-safe (envoltura sin correlacionar) | `sqlite_utils.compile_subquery_sql(null_safe=True)` |
| Los 4 sitios que materializaban ids → subconsultas | `chemistry/repository.py`, `qsar/jobs.py`, `filtering/sql.py`, `molecules/api.py` |
| Specs de scope en hogar neutral | `amdock/scopes.py::molecule_set_spec`, `prepared_molecules_spec` |
| Payload de clustering por referencia (69 GB → ~300 B) | `selection/jobs.py::_write_packed_fingerprints`, `clustering.PackedFingerprints` |

Techo declarado que **no** se toca en este plan: el task de clustering devuelve `assignments`
como lista (~300 B/mol) porque ese es el contrato de un task. Se resuelve con §3, no antes.

Regla que sigue vigente y gobierna todo lo de abajo:

> **AMDock declara `QuerySpec`. Solo ms_flow traduce a SQL.**
> **Nada se materializa en Python si la db lo resuelve.**
> **La memoria de un job no depende del número total de elementos programados.**

---

## §1 — Normalizar la fuente de chunks (`InputSource` y variantes)

### El problema

Cada herramienta inventó su feed. Hoy conviven cinco formas de hacer lo mismo:

| Herramienta | Feed actual | Estado |
|---|---|---|
| docking | `molecule_scope_spec` + `db_pages` + `iter_docking_batches` | **patrón de referencia** |
| selection | `_scope_spec` + `db_pages` + `batched` | conforme (desde 2026-08-29) |
| qsar | `stream_keyset` + `batched` | conforme (desde 2026-08-29) |
| chemistry | `stream_keyset` + bucle de lotes a mano ×2 | a alinear |
| preparación | `_rows_for_preparation` + `batched` + chunk a mano | a alinear |
| pockets | recorrido propio, sin spec | a alinear |
| import | `iter_import_chunks` (archivos, no db) | caso aparte, ver §4 |

### El patrón único

**Una sola función por herramienta.** El iterador ya existe y es global; no se envuelve:

```python
def scope_spec(params) -> QuerySpec:   # qué filas: todo el WHERE, subconsultas incluidas
```

y el job hace `for batch in batched(db_pages(db, scope_spec(p), page_size=n), n): ...`.

Lo único específico de cada herramienta es **el WHERE**. Iterar y contar no lo son: son
`db_pages` / `db_count`, ya en `ms_flow.query`. `selection` sí tiene hoy
`scope_molecule_rows` / `scope_molecule_count`, pero eso es una conveniencia local (tres
llamantes en `selection/api.py` que comparten `page_size`), **no un patrón a replicar**.

### El trabajo real: hay dos puertas de iteración, no una

Este es el problema de fondo que el §1 anterior no nombraba:

| Puerta | Firma | Quién la usa |
|---|---|---|
| `ms_flow.query.db_pages` | `QuerySpec` (declarativa, serializable) | docking, selection, molecules |
| `ProjectStore.stream_keyset` | modelo SQLModel + `filters` sueltos | qsar, chemistry |

Las dos hacen keyset y las dos consumen memoria constante, así que ninguna está *mal*. Pero son
dos compiladores de filtros distintos — es exactamente la duplicación que obligó a parchear los
dos (`compile_filters` y `_model_filter_expressions`) para las subconsultas. **Converger en
`QuerySpec`**, que es la que la regla exige (declarativa, sin SQL en AMDock, serializable a un
payload). `stream_keyset` se queda como la implementación interna, no como API para las
herramientas.

### El nombre

Los nombres viejos (`project_rows` / `project_count` / `project_pages` / `project_stream`) leían
como si operaran sobre *proyectos de mf*, cuando lo que hacen es leer de la base del proyecto.
Renombrados a `db_rows` / `db_count` / `db_pages` / `db_stream` en `ms_flow/query.py`. Son 81 llamadas en los
tres repos, mecánico, un solo commit.

### Tareas — **§1 CERRADO (2026-08-29)**

| # | Archivo | Acción | Estado |
|---|---|---|---|
| 1.a | `chemistry/jobs.py` | Las dos feeds acumulaban a mano y duplicaban el bloque de vaciado final. Colapsadas en `_chemistry_batches` con `itertools.batched`. ~105 → ~70 líneas. | hecho |
| 1.b | `docking/preparation_jobs.py` | Expone `scope_spec(params, entity_kind=...)`. Documenta por qué su conteo es un **techo** y no un total: el feed descarta además lo ya preparado *en disco*, que no es una consulta. Por eso no declara `total_chunks`. | hecho |
| 1.c | `pockets/jobs.py` | Expone `scope_spec(params)`; el feed pasa de `db_rows` (materializaba todo) a `db_pages`. | hecho |
| 1.d | `qsar/jobs.py`, `chemistry/repository.py` | De `stream_keyset` a `scope_spec` + `db_pages`. **Cierra la segunda puerta**: ya no queda ninguna herramienta sobre el compilador SQLModel. | hecho |
| 1.e | `ms_flow/query.py` + 14 archivos | `project_*` → `db_*`. | hecho |

Estado final: **todas las herramientas declaran `scope_spec(...) -> QuerySpec` y nada más.**
Contar es `db_count(db, scope_spec(p))`; iterar es `db_pages(db, scope_spec(p), page_size=n)`;
trocear es `itertools.batched`. `stream_keyset` queda como implementación interna de ms_flow.

**Check:** `test/test_scope_feed_agreement.py` — por herramienta, `db_count(db, scope_spec(p))`
contra `sum(len(chunk[...]) for chunk in feed(...))`. Si divergen, el scope y el feed no dicen
lo mismo y `total_chunks` quedaría mal declarado.

---

## §2 — Núcleos puros

### Por qué importa (palabras del usuario)

> *"esto es crítico, porque esto está relacionado con desarrollo, mantenimiento, aislamiento, uso
> en jobs multipasos. las funciones puras son independientes de la mecánica de funcionamiento,
> solo les importa el input y output."*

### Referencias que ya cumplen

- `selection/clustering.py` — consume matrices 0/1, devuelve labels + reps + stats. Sin DB.
- `htp/screening.py` — `HTPFilterConfig` + `evaluate_mol`, cadena barato→caro.

### Cerrado

| # | Módulo | Qué se hizo | Estado |
|---|---|---|---|
| 2.a | ~~`pockets/reconcile.py` (nuevo)~~ | Existió y **se borró entero**: la predicción pasó a ser aditiva, así que no hay nada que reconciliar. Ver §3.a. | superado |
| 2.b | `io/rows.py` (nuevo) | `evaluate_ligand_mol` + `ligand_row_fields`: fragmentación, prep y criterios en memoria. `materializers.py` 1290 → 1170 líneas, ya solo escribe — un rechazo tardío cuesta cero ficheros. | hecho |
| 2.c | `test_import_rows.py`, `test_standardization.py` | La comprobación de cada núcleo, en `test/`. (`test_pocket_reconcile.py` se borró con su módulo.) | hecho |

**Corrección sobre 2.c.** El plan pedía un `_demo()` con `assert` dentro del módulo. Se hizo así y
**estaba mal**: este repo tiene pytest y un `test/`, así que meter la comprobación en `src/` deja
código de prueba en el paquete que se instala, y encima duplicaba lo que ya cubría
`test_p2rank.py`. Los tres `_demo()` se convirtieron en tests de verdad y se borraron de `src/`
(`reconcile.py` 166→105, `io/rows.py` 295→232, `standardization.py` 146→96 líneas).

La regla que queda: **la comprobación de un núcleo puro va en `test/`, siempre.** El `_demo()`
solo tiene sentido donde no hay suite. Queda uno pre-existente en `selection/clustering.py`
(líneas 539-613); mismo caso, sin migrar.

---

## §3 — Salidas: `table_sink` y `graph_sink` se quedan separados

**Corrección.** Una versión anterior de este plan proponía unificarlos. Estaba mal, y contradecía
lo que ya se había mirado en el código. Se mantiene la lectura original: **no se unifican.**

Comparten `DbOutputSpec`, sí, pero **el contrato de retorno del task es distinto**:

| | `table_sink` | `graph_sink` |
|---|---|---|
| devuelve el task | `list[dict]` (filas planas) | `dict[str, list[dict]]` (filas por nodo) |
| `DbOutputSpec` | `table`, `columns`, `model_path`, `write_mode`, `conflict_keys` | todo eso vacío; `mode="graph"`, nodos/relaciones en `meta["graph"]` |
| ramas dedicadas | — | `tasking.py:61`, `backends.py:587`, `result_handlers.py:303,310,479` |

Los campos que usa cada uno son **disjuntos**, y el desvío empieza en el propio task. Un
`table_sink(model=..., children=[...])` no borraría ninguna de esas cinco ramas: seguiría
habiendo dos formas de retorno, solo que ahora escondidas tras una firma que finge una sola.
Sería renombrar, no simplificar. Y forzaría a docking a declarar un grafo de un nodo.

Lo que sí falta normalizar es otra cosa:

| # | Acción |
|---|---|
| 3.a | Dar sink a `pockets/jobs.py` — **hecho**, pero no como estaba planteado. Ver abajo. |

### 3.a — pockets sí declara un sink; lo que sobraba era la reconciliación

Este apartado decía antes que era inviable. Lo era mientras la predicción **reemplazara** la corrida
anterior: había que borrar, conservar el `bs_index` de los que seguían, buscar el primer índice libre
para los nuevos y mover el sitio activo si desaparecía el suyo. Nada de eso lo hace un sink, que solo
escribe hacia delante.

La decisión que lo deshizo no fue técnica: **la predicción es aditiva**. Una corrida nueva se pone al
lado de la vieja y es el usuario quien borra lo que no quiere (`pockets.delete_binding_sites`, que
limpia antes `MoleculeRecord.active_binding_site_id` y `ComplexRecord.binding_site_id` para no dejar
punteros colgando). Sin borrado no hay que reservar índices; sin reservar índices no hace falta el
`bs_index`; y sin `bs_index` la identidad de un sitio es su `id` de la db, que es lo que era desde el
principio. Se fueron: `bs_index` y su `UniqueConstraint(molecule_id, bs_index)`,
`MoleculeRecord.active_binding_site_index` (duplicaba la FK que ya existía),
`ComplexRecord.binding_site_index` → `binding_site_id` (FK de verdad), `pockets/reconcile.py` entero
y las 105 líneas de `_replace_receptor_predictions` / `_cleanup_output_dirs`.

`amdock_pocket_prediction_job` declara hoy `output_spec=table_sink(model=BindingSite,
write_mode="bulk")`. El task ya devolvía las filas parseadas, así que el sink las escribe tal cual.
En `finalize` solo queda tirar los directorios de una corrida que falló.

**Lo que hubo que añadir en ms_flow: relaciones diferidas.** El único punto donde el `bs_index` era
imprescindible era la importación: un receptor designa su sitio activo, pero los sitios se insertan
*después* que la molécula (necesitan su `molecule_id`), así que la FK apunta "hacia atrás" y el orden
topológico del `graph_sink` la veía como un ciclo. Ahora una relación puede declararse
`deferred: True`: queda fuera del orden topológico, el `$ref` se guarda durante el insert y se cierra
con **un `UPDATE` por relación dentro de la misma transacción**, ya con los ids de ambos lados. El
payload de importación designa el sitio activo por **posición** en su propia lista de specs
(`materializers._site_ref`), que es el único handle que existe antes de que haya ids.

**Checks:** `tests/test_graph_deferred_relation.py` (ms_flow, mecanismo genérico + que sin `deferred`
sigue siendo un ciclo), `test/test_import_binding_sites.py` (AMDock, el import real cierra
`molecules.active_binding_site_id` y `complexes.binding_site_id`, y el sitio del complejo es el del
**receptor** aunque el `complex_spec` viaje en la fila del ligando), `test/test_p2rank.py` (una
segunda corrida se añade sin tocar la primera ni el sitio activo) y `test_job_output_specs.py`, cuya
lista blanca de jobs sin sink queda ya solo con `amdock_diagram_job`.

**Pendiente:** ninguna vista llama a `pockets.delete_binding_sites` todavía; el modelo aditivo depende
de que el usuario pueda podar corridas viejas desde la UI.

| 3.b | ~~Documentar `file_sink(append/per_batch)`~~ — **ya está hecho**: los dos modos existen, son **mutuamente excluyentes** (`FileOutputSpec` lo valida), están en el docstring de `file_sink` y cubiertos por `tests/test_file_sink_append.py` (6 tests). Ver abajo. |

### `file_sink` y el modo HTP

`file_sink` es el tercer sink: `FileOutputSpec` → `LocalFileBackend`. Es "una fila en la db, pero
en disco" — su entrada es el valor de retorno del task. **Un artefacto por job, no un fichero por
elemento**: los `.sdf`/`.pdbqt`/poses los siguen escribiendo los workers directos al directorio del
proyecto. AMDock hoy no lo declara en ningún job.

| Modo | Qué escribe cada flush | Memoria del handler |
|---|---|---|
| por defecto | reescribe el fichero entero | `_file_history` = todo lo producido → O(N) RAM, O(N²) IO |
| `append=True` | solo su tanda, al final del fichero | nada acumulado |
| `per_batch=True` | `out.000000.sdf`, `out.000001.sdf`, … | solo el manifiesto de rutas |

`append` no admite `fmt="json"` (dos arrays JSON no concatenan) y no combina con `per_batch`.
`per_batch` es la forma natural de un shard de §4/§5.

**Dos huecos antes de apoyarse en esto para HTP:**

1. `_file_batch_index` es un contador del handler que **arranca en 0 en cada run**. Un re-run tras
   crash reescribe `out.000000.sdf` con contenido distinto. Sirve si el re-run recomputa todo;
   rompe en cuanto haya skip-existing por chunk. Para HTP el nombre del shard tiene que salir del
   chunk, no del contador de flush.
2. El manifiesto (`batch_paths`) solo viaja en el recibo del **último** flush y en RAM del handler.
   Nadie lo persiste. Si el resultado del job *es* el conjunto de shards, hay que poder listarlos
   después — glob del directorio, o una fila por shard.

**§3 CERRADO (2026-08-29).**

---

## §4 — Shards con offsets (preparación y resultados intermedios)

### La idea, de dónde viene

De AdaptiveFlow-VS / VirtualFlow, tomando lo bueno y descartando lo malo:

- **Lo bueno**: la unidad de registro es la **colección/shard** (~1000 ligandos), **no** la
  molécula. Así indexan 10⁹–10¹¹ moléculas con ficheros planos. Aplicado a nosotros: un cribado
  de 10⁹ = 10⁶ filas de shard en SQLite (medido: ~1 s, 24 MB). `molecules` no necesita cambiar.
- **Lo malo, que no copiamos**: borran y comprimen en tar por presión de espacio y de inodos. Un
  `.tar.gz` no se puede mapear. Nosotros queremos indexación memmap **sin** compresión.

### El formato

Dos casos, dos formatos, nada más:

| Dato | Formato | Por qué |
|---|---|---|
| Ancho fijo (huellas, vectores de descriptores) | `.npy` bit-empaquetado + memmap | medido: 0,22 s escritura / 256 MB / 18 ms para 10k filas al azar sobre 1M×2048 |
| Ancho variable (PDBQT, SDF, salidas de texto) | fichero plano concatenado + `.idx.npy` de offsets int64 | `read_record_span` ya lo lee; sin compresión, acceso O(1) |

Arrow IPC queda solo para tablas de resultados con columnas mixtas. `pkl` se descarta: no es
mapeable y ata el formato a la versión de Python.

### La regla que lo hace funcionar sin locks

> **Frontera de shard = frontera de chunk.**

El worker es dueño de su shard: escribe su `.pdbqt` concatenado y su `.idx.npy`, y nadie más los
toca. Sin lock, sin cola central, sin merge. Y **33 M de ficheros individuales no existen nunca**
— que era la objeción real de meter un fichero por molécula.

El *commit* de un shard es un `rename` de `shard-00042.incomplete` a `shard-00042` (atómico en el
mismo filesystem). Un shard a medio escribir por una caída nunca se lee.

### Tareas

| # | Acción |
|---|---|
| 4.a | Primitivas de shard: `write_record_shard(path, records) -> (data_path, idx_path)` junto a `iter_record_spans`/`read_record_span` en `io/parsers/readers.py`. |
| 4.b | Preparación (`docking/preparation_jobs.py`) escribe shards en vez de un `.pdbqt` por molécula. El chunk de docking recibe `(shard_path, idx_path, rango)`. |
| 4.c | `engines.py:474` (ruta python de Vina): `vina_obj.set_ligand_from_string(pdbqt)` en vez de `set_ligand_from_file`. Quita el temporal por completo. **Una línea.** |
| 4.d | `engines.py:287` (ruta binaria): el temporal se queda, pero es una escritura *movida* (~3 KB a tmpfs frente a un dock de segundos). No se toca. |
| 4.e | Verificar antes de 4.b si gnina/smina aceptan multi-ligando por stdin. Si sí, misma ruta que 4.c. |

**Check:** escribir un shard de N registros, releer cada uno por `read_record_span` y comparar
byte a byte con el original; y matar el proceso a mitad para comprobar que el `.incomplete` no
se lee.

---

## §5 — Modo HTP

### Decisión del usuario, literal

> *"es mejor separar el modo htp que tiene características diferentes que adaptar todo para ese
> modo que se usará relativamente poco."*

Así que **no** se toca `molecules` ni el modo normal. HTP es un camino aparte que converge con el
normal en un solo punto.

### Arquitectura: un embudo con punto de promoción

```
  entrada 10⁶–10⁹ mols
        │
        ▼
  [shards en disco]  ← §4. La db NO ve moléculas individuales.
        │             Registro en db = 1 fila por shard (~10⁶ filas para 10⁹ mols).
        ▼
  [filtro HTP]  ← htp/screening.py, barato→caro (§2 lo tiene ya)
        │
        ▼
  [docking rápido sobre shards]
        │
        ▼
  ══ PUNTO DE PROMOCIÓN ══   los hits (10³–10⁵) entran en `molecules` como filas normales
        │
        ▼
  modo normal: rescoring, PLIP, QSAR, selección, UI
```

Lo que se registra en la db durante la fase HTP es el **shard**, no la molécula:

| Columna | Para qué |
|---|---|
| `shard_id`, `path`, `idx_path` | dónde está |
| `n_records` | cuántas moléculas lleva |
| `state` (`pending`/`running`/`done`/`failed`) | reanudación e idempotencia |
| `source_ref` | de qué fichero/lote salió |

La identidad de una molécula en fase HTP es `(shard_id, record_index)`, no un `molecules.id`.
Un `molecules.id` solo se crea al promover. Esto es coherente con lo ya acordado: **hash de
contenido = identidad, `id` de db = localidad**.

### Tareas

| # | Acción |
|---|---|
| 5.a | Tabla `screening_shards` + su repository con `QuerySpec` (nada de SQL a mano). |
| 5.b | Feed HTP = `db_pages` sobre `screening_shards` con `state='pending'`. Mismo patrón del §1: el scope es un WHERE. |
| 5.c | Idempotencia por shard: un shard `done` no se recalcula. Es el mismo patrón "¿existen ya las salidas?" que `docking-skip-existing`, subido de granularidad. **Necesario**: MF ya no lleva recibos por chunk. |
| 5.d | Promoción: job que toma los hits de N shards y los inserta en `molecules` por el importador normal. A partir de ahí, todo el resto de AMDock funciona sin saber que hubo HTP. |
| 5.e | La UI de HTP muestra shards y agregados, no una tabla de 10⁹ filas. |

### Lo que NO se hace aquí

Los dos casos son **aplazamiento, no incompatibilidad**:

- **Ray / HPC multinodo.** Ray ya es un backend de ms_flow (`core/executor/ray_adapter.py`,
  `backend_selector.py`), y el diseño de shards lo favorece: el payload de un chunk es una ruta,
  no datos, que es justo lo que un backend distribuido necesita. Lo que falta no es arquitectura
  sino operación — sistema de ficheros compartido y envío de lotes a varios nodos. Se hace
  después de 5.a–5.c porque hasta entonces no hay shards que repartir.
- **Esquema de `molecules`.** No se toca precisamente porque no hace falta: `screening_shards`
  absorbe la escala y solo lo promovido llega a `molecules`. Si el embudo funciona, `molecules`
  nunca ve 10⁸ filas y sus 40 campos no son un problema. Es la decisión de "separar el modo HTP"
  llevada a su conclusión, no una limitación pendiente.

---

## §6 — Clustering como map/reduce (cuando 5 exista)

Hoy el clustering es un job de un chunk que referencia un `.npy` — correcto y suficiente. Cuando
haya que clusterizar por encima de lo que cabe en un nodo, la descomposición es la que ya está
documentada en `bitbirch_lean_parallel`:

1. Job A: clusteriza cada shard por separado (paralelo real, un chunk por shard) y emite centroides.
2. Job B: clusteriza los centroides de A.

Se encadenan con `submit_job(depends_on=[job_a])`, **no** con `finalize`: `finalize` corre en el
proceso del manager y no lo programa el planificador de tokens CPU/GPU. No hay DAG de tareas
dentro de un job; `depends_on` es el mecanismo nativo (`job-auto-dependencies`).

---

## Orden de ejecución sugerido

```
§1 (mecánico, desbloquea el resto)
 └─ §2 (núcleos puros; 2.b es precondición de §4)
     └─ §3 (sinks; 3.b necesita 2.a)
         └─ §4 (shards; 4.c es independiente y se puede hacer ya)
             └─ §5 (HTP)
                 └─ §6 (clustering distribuido)
```

`4.c` (`set_ligand_from_string`) no depende de nada y es una línea: se puede hacer en cualquier
momento.

## Referencias

- `docs/plan_acceso_datos_2026-08-27.md` — fases 1 y 2, cerradas.
- Memoria: `subquery-scopes-everywhere`, `clustering-payload-by-reference`,
  `htp-file-centric-plan`, `job-auto-dependencies`, `docking-skip-existing`,
  `declared-total-chunks-is-a-floor`, `mf-domain-idempotency-contract`.
