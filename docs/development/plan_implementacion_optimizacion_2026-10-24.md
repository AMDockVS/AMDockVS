# Plan de implementación: SQLite delgado, química de importación y shards Parquet (2026-10-24)

Continúa `plan_optimizacion_almacenamiento_2026-10-22.md` (partes A y C). Ya está en `db/sqlite`:
índices diferidos (C.1), y de la fila delgada (C.2) PAINS / InChIKey / SMILES canónico bajo
demanda y la regla D6 (sin molblock 2D con entrada SMILES). Este plan cubre lo que falta.

Trabajo en las ramas `db/sqlite` de AMDockVS, ms_flow y ms_components, con un commit por
punto y sin ramas por funcionalidad. Las ramas `db/duckdb` se quedan para volver a probar
DuckDB 2.0.1.

Medidas de hoy:
- Proyecto `s1`: 3.854.288 SMILES lead-like importados en `db/sqlite`.
  - `molecules` ocupa 1055 MB y sus índices 517 MB.
- Perfil de la importación por filas: 5000 SMILES de `DGAA.smi`, un núcleo.

---

## Fase 1. SQLite delgado: columnas e índices

### 1.0 Qué contiene cada columna en `s1`

| Columna | Contenido en s1 | Bytes/fila (aprox.) | Propuesta |
|---|---|---:|---|
| `created_at`, `updated_at` | texto ISO; las dos iguales en todas las filas | 52 | ver 1.1 |
| `source` | ruta absoluta del fichero; 168 valores distintos | 57 | ver 1.2 (pasa al lote) |
| `input_format` | constante `smiles` | 6 | ver 1.2 (pasa al lote) |
| `current_payload_id` | igual a `stored_payload_id` en el 100 % de las filas | 7 + índice 46 MB | 1.3 |
| `pains_matches` | JSON `null`, 4 B | 4 | NULL de SQL |
| `extra_data` | `{}` en todas las filas | 2 | NULL de SQL |
| `ro5_violations` | lista JSON calculada en la importación | 2+ | 1.4: expresión SQL, sin columna |
| `molecule_type`, `usage_class`, `primary_context` | constantes (texto) | 28 | se quedan (ver 1.6) |
| `properties_payload_id`, `current_model_index`, `active_binding_site_id`, `inchikey`, `canonical_smiles` | todo NULL | ~0 | se quedan; el índice de `properties` sí cuesta (1.5) |
| booleanos (8) | 0/1 | 0 (SQLite guarda 0 y 1 solo en la cabecera) | nada |
| `n_atoms` = `heavy_atom_count` | iguales en el 100 % de las filas | 2 | coinciden solo porque la entrada SMILES no trae H; ambos se quedan |

El total estimado ronda **~130 B de ~274 B por fila (≈45 %)**. La cifra sale de las
longitudes en texto; la real se mide con el banco (1.8).

### 1.1 Fechas fuera de `molecules`

- Lectores actuales de `molecules.updated_at`:
  - `molecules.excluded_since(since)`, que usan los diálogos de fallos de preparación
    (`preparation_panel.py:678`) y de Build (`build.py:755`);
  - el orden por defecto `updated_at` de la vista de preparación (`preparation_panel.py:81`).
    Hay que comprobar si esa vista lee `molecules` o la tabla de entradas de docking.
- Propuesta: el job que excluye devuelve los ids que excluyó, como salida del job o como
  `exclusion_reason` con el id del job. El diálogo los lee de ahí, y `excluded_since`
  desaparece.
- `created_at` pasa al lote de importación (1.2).
- Resultado: `MoleculeRecord` deja de heredar `TimestampedRecord`, que hoy solo usa
  `molecules`.

### 1.2 Tabla de lotes de importación

Nueva tabla `import_batches(id, source, input_format, created_at, …)`. En `molecules`,
`source`, `input_format` y `created_at` se sustituyen por `import_batch_id`, un entero de
1-2 B.
- Ahorro: ~90 B por fila.
- Los lectores de `source` (detalles, re-corte de fragmentos, exportar) hacen JOIN o leen el
  lote.
- Las moléculas que nacen fuera de una importación (edición, Build, copia) apuntan a un lote
  sintético del job que las creó.

### 1.3 `current_payload_id` NULL significa "igual que el almacenado"

- Se escribe solo cuando una transformación lo cambia.
- Los lectores (51 referencias en 26 ficheros) pasan a usar `coalesce(current, stored)`
  mediante una sola función o propiedad, para que no quede repartido.
- `PayloadStore.release/check` sigue mirando los dos campos.

### 1.4 Ro5 sin columna

`ro5_violations` es aritmética sobre `mw`, `logp`, `hbd` y `hba`. El filtro la expresa en SQL y
la UI la calcula al mostrarla. Era el punto C.2-4 y aún no está hecho.

### 1.5 Índices

