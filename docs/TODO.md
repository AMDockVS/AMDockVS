# TODO

## MoleculeFragment

- UI fragment selection: no hay forma de cambiar de fragmento desde la UI; no está claro que haga
  falta (anotado 2026-10-14). La API existe: `molecules.select_fragment(id, index)` recorta el
  fragmento del original importado (`stored_path`) y lo escribe solo entonces. Los fragmentos ya no
  se materializan al importar; `extra_data.fragmentation.components` lista `{fragment_index, role, smiles}`.
- Deduplicación manual (columnas `inchikey` / `canonical_smiles` ya existen; no para shards).
  Proyectos importados antes del 2026-10-14 tienen esas columnas vacías: hace falta un backfill.

## Docking programs

### Lista
- Autodock Vina (Vina + ad4, version nueva)
- AutodockGPU
- SMINA
- GNINA
- DOCK6
- LeDock
- RxDock
- DiffDock

### Requerimientos
#### Autodock Vina
- receptor pdbqt
- ligand pdbqt
- box

#### AutodockGPU
- receptor pdbqt
- ligand pdbqt
- box

#### SMINA (algo viejo y desactualizado, lo dejo por ahora)
- receptor pdbqt
- ligand pdbqt
- box

#### GNINA
- receptor pdbqt
- ligand pdbqt
- box

#### DOCK6 (demasiado complejo por ahora)
- parameters
- receptor
  - Spheres file (*.sph)
  - Energy Grid (*.nrg, *.bmp, *.cnt)
  - box in pdb
- ligand mol2

#### LeDock
- receptor pdb
- ligand mol2
- box

#### RxDock (se ve robusto pero bastante complicado, lo excluyo por ahora)

#### DiffDock
- receptor sequence (pdb optional)
- ligand smiles


## Preparación de receptores (anotado 2026-09-01)

Tres bugs verificados con 4UWH (p2). Los tres **reparados 2026-09-01**: el escritor de
`io/receptor_preview.py` ya no aplica su lista blanca ciega, y qué llega al PDBQT se decide
ahora en *Docking Studio > Receptors* ("Include structural waters" / "Include cofactors",
apagadas por defecto). Cubierto por `test/test_receptor_hetero.py`.

1. ~~**`remove_cofactors` no hace nada** — `io/receptor_preview.py:315`~~ **DONE 2026-09-01**
   `_components_to_remove()` ya decidía bien, pero el escritor aplicaba después
   `if not _is_polymer_residue(resname) and resname not in _COORDINATION_METAL_CODES: continue`,
   así que los cofactores se borraban siempre, marcada o no la casilla. Meeko los prepara sin
   problema: probado con JXM (30 átomos) → PDBQT con 31. *Reparado:* el filtro es ahora "lo que
   no está en `component_keys`", y clasifica por resname (no por `residue_key`: una assembly
   biológica renombra las cadenas copiadas).

2. ~~**`remove_non_structural_waters` no hace nada** — `io/receptor_preview.py:317`~~ **DONE 2026-09-01**
   Se borraban **todas** las aguas, incluidas las estructurales que la preview marca como
   `is_selected`. Meeko sí las admite: con coordenadas reales salen al PDBQT como
   `OW/HW1/HW2` tipo `OA/HD` y cargas TIP3P (-0.412 / +0.206). Ojo al depurar: aguas con
   coordenadas inventadas que solapan la proteína hacen que Meeko falle con
   `adjacent_mol doesn't contain the mapped atoms` (enlaces inter-residuo espurios), no es un
   problema de soporte de agua.

3. ~~**`retained_structural_waters` miente y nadie lo consume** — `io/receptor_preview.py:331`~~
   **DONE 2026-09-01 a medias**: ya no miente (con (2) reparado, las aguas retenidas están de
   verdad en el fichero), pero el campo **sigue sin consumidor**. Borrarlo cuando se decida que
   nadie lo va a leer.

### Pendiente relacionado (no es bug): protonación consciente de metales
pdb2pqr 3.7.1 no ve los metales (PROPKA trabaja solo sobre el biomolecule). Para congelar el
estado de los residuos coordinantes hay dos palancas ya comprobadas:
- renombrar el residuo en el input: `HID/HIE/HIP`, `CYM`, `ASH`, `GLH`, `LYN` (probado:
  HIS A:370 → HIE sale HIE y las otras 153 HIS siguen HIS);
