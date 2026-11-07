# Modelo de ejecución del backend

## Una ruta pública, dos adaptadores

El backend no tiene dos pipelines científicos. La biblioteca activa determina la unidad de
trabajo:

| Biblioteca | Unidad entregada a MF | Adaptador de E/S |
|---|---|---|
| Molecules en la DB | Lote de filas | `transform_ligand_rows` |
| Shards | Uno o varios shards completos | `transform_ligand_shard` |

`ChemistryAPI.run_ligand_pipeline`, `DockingAPI.prepare_ligands` y `DockingAPI.run` seleccionan
el adaptador automáticamente cuando no se proporciona un scope explícito de filas. Los métodos
`run_shard_pipeline`, `prepare_ligand_shards` y `run_shards` siguen disponibles como primitivas
internas y para llamadas especializadas.

MF recibe jobs y chunks normales. En HPC, `compute` puede corresponder al clúster Ray levantado
dentro de una reserva; AMDock no genera arrays ni un submission del scheduler por shard.

## Qué significa “puro” aquí

Se distinguen tres niveles. Es importante no llamar pura a una función sólo porque corre en un
worker:

1. **Pura:** depende únicamente de sus argumentos y no hace E/S.
2. **Kernel en memoria:** no abre la DB ni administra archivos, pero puede usar aleatoriedad,
   librerías nativas o un backend externo.
3. **Adaptador:** lee/escribe archivos, ejecuta programas o persiste estado.

| Área | Funciones principales | Nivel | Aplicación |
|---|---|---:|---|
| Composición química | `normalize_steps`, `kwargs_for`, `run_pipeline` | Kernel en memoria | Una llamada continua por lote o shard |
| Química RDKit | `standardize_ligand_molecule`, `generate_ligand_3d`, `minimize_ligand_molecule` | Kernel en memoria | Pasos encadenables sin escritura intermedia |
| Descriptores | `calculate_basic_descriptors` | Pura respecto al `Mol` | Paralela por molécula/lote |
| Fingerprints | `morgan_fingerprint`, conversiones a bitstring/matriz | Pura respecto al `Mol` | Debe agruparse con descriptores cuando ambos se necesitan |
| Clustering | `bitbirch_lean`, `select_representatives`, `cluster_stats`, proyección PCA | Pura sobre matrices | Reducción global después de construir fingerprints |
| Geometría del sitio | `centroid`, `radius_of_gyration`, `box_edge_from_rg`, `box_from_coords`, `residues_in_box` | Pura | Inline o por receptor; no requiere job propio |
| Identidad de protocolo | `protocol_identity`, `protocol_hash` | Pura | Antes de enviar el job y al consultar receipts |
| Métricas simples | `predicted_ki_m`, `predicted_pki`, `ligand_efficiency` | Pura | En el mismo task que procesa el resultado de docking |
| Shard químico | `read_shard`, `write_shard`, `transform_ligand_shard` | Adaptador | Un shard entra y otro sale |
| Preparación | `prepare_entities_rows`, `prepare_ligand_shard` | Adaptador | Job independiente y reutilizable por familia de engine |
| Docking | `run_docking_chunk`, `extract_ligands` | Adaptador | Chunk distribuido por Ray; empaqueta resultados y sólo materializa hits en HTP |
| P2Rank/interacciones/diagramas | ejecutables y parsers respectivos | Adaptador | Jobs separados por dependencia y artefactos propios |

`run_pipeline` es libre de DB y rutas, pero no es pureza matemática estricta: protonación puede
invocar backends distintos y la generación 3D sólo es reproducible si conserva su semilla.

## Qué se agrupa continuamente

### Química de ligandos

`standardize -> protonate -> generate_3d -> minimize` debe viajar como una lista de pasos en un
único job. Cada task carga su lote o shard una vez, ejecuta toda la lista y escribe únicamente el
resultado final. Enviar cuatro jobs separados implica cuatro lecturas, cuatro escrituras y tres
barreras de sincronización innecesarias.

