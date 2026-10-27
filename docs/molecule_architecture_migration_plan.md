# Plan de migración a arquitectura basada en moléculas

Este documento fija la migración objetivo de AMDock hacia una arquitectura
centrada en una entidad molecular general, roles contextuales y sets por
propósito. No se mantendrá compatibilidad con la estructura legacy.

## Objetivo

La base del sistema dejará de ser:

- `amdock_ligands`
- `amdock_receptors`
- trazabilidad pesada por estado/artefacto/capability

Y pasará a ser:

- `amdock_molecules`
- `amdock_molecule_roles`
- `amdock_complexes`
- `amdock_sets`
- `amdock_set_items`

La idea es separar:

- identidad estructural persistida;
- rol contextual dentro de un workflow;
- conjuntos de trabajo estables o dinámicos;
- resultados reales que sí vale la pena guardar.

## Contrato objetivo

El runtime público seguirá siendo `AMDockVSRuntime`, pero el eje de trabajo
pasará a `runtime.molecules`.

Los dominios públicos esperados son:

- `runtime.loader`
- `runtime.molecules`
- `runtime.complexes`
- `runtime.chemistry`
- `runtime.docking`
- `runtime.qsar`

`runtime.ligands` y `runtime.receptors` no forman parte del contrato objetivo.
`ligand` y `receptor` se expresan como roles dentro de `runtime.molecules` y
como semántica específica dentro de `runtime.docking`.

## Modelo de datos objetivo

### `amdock_molecules`

Representa cualquier entidad estructural:

- small molecule
- protein
- peptide
- nucleic acid
- complex
- material
- unknown

Columnas rápidas previstas:

- `molecule_kind`
- `primary_role`
- `primary_context`
- `input_format`
- `source`
- `stored_path`
- `current_path`
- `n_atoms`
- `mw`
- `logp`
- `hbd`
- `hba`
- `tpsa`
- `rotatable_bonds`
- `fragment_count`
- `has_3d`
- `has_hs`
- `is_minimized`
- `prepared_vina`
- `prepared_vina_path`
- `grid_vina`
- `grid_json`
- `has_activity`
- `status_flags`
- `exclusion_flags`
- `metadata_json`

### `amdock_molecule_roles`

Asigna contexto de uso a una molécula:

- `ligand`
- `receptor`
- `reference_ligand`
- `query`
- `target`
- `cofactor`
- `complex_component`

Y además:

- `docking`
- `redocking`
- `rescoring`
- `qsar`
- `similarity`
- `pharmacophore`

### `amdock_complexes`

Representa relaciones receptor-ligando de referencia y casos de redocking,
rescoring o definición de sitio.

Campos esperados:

- `receptor_molecule_id`
- `ligand_molecule_id`
- `complex_molecule_id`
- `reference_pose_path`
- `activity_id`
- `purpose`
- `metadata_json`

### `amdock_sets`

Los sets se usarán para congelar colecciones con propósito explícito:

- `qsar`
- `qsar_train`
- `qsar_test`
- `qsar_validation`
- `similarity_query`
- `similarity_hits`
- `docking_candidates`
- `redocking_complexes`

No se crearán sets por cada filtro de visualización. Para eso se usarán scopes
dinámicos.

## Principios operativos

1. Las comprobaciones previas deben basarse en columnas rápidas y filtros.
2. La trazabilidad granular por fila no es prioritaria.
3. Los jobs deben consumir scopes y opcionalmente sets congelados.
4. Los complejos no son “receptores con metadata rara”; son una entidad
   contextual propia.
5. Los ligandos cocristalizados o de referencia no deben entrar por defecto en
   `docking_candidates`.

## Fases

### Fase 1. Congelar contrato objetivo

Entregables:

- documento de migración;
- `runtime.molecules`;
- `MoleculeScope`;
- `MoleculeSetRef`;
- API base de consulta y set sobre moléculas.

Estado:

- iniciado

### Fase 2. Introducir nuevo esquema núcleo

Entregables:

- `MoleculeRecord`;
- `MoleculeRoleRecord`;
- nuevo `ComplexRecord`;
- constantes de tablas nuevas;
- exportación desde `models`.

Estado:

- iniciado

### Fase 3. Reescribir importación

Entregables:

- `loader.load_molecules(...)`;
- asignación explícita de `molecule_kind`, `role` y `context`;
- carga de complejos y extracción de pares receptor-ligando.

Estado:

- pendiente

### Fase 4. Migrar chemistry

Entregables:

- jobs y funciones puras sobre `amdock_molecules`;
- actualización directa de columnas rápidas;
- sin `molecule_states` ni `artifact capabilities`.

Estado:

- pendiente

### Fase 5. Migrar docking y redocking

Entregables:

- preparación sobre moléculas;
- docking normal por rol;
- redocking/rescoring por `amdock_complexes`;
- chequeos de requisitos por filtros y estados rápidos.

Estado:

- pendiente

### Fase 6. Migrar QSAR y similarity

Entregables:

- actividades sobre moléculas;
- sets de propósito;
- train/test/validation como conjuntos explícitos.

Estado:

- pendiente

### Fase 7. Actualizar notebooks y UI

Entregables:

- notebooks canónicos sobre la API nueva;
- adaptación de la UI a scopes dinámicos + sets explícitos.

Estado:

- pendiente

### Fase 8. Limpieza final

Entregables:

- eliminación de tablas y código legacy;
- recreación limpia de la DB;
- validación de workflows.

Estado:

- pendiente

## Orden de implementación recomendado

1. contrato + esquema nuevo
2. API `molecules`
3. importación
4. chemistry
5. docking
6. complexes/redocking
7. qsar/similarity
8. notebooks
9. UI
10. limpieza total