- `-i/--titrate_only "A:10,A:11"` para limitar qué residuos son titulables.
La detección geométrica ya existe (`receptor_preview.py:518`: ≥2 donantes a ≤2.8 Å del metal).

## Diagramas y SMILES (anotado 2026-09-16)

- Setting para exportar imágenes de diagramas (PNG/SVG): subproceso prioritario en los 2 CPUs lógicos de reserva vs job diferido en MF. Hoy: JSON en proceso, imagen siempre por subproceso (`export_diagram_image`). Pendiente de decidir; no se implementa todavía.
- La columna `smiles` no se actualiza cuando cambia la química (protonación, tautómero, 3D, etc.). Decidir: refrescarla en cada paso o generarla al vuelo desde el SDF guardado. Para salidas de docking vale el SMILES de meeko.
- Exportación de diagramas (ms_contactmap `export.py`), solo nota; hoy está bien así:
  - HECHO 2026-09-16: SVG sin raster (ligando incluido) y `export_png(dpi=...)` / `--dpi`. AMDockVS sigue usando `scale=2` (192 dpi); decidir si pasa `dpi=300`.
  - El SVG de `QSvgGenerator` pesa ~370 KB (cinta = cientos de segmentos, gotas con 4 capas de sombra, ligando re-pintado). Solo importa para web; si hace falta, pensar un modo de render eficiente.

## Interacciones en la tabla (anotado 2026-10-08)

Decidido por Mario; no se trabaja hasta cerrar el motor de orquestación (ms_flow).

- Las interacciones viven en `interaction_results`. Hoy la casilla del docking (`compute_diagram`) solo deja `<pose>.rank<N>.diagram.json` + imagen junto a la pose y la tabla queda vacía; solo la llena `amdock_interactions_job`.
- Docking con la opción marcada: una sola task que devuelve filas para `docking_results` e `interaction_results` por un `graph_sink` (la fila hija lleva la referencia a su pose y el sink pone `docking_result_id`).
- Decidido 2026-10-17: `interaction_results` se identifica por (payload del receptor, payload de la pose del ligando), no por `docking_result_id`. Así un complejo de referencia (cocristal, sin fila de docking) tiene su diagrama con la misma tabla, sin otra estructura. El docking escribe la fila con los payloads que ya conoce; no hace falta la relación del `graph_sink` hacia `docking_results`.
- No se filtra por interacciones (consultas tipo "poses con puente de H a ASP86"): solo se guardan y se muestran. Quizás más adelante; no se diseña para ello ahora.
- La imagen no se genera en el docking: el diagrama se dibuja en la UI a partir de las interacciones. Si algún día hace falta, se añade.
- El panel de resultados lee las interacciones de la tabla (`list_interactions`), no del JSON (`pose_interactions`, `ui/catalog/results.py:864`): el fichero puede no estar.
- El botón "Build diagram" y `amdock_diagram_job` detectan y no escriben en la tabla: arreglar.
- Depende de ms_contactmap (en desarrollo): entrada solo de detección y SASA más rápido. Medido el 2026-10-08 en una pose (1iep): detección 0,04 s; exposición 3,0 s; layout 0,15 s; imagen 1,8 s. El job de interacciones paga hoy esos 3 s porque llama a `build_pose_diagram` (`docking/results/interactions.py:57`).

## Editar un receptor ya importado (anotado 2026-10-15)

- Hoy cadenas, ensamblaje, altloc, aguas y cofactores quedan fijados al importar; no hay acción
  para cambiarlos. Reimportar el mismo archivo con otras opciones crea una variante nueva (otra
  clave de almacenamiento, `_receptor_import_variant`), no reemplaza. Los binding sites sí se
  editan después (herramienta Binding Sites).
- Todo lo necesario para reprocesar ya se guarda: la copia del original (`original_path`) y las
  opciones usadas (`extra_data.structure.import_profile`).
- Propuesta: acción de fila "Edit import…" que abre el panel de importación con esa fila
  rellenada; al aplicar, "Edit a copy" (variante nueva) o "Replace" (mismo patrón que el editor
  Ketcher de ligandos). "Replace" avisa de que se borran dockings, binding sites y preparaciones
  que dependen del receptor.

## Duplicados en Ligands (anotado 2026-10-17)