Los conformeros son una frontera. El modelo por filas puede materializar varios modelos, pero el
formato actual de shard conserva una estructura por record. Por eso el backend rechaza
`conformers` sobre shards en vez de perder silenciosamente el ensemble.

### Descriptores y fingerprints

El job de descriptores ya puede calcular el fingerprint durante la misma lectura. Si ambos son
necesarios, deben pedirse juntos. Calcular fingerprints como job independiente sólo es útil si:

- los descriptores ya existen;
- se necesita otra familia/radio de fingerprint; o
- los fingerprints se producirán directamente desde shards para una reducción global.

### Diversity/BitBIRCH

La creación de fingerprints es paralelizable por lote/shard. El clustering no es una colección
de clusters independientes: necesita una reducción global. `bblean multiround` administra su
propio paralelismo; desde MF debe verse como un job con los recursos reservados, no como miles de
jobs pequeños.

La forma escalable es:

```text
shards -> fingerprints parciales en paralelo -> manifiesto/matriz compacta -> clustering global
       -> selección de ids -> reconstrucción transaccional de shards
```

### Preparación y docking

Permanecen como dos jobs lógicos porque la preparación es reutilizable entre receptores y
protocolos. En una ejecución completamente remota ambos jobs deben correr dentro de la misma
reserva Ray y sobre el mismo almacenamiento de trabajo; “dos jobs” no significa “dos reservas”.

La generación 3D puede ejecutarse antes como parte del pipeline químico. `prepare_ligand_shard`
detecta conformaciones existentes y sólo genera 3D para los records que aún no las tienen.

### Resultados HTP

El shard químico no cambia al prepararlo. `ShardEngineState` mantiene los artefactos por familia
de preparación. `ScreeningShardRun` registra un receipt por run/shard/receptor/engine/protocolo;
así un resume omite exactamente los pares terminados y no hereda el estado de otra campaña.

Cada task `(shard, receptor)` abre un fragmento Parquet de resultados y, después de cada ligando, añade
una fila con score, métricas, SMILES y el SDF de todas las poses. En ese mismo
punto elimina el PDBQT y, si no es hit, también el SDF suelto. El `.results.parquet.tmp` crece durante el
trabajo y se publica atómicamente al terminar; por eso nunca se acumulan mil pares de archivos
ni queda un shard final corrupto si el task falla. Los SDF individuales que sobreviven son sólo
los hits que la UI necesita abrir. El receipt guarda la ruta del shard final.

El callback de MF recibe progreso por ligando y sirve también como punto de cancelación
cooperativa para `compute`. En Ray la cancelación sigue siendo forzada por el propio adapter.
Una campaña cancelada o fallida marca sus targets con ese estado y no promociona un top-N
parcial.

### Dependencias y alcance diferido

El alcance de docking es una fotografía tomada una vez, pero se toma **después** de completar
los jobs activos de importación, química y preparación. MF difiere `build_chunks`; en ese punto
AMDock enumera los shards preparados, calcula el total definitivo y crea el plan de receptores.
Esto evita tanto ignorar el último shard como hacer que el alcance cambie mientras una campaña
ya está corriendo. No se consulta repetidamente una tabla “viva”.

## Fronteras que no deben fusionarse

- La escritura del inventario y el cómputo del worker: el sink de MF persiste en el parent.
- Preparación y docking cuando el artefacto preparado se reutilizará.
- Fingerprints por shard y clustering global.
- Docking y materialización completa: HTP sólo promueve hits.
- Transformaciones sustitutivas de shards y borrado del conjunto anterior: primero se valida y
  activa el conjunto nuevo; después se elimina el anterior.

`shards.keep_history` controla si futuros rebuilders conservan los conjuntos sustituidos. El
valor por defecto es `false`; aun así, el reemplazo debe ser transaccional y nunca borrar las
entradas activas antes de que el nuevo conjunto esté completo.