| Índice | Tamaño en s1 | Propuesta |
|---|---:|---|
| `ix_molecules_usage_class` | 62 MB | retirar: `idx_mol_role_scope` empieza por `usage_class` |
| `ix_molecules_current_payload_id` | 46 MB | parcial `WHERE current_payload_id IS NOT NULL` (con 1.3 queda casi vacío) |
| `ix_molecules_properties_payload_id` | 35 MB, columna toda NULL | parcial `WHERE … IS NOT NULL` |
| `ix_molecules_name` | 67 MB | decidir (D-S3): solo lo usan el orden por nombre de la tabla y `binding_sites` (receptores) |
| `idx_mol_role_scope` | 128 MB | se queda (conteos de rol) |

Ahorro: ~140 MB de 517 sin tocar `name`.

### 1.6 Lo que NO se toca

- `molecule_type`, `usage_class` y `primary_context`: son mutables o se filtran, y pasarlos
  a códigos enteros toca más de 35 lectores para ganar unos 28 B.
- Los booleanos.
- Las columnas NULL: cuestan ~1 B.

### 1.7 ms_flow: `ms_payloads` (126 MB + 40 MB del índice `volume_id`)

En s1, `kind` y `volume_id` tienen un solo valor, y `part_offset`/`part_length` son NULL.
La ganancia es pequeña (los NULL ya son baratos). Queda anotado, sin construir, salvo que el
banco diga lo contrario.

### 1.8 Migración y medida

- Los proyectos nuevos se crean con el esquema delgado.
- En los existentes, decisión D-S2:
  - **(a)** dejar las columnas viejas sin usar, que no cuesta nada;
  - **(b)** migrarlos con `ALTER TABLE DROP COLUMN` tras quitar sus índices, lo que reescribe
    la tabla.
- Banco: `bench_import_scale.py` con 1M SMILES (el disco tiene 7,8 GB libres, así que 4M no
  cabe). Se mide B/fila, MB de índices, tiempo de importación y tiempo de página de la tabla.
- Pruebas: las suites actuales y una prueba de "fila delgada" que fije qué columnas escribe
  una importación de librería.

---

## Fase 2. Química de la importación

### 2.0 Perfil de hoy: 945 µs/mol, frente a 4522 antes de C.2

| Parte | µs/mol | Comentario |
|---|---:|---|
| Crippen logP (`CalcCrippenDescriptors`) | ~243 | primera percepción de tipos de átomo; básico, se queda |
| Bucles Python sobre átomos (`chemistry/state.py`, `molecules/fragments.py`) | ~280 | ~130 iteraciones por molécula desde Python |
| `Path.resolve()` por fila (`core/paths.py:42` y `molecule_row`) | ~106 | 14 `lstat` por molécula |
| Parseo (`_parse_smiles_line` + `MolToSmiles` no canónico) | ~130 | necesario; ver 2.4 |
| HBA/HBD y resto de descriptores | ~120 | básicos |

### 2.1 Bucles de átomos a C++ (estimado: −250 µs)

- `mol_has_explicit_hs` → comparar `GetNumAtoms()` con `GetNumHeavyAtoms()`, o devolver
  `False` directamente con entrada SMILES.
- `_has_metal_atoms`, `_is_simple_ion` y `_has_carbon` → un patrón SMARTS precompilado y
  `Chem.GetFormalCharge`.
- La clasificación de componentes se salta cuando hay un solo componente. El commit 986c149
  ya lo intenta, pero el perfil sigue mostrando 23 llamadas por molécula; hay que revisar
  quién las hace.

### 2.2 Resolver la ruta una vez por lote (estimado: −100 µs)

`source_file.resolve()` se calcula al crear el lote, no en cada fila. Con 1.2 la ruta además
deja de viajar en la fila.

### 2.3 Descriptores secundarios bajo demanda

`ring_count`, `aromatic_ring_count`, `hetero_atom_count`, `formal_charge`, `fraction_csp3` y
`fragment_count` (C.2-2, sin hacer) se calculan con el job `compute_descriptors` que ya existe
y que el filtro lanza cuando los necesita.
- Ahorro: poca CPU (~45 µs, porque comparten la percepción) y ~6 B por fila.
- Es la misma regla que PAINS: NULL = no calculado.

### 2.4 Guardar el SMILES de entrada tal cual

Hoy se reescribe con `MolToSmiles(canonical=False)` después de parsearlo. Si la entrada ya es
un SMILES válido y no hubo corte de fragmento, se guarda el token original. Hay que medir
antes de decidir; si gana menos de 20 µs, no se toca.

### 2.5 Qué más puede salir de la fila hacia volúmenes o shards

- Con entrada SMILES ya no queda nada: el molblock se fue con D6, y `fragmentation` solo se
  guarda con más de un componente.
- Con entrada SDF hay que verificar si las etiquetas del SDF se guardan dos veces: en el
  payload SDF y en el sidecar de propiedades. Si es así, se elige una sola copia.
- Los cientos de descriptores para QSAR ya viven en `descriptor_blocks` (JSON), bajo demanda.

### 2.6 Fuera de la importación

Build (protonar, 3D, minimizar) y la preparación de docking (meeko) se perfilan igual, con
1000 ligandos. Se escriben propuestas solo con el perfil en la mano. Meeko se queda (decisión
del 2026-10-13).