- Hoy el import no detecta duplicados: el mismo compuesto importado dos veces son dos filas y se
  dockea dos veces (pasó con datos duplicados a propósito para hacer crecer la tabla).
- No comprobarlo en el import (coste por molécula en el camino caliente). `molecules.inchikey`
  ya existe y tiene índice: detectarlos después es un `GROUP BY inchikey HAVING count(*) > 1`.
- Forma propuesta: acción "Find duplicates" en Ligands que filtra/selecciona las repetidas
  (como un scope), y el usuario decide borrar o conservar. Opcional: aviso al terminar el import
  si la consulta encuentra alguna. Sin implementar.

## Cross-docking (anotado 2026-10-17)

- Redocking dockea cada ligando de referencia en SU receptor (el par original del complejo).
  Cross-docking: cada ligando de referencia contra los receptores de OTROS complejos del mismo
  blanco, para medir si el protocolo reproduce poses con una conformación de proteína ajena.
- Lo que falta: elegir el conjunto de receptores (mismos binding sites / misma proteína),
  superponer cada receptor sobre el del complejo original para que el RMSD contra el ligando de
  referencia tenga sentido (hoy el RMSD se mide en el marco del receptor dockeado), y una vista
  de resultados en matriz ligando × receptor (éxito ≤ 2 Å).
- Encaja como otro `run_kind` sobre el mismo motor de docking. Sin implementar.

## Almacenamiento del docking (anotado 2026-10-19)

Sale del benchmark `docs/benchmarks/vs_classic_2026-10-18/`. Hoy son 27 KB por ligando con 9 poses
(~890 GB para 33 M). Por pose, `docking_results.metrics` lleva 1199 B de JSON: 551 B del run, 352 B
del ligando, 247 B derivados del score y solo 49 B que son de la pose.

- **Configuración del run una sola vez:** caja, protocolo, engine, backend, exhaustiveness,
  `run_id`, fecha y receptor van en una tabla de runs (o en un volumen si es grande), y cada fila
  la referencia por id.
- **Propiedades del ligando fuera de `docking_results`:** MW, TPSA, cLogP, HBD/HBA y átomos pesados
  ya están en `molecules`. Las 9 energías del ligando se repiten en cada una de sus poses.
- **Derivados del score (LE, pKi, BEI, SEI, LLE…):** se quedan, porque se usan para ordenar y
  filtrar. Son ligeros, pero mejor como columnas que dentro del JSON. `lle` y
  `lipophilic_efficiency` son el mismo valor dos veces.
- **Limpieza profunda de lo duplicado o sin uso:** índices repetidos (`idx_dr_ligand_molecule` =
  `ix_docking_results_ligand_molecule_id`, `idx_dr_receptor_molecule` =
  `ix_docking_results_receptor_molecule_id`), tablas sin uso y propiedades que están en 3 o 4
  sitios.
- **¿Hacen falta 9 poses?** En VS bastaría probablemente con el top 3. Antes de fijarlo, medir
  con rescoring de gnina en qué posición original cae la pose que gana.
- **Compresión de poses:** cada pose es un payload aparte (SDF con todos los H, ~3,9 KB, zstd
  sin diccionario → 848 B). Medido con las 4446 poses: zstd-19 sin diccionario 784 B, con
  diccionario entrenado (`PayloadStore.train_dictionary` ya existe y AMDockVS no lo usa) 566 B,
  todas como un solo bloque 416 B. Guardar la topología una vez por ligando y solo las
  coordenadas por pose: medido en `docs/benchmarks/vs_classic_2026-10-18/` (un payload por ligando
  = 50 %; int16 a 1e-3 Å = 33 %; solo átomos pesados = 20 % de lo de hoy).
  Propuesta (2026-10-19, sin decidir): lo genérico es **un payload por ligando con sus poses en el
  formato de hoy** (SDF de varias moléculas, PDB/PDBQT con MODEL). Sirve para cualquier tipo de
  molécula y da el 50 %. int32 sin pérdida da lo mismo (49 %), y un diccionario zstd no aporta nada
  con bloques agrupados (51 %). Las coordenadas int16 (33 %) solo valen la pena para el modo shards.
- **"Fire and forget" con corte de energía**, como en shards: el usuario fija un corte (p. ej.
  −8,5 para Vina y derivados) y no se guardan en disco las poses de los ligandos peores que el corte
  (score > −8,5). Solo queda su registro de energía en la tabla. Sin implementar.
