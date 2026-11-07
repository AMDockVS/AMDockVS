# Plan: transporte de datos y generaciones de shards

Cierre de la discusión VS/HTPVS. Tres fases independientes; cada una se puede
parar sin dejar el árbol roto.

## Fase 0 — transporte (ms_flow)

Dos correcciones puntuales, ningún diseño nuevo.

**F0.1 — `core/data/planner.py`: el FS compartido gana al cache.**
El orden actual devuelve `ray_object_transfer` en cuanto `spec.cache` es cierto,
antes de mirar `profile.shared_fs`. Resultado: con FS compartido el receptor se
transfiere por bytes aunque el fichero sea visible en el nodo. La comprobación de
`shared_fs` pasa delante; `cache` sigue forzando la transferencia sólo cuando no
hay FS compartido.

**F0.2 — `core/data/bridge.py`: deduplicar los inputs de HPC.**
`_stage_file_to_hpc` hace `shutil.copy2` a `<wdir>/molsuite_staging/<job>/<chunk>/`,
o sea una copia **por chunk**: el receptor viaja 1000 veces en una campaña de 1000
shards. Los inputs pasan a un directorio direccionado por contenido lógico,
`<wdir>/molsuite_inputs/<clave>/<nombre>`, y se copian sólo si no están.

El scratch por chunk **se queda como está**: está aislado (dos chunks no pisan el
mismo nombre relativo), se limpia borrando el árbol del job, y es la convención
del scheduler. No se espeja el proyecto en el wdir: eso exigiría definir un ciclo
de vida remoto (quién lo crea, quién lo recoge, qué pasa con dos campañas en el
mismo wdir) y crearía una segunda copia autoritativa que reconciliar.

Misma forma que el cache de nodo de Ray (`ms_flow_ray_cache/<digest>/<name>`):
un solo modelo mental para los dos backends.

**Fuera de alcance, anotado:** reutilizar *salidas* entre pasos de HPC (preparar
shards allí y dockearlos sin que vuelvan). Eso no es trabajo del staging sino de
la generación de shards: una generación con ubicación remota. Se deja para
cuando el HPC encadene dos pasos de verdad.

## Fase 1 — identidad dentro del registro (AMDock)

Para que el modo *fire and forget* funcione, la identidad tiene que viajar dentro
del registro, no en una tabla paralela. Se verificó que `_Name` y las etiquetas
SDF sobreviven a `standardize → protonate → generate_3d`. El agujero está en
`io/shards.py::mol_from_record`: en la rama SMILES toma sólo `tokens[0]` y tira el
nombre. Se recupera como `_Name`.

## Fase 2 — generaciones de shards (AMDock)

El problema: cada paso que reescribe shards tiene que renumerar (el mapa de slots
es `uint16`, `MAX_RECORDS = 65535` limita el *span de ids* de un shard, no su
número de registros). Renumerar sin linaje pierde la trazabilidad; hoy además
`shard_chemistry_task` hace `upsert` sobre `(source, shard_index)` **por chunk**,
así que el inventario queda a medio conmutar si el job falla a mitad.

Modelo, con ventana de dos:

- `ShardGeneration(id, parent_id, step, shard_dir, job_id, is_active, created_at)`.
- `ScreeningShard.generation_id`; la unicidad pasa a `(generation_id, source, shard_index)`.
- `shard_scope_spec` inyecta **siempre** la generación activa. Es lo único que
  impide que un paso lea shards de dos generaciones a la vez.
- La conmutación es un `UPDATE` de un puntero, no 10k DELETE + 10k INSERT.
- Al activar la nueva generación, el **abuelo** se olvida: filas borradas y
  ficheros barridos. Se conservan la activa y su padre.

La escritura y la conmutación van en un *result handler* (`ShardGenerationWriter`),
igual que `ShardQueueWriter` en el import: crea la generación al construirse,
inserta filas por chunk, y conmuta el puntero en `flush()` — que MF llama una sola
vez cuando el job termina (`services/job_status.py:334`). Eso mata el upsert a
medio camino: o el job termina y la generación entra entera, o no entra.

**Ids:** los del shard son locales y densos. La conciliación contra la db va por
`source_indices` en los metadatos del contenedor. Un shard empaquetado desde filas
de la db **no** puede conservar los ids originales en general: 10k supervivientes
cuyos ids van del 3.000 al 2.000.000 dan un span de 2M y revientan el `uint16`.
Un solo camino: renumerar denso, llevar la lista en los metadatos.

`records_per_shard` se queda en 1000.

## Lo que este plan NO toca

La unificación del iterador (`iter_batches` sobre `DbStore`/`ShardStore`/`FileStore`),
la unificación de las tools bajo un botón con checkboxes, y el `ResultSink` de
docking. Son las fases A–E discutidas (~1500 líneas) y dependen de que las
generaciones existan primero.