**Objetivo de la fase:** ~945 → ~550 µs/mol con entrada SMILES, medido con el mismo perfil y
con `bench_import_scale.py` a 1M.

---

## Fase 3. Shards en Parquet

### 3.0 Contenedor: hecho en el árbol de trabajo, sin commit

- ms_flow, `core/data/shard.py`:
  - misma API (`ShardWriter`, `Shard`, `Record`); el fichero es Parquet `id uint64` +
    `record`, con la cabecera en los metadatos `ms_flow.shard`;
  - zstd y checksums de página;
  - escritura atómica (`.tmp` + fsync + `replace`).
- AMDockVS:
  - `SHARD_SUFFIX = ".parquet"`;
  - se borraron la lectura heredada de los fragmentos de resultados gzip-JSON y el alias
    `RESULT_SHARD_FORMAT`;
  - tests y docs actualizados.
- Un `.mshard` viejo se rechaza con "re-import the library" (p1 hay que reimportarlo).
- Suites:
  - ms_flow: 262 passed;
  - AMDockVS: 645 passed y 2 failed. Los dos fallos (`test_offtarget_colors`,
    `test_results_pivot`) vienen del COUNT en segundo plano de ms_components (`bac4d85`);
    con ms_components anterior a ese commit pasan los 42 tests de esos ficheros.

### 3.1 Granularidad del docking

Hoy un shard (hasta 65.535 registros) por receptor es una tarea (`shard_jobs.py:221`). Con
Parquet, un rango de ids o un grupo de filas se lee sin abrir el resto. La propuesta es que la
tarea sea `(shard, rango)` con un tamaño configurable, de modo que una librería de pocos shards
reparta trabajo entre todos los núcleos.

### 3.2 Descriptores dentro del shard (opcional, D-S5)

Un paso "anotar" escribe en el Parquet del shard las columnas `mw`, `logp`, `hbd`, `hba`,
`tpsa`, `rotatable_bonds` y `heavy_atom_count`. Así el filtro HTP y los conteos se hacen sobre
`shards/*.parquet` sin RDKit y sin pasar por `molecules`. La importación de shards sigue sin
química (5,4 s por 100k); anotar es un job aparte.

### 3.3 Capa de consulta

- `pyarrow.dataset`, que ya es dependencia, para conteos, filtros y detalles del catálogo.
- DuckDB en memoria solo si una consulta lo necesita (D8 sigue abierta). No se abre ningún
  fichero DuckDB propio.

### 3.4 Fragmentos de resultados pequeños

Hay un `.results.parquet` por cada par (shard, receptor). Con miles de ficheros el filtro se
vuelve ~20× más lento (B.7). Propuesta: compactar al terminar la campaña en un Parquet por
receptor, con grupos de filas de ~256 KB. Los `.failures.jsonl` pasan a ser filas con
`status=failed` en el mismo fragmento.

### 3.5 Banco

La etapa `app_mshard` de `bench_storage_formats.py` se rehace con el contenedor Parquet. Se
miden importación, disco y lectura en el docking, con 100k y 1M.

---

## Orden y decisiones

1. **Commit de 3.0 primero.** Ya está hecho y probado. Si se deja en el árbol de trabajo,
   choca con los cambios de las fases 1 y 2.
2. Fase 1: 1.5 → 1.3 → 1.4 → 1.1 → 1.2. Va de menor a mayor radio de cambio, y cada paso es
   un commit con su medida.
3. Fase 2: 2.2 → 2.1 → 2.3 → 2.4. Antes de cada cambio, se mide el perfil.
4. Fase 3: 3.1 → 3.4 → 3.2/3.3 según D-S5.

| # | Decisión |
|---|---|
| D-S1 | ¿Fuera `created_at`/`updated_at` de `molecules`, con el job devolviendo los ids que excluye? |
| D-S2 | Proyectos existentes: ¿columnas viejas sin usar (a) o migración con `DROP COLUMN` (b)? |
| D-S3 | ¿Se queda `ix_molecules_name` (67 MB en 3,85M) para ordenar por nombre? |
| D-S4 | ¿Tabla `import_batches` (1.2)? Es el cambio más grande de la fase 1. |
| D-S5 | ¿Descriptores básicos dentro del shard Parquet (paso "anotar")? |
| D-S6 | ¿Commit del contenedor Parquet (3.0) antes de empezar la fase 1? |

Respuestas de Mario (2026-10-24):
- D-S1: se quitan las fechas de todas las tablas, salvo las de los jobs.
- D-S2: no se migra nada; los cambios valen para proyectos nuevos.
- D-S3: `ix_molecules_name` se quita. Medido en s1 sin el índice: búsqueda exacta por nombre
  0,46 s y orden por nombre 0,49 s por página.
- D-S4: se hace `import_batches`, pero antes se presenta su diseño.
- D-S5: recomendación, no por ahora.
- D-S6: commit en `db/sqlite` hecho (ms_flow 2eb07d2, AMDockVS fa4ff3e).
