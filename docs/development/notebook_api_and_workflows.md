# API de notebook y workflows canónicos de AMDock

Este documento fija una guía de uso de AMDock como backend programable.

Nota de vigencia:

- el runtime público actual es `AMDockVSRuntime`
- la ruta de import soportada es `from amdock import AMDockVSRuntime`
- los ejemplos viejos que mencionen `AMDockRuntime` deben leerse como `AMDockVSRuntime`
- `docking.screen(...)` es async por defecto; la espera se pide explícitamente

No describe la UI. Describe cómo debería sentirse AMDock desde Python, por ejemplo
en un notebook, script o pipeline reproducible.

La intención es:

- evitar que el diseño interno siga creciendo sin un contrato de uso claro;
- fijar operaciones de alto nivel antes de seguir agregando métodos aislados;
- decidir qué vive en `AMDockVSRuntime` y qué vive en herramientas/subcomponentes;
- tener workflows canónicos que sirvan de referencia para backend y UI.

## 1. Principio rector

`AMDockVSRuntime` no debería convertirse en una clase gigantesca con decenas de
métodos globales.

Su responsabilidad principal debería ser:

- manejar el proyecto activo;
- coordinar ejecución con MolSuite;
- exponer el estado del runtime;
- servir como punto de entrada a herramientas de dominio.

La lógica operativa de dominio debería agruparse en componentes como:

- `loader`
- `ligands`
- `receptors`
- `docking`
- `qsar`
- `structure`
- `analysis`

Es decir:

```python
amdock = AMDockVSRuntime()

amdock.loader.load_ligands(...)
amdock.ligands.filter(...)
amdock.docking.prepare_ligands(...)
amdock.docking.run(...)
amdock.qsar.train(...)
```

Esto deja a `AMDockVSRuntime` como fachada y coordinador, no como bolsa de
funciones heterogéneas.

## 2. Qué no conviene hacer

No conviene una API plana de este tipo:

```python
amdock.load_files(...)
amdock.load_ligands(...)
amdock.filter_ligands(...)
amdock.prepare_ligands(...)
amdock.prepare_receptors(...)
amdock.compute_descriptors(...)
amdock.qsar_train(...)
amdock.cluster_ligands(...)
amdock.docking(...)
amdock.docking_reverse(...)
```

Problemas:

- escala mal;
- mezcla dominios distintos en el mismo namespace;
- vuelve difícil descubrir capacidades;
- hace que `AMDockVSRuntime` cargue demasiada semántica de negocio;
- complica la documentación y la UI, porque todo parece una acción global.

## 3. Propuesta de forma general

La forma recomendada es esta:

```python
amdock = AMDockVSRuntime()

amdock.loader
amdock.ligands
amdock.receptors
amdock.docking
amdock.qsar
amdock.structure
amdock.analysis
amdock.<domain>.jobs
```

### 3.1 Responsabilidad de cada componente

#### `amdock.loader`

Entrada de datos externos.

Responsabilidades:

- cargar desde archivos locales;
- cargar desde texto;
- cargar desde URL o recursos remotos, si aplica;
- clasificar el tipo de artefacto de entrada;
- delegar a `ligands` o `receptors` cuando el tipo ya es explícito.

API ejemplo:

```python
amdock.loader.load_files(files=[...], as_="ligands")
amdock.loader.load_text(text=..., format="smiles", as_="ligands")
amdock.loader.load_url(url=..., as_="receptors")
amdock.loader.load_ligands(files=[...])
amdock.loader.load_receptors(files=[...])
```

Regla:

- `load_files(..., as_=...)` puede existir como API general
- `load_ligands(...)` y `load_receptors(...)` deben existir como wrappers claros

Eso permite flexibilidad sin volver obligatoria la API genérica para el usuario.

#### `amdock.ligands`

Operaciones de dominio sobre el inventario de ligandos.

Responsabilidades:

- filtrado;
- deduplicación;
- selección;
- validación química básica;
- cálculo de propiedades comunes de tabla principal;
- agrupación de colecciones y conjuntos de trabajo.

API ejemplo:

```python
scope = amdock.ligands.all()
scope = amdock.ligands.filter(scope, filters={"is_valid": True})
for ligand in amdock.ligands.stream(scope):
    ...
ligand_set = amdock.ligands.snapshot(scope, name="screening_round_1")
```

#### `amdock.receptors`

Operaciones de dominio sobre receptores.

Responsabilidades:

- filtrado;
- validación de estructura;
- selección de receptor activo;
- configuración de residuos flexibles;
- definición de sitio o caja si aplica al motor.

API ejemplo:

```python
scope = amdock.receptors.all()
scope = amdock.receptors.filter(scope, filters={"active": True})
receptor_set = amdock.receptors.snapshot(scope, name="active_panel")
```

#### `amdock.docking`

Capacidades específicas de docking y redocking.

Responsabilidades:

- preparación de ligandos para motores de docking;
- preparación de receptores para motores de docking;
- definición y validación de cajas;
- ejecución de docking;
- ejecución de reverse docking;
- redocking;
- análisis de interacciones;
- ranking de poses.

API ejemplo:

```python
amdock.docking.prepare_ligands(engine="vina")
amdock.docking.prepare_receptors(engine="vina")
amdock.docking.set_grid(receptor_id=1, center=(...), size=(...), spacing=0.375)
amdock.docking.run(engine="vina", receptor_id=1, ligand_ids=[...])
amdock.docking.reverse(engine="vina", ligand_id=1, receptor_ids=[...])
amdock.docking.redock(engine="vina", complex_id=3)
amdock.docking.interactions(result_id=10)
```

#### `amdock.qsar`

Capacidades específicas de QSAR.

Responsabilidades:

- actividades y endpoints;
- cálculo de matrices de descriptores/fingerprints;
- selección de features;
- entrenamiento;
- validación;
- predicción;
- evaluación y applicability domain.

API ejemplo:

```python
amdock.qsar.load_activities(file=...)
amdock.qsar.compute_descriptors(family="rdkit_2d")
amdock.qsar.compute_fingerprints(kind="morgan")
amdock.qsar.train(model="random_forest", target="pIC50")
amdock.qsar.predict(model_id=3, ligand_ids=[...])
amdock.qsar.evaluate(model_id=3)
```

#### `amdock.structure`

Capacidades de modelado y transformación estructural.

Responsabilidades:

- generación de 3D en pequeñas moléculas;
- modelado de proteínas;
- minimización;
- relajación estructural;
- conformeros;
- alineamiento y RMSD.

API ejemplo:

```python
amdock.structure.small_generate_3d(ligand_ids=[...])
amdock.structure.small_minimize(ligand_ids=[...])
amdock.structure.protein_modeling(engine="esmfold", sequences=[...])
amdock.structure.protein_minimize(receptor_ids=[...])
amdock.structure.align(reference_id=1, probe_ids=[...])
```

#### `amdock.analysis`

Capacidades analíticas transversales.

Responsabilidades:

- clustering;
- PCA;
- diversity selection;
- similitud;
- subestructura;
- análisis exploratorio de resultados.

API ejemplo:

```python
amdock.analysis.pca(source="descriptor_set:12")
amdock.analysis.cluster(source="ligands", method="bitbirch")
amdock.analysis.similarity(query=1, against=[...], kind="morgan")
amdock.analysis.substructure(smarts="c1ccccc1")
```

## 4. Decisiones de modelado de API

### 4.1 `loader` general + wrappers especializados

Recomendación:

- sí tener `loader.load_files(..., as_=...)`
- sí tener `loader.load_ligands(...)` y `loader.load_receptors(...)`

Razón:

- el método general sirve para flujos programáticos o futuros inputs heterogéneos;
- los wrappers especializados dan una API clara, corta y fácil de descubrir.

### 4.2 Las propiedades básicas del ligando se calculan al cargar

Esto sí conviene como comportamiento por defecto para ligandos:

- descriptores simples tipo Lipinski;
- `n_atoms`;
- `rotatable_bonds`;
- quizá `mw`, `logp`, `hbd`, `hba`, `tpsa`.

Razón:

- son necesarias para la tabla principal;
- también sirven para filtrado temprano;
- su costo es bajo comparado con otras etapas.

Esto no sustituye `qsar.compute_descriptors(...)`. Son dos niveles distintos:

- propiedades básicas de inventario;
- matrices de features para modelado.

### 4.3 Actividades QSAR

No conviene mezclar “ligandos con actividad” como una entidad separada si químicamente
son los mismos ligandos.

Recomendación:

- `ligands` sigue siendo la entidad química central;
- la actividad vive en una tabla aparte (`activities`);
- la pertenencia a workflows vive en conjuntos materializados (`ligand_sets`, `ligand_set_items`);
- `qsar_datasets` representa datasets de modelado más explícitos y reutilizables.

Es decir:

- el ligando y su estructura siguen existiendo una sola vez;
- la actividad es una anotación o medición asociada;
- el hecho de que ese ligando participe en QSAR, docking o ambos no debería
  contaminar la entidad química base.

#### Interpretación operativa

Para estos escenarios:

1. ligandos de docking sin actividad;
2. ligandos de QSAR con actividad;
3. ligandos de referencia usados en complejos/redocking;

la arquitectura recomendada es:

- `ligands`: inventario químico único;
- `activities`: valores experimentales asociados a algunos ligandos;
- `ligand_sets`: subconjuntos materializados como `screening_library`,
  `qsar_train`, `qsar_test`, `qsar_validation`, `reference_ligands`;
- `complexes`: relación estructural receptor-ligando de referencia;
- `complex_sets`: subconjuntos de complejos, por ejemplo `redocking_validation`.

Consecuencia:

- un ligando con actividad sigue siendo un ligando;
- no por eso debe entrar automáticamente a docking;
- un ligando de referencia para redocking también sigue siendo un ligando;
- su rol en redocking debe salir del set o del complejo, no de flags ambiguos
  dentro de `ligands`.

#### Sobre `train`, `test` y `validation`

No conviene meter `train/test/validation` como columnas fijas en `ligands`.

Conviene materializarlo como pertenencia a conjuntos:

- `qsar_train`
- `qsar_test`
- `qsar_validation`

o, si se quiere algo más formal para modelado:

- `qsar_datasets`
- `qsar_dataset_items`

Esto permite regenerar splits dinámicos y a la vez congelar snapshots cuando se
necesita reproducibilidad.

#### Sobre complejos y redocking

Los complejos de referencia tampoco deberían duplicar al ligando base.

La estructura recomendada es:

- `complexes` guarda la relación `receptor_id + ligand_id + reference_state`;
- `complex_sets` agrupa subconjuntos como `redocking_reference_set`;
- si un ligando de complejo además tiene actividad, eso sigue viviendo en
  `activities`, no en una segunda tabla de ligandos.

#### Implicación de rendimiento

Sí, esta arquitectura es viable también desde rendimiento si se hace de esta forma:

- no guardar listas enormes de ids en JSON;
- sí materializar pertenencias como filas en `ligand_set_items` y `complex_set_items`;
- indexar por `(set_id, ligand_id)` y `(ligand_id, set_id)`.

Eso es mejor que:

- filtrar constantemente por flags mezclados en `ligands`;
- o construir `WHERE id IN (...)` gigantes para cada operación.

En resumen:

- `ligands` = identidad química;
- `activities` = medición experimental;
- `ligand_sets` = contexto de uso;
- `complexes` = artefacto estructural receptor-ligando;
- `complex_sets` = contexto de uso de complejos.

### 4.4 Preparación global vs preparación específica por motor

Conviene separar:

- preparación global:
  - validación
  - protonación
  - generación 3D
  - minimización

- preparación específica por motor:
  - PDBQT para Vina/AutoDock
  - formatos/artefactos específicos para DOCK
  - inputs distintos para DiffDock

Esto permite una API como:

```python
amdock.docking.prepare_ligands(global_only=True)
amdock.docking.prepare_ligands(engine="vina")
```

o más explícita:

```python
amdock.structure.small_generate_3d(...)
amdock.docking.prepare_ligands(engine="vina")
```

La segunda forma es más clara y más alineada con una arquitectura limpia.

## 5. Workflow canónico: docking simple

```python
amdock = AMDockVSRuntime()

# 1. Carga
amdock.loader.load_ligands(files=["library.sdf"])
amdock.loader.load_receptors(files=["receptor.pdb"])

# 2. Curación de ligandos
amdock.ligands.filter(by=["valid", "organic", "mw < 700", "rotatable_bonds <= 12"])
amdock.ligands.deduplicate(method="canonical_smiles")

# 3. Preparación global
amdock.structure.small_generate_3d(ligand_ids="current")
amdock.structure.small_minimize(ligand_ids="current")

# 4. Preparación específica para docking
amdock.docking.prepare_ligands(engine="vina", ligand_ids="current")
amdock.docking.prepare_receptors(engine="vina", receptor_ids="current")

# 5. Definición del sitio
amdock.docking.set_grid(
    receptor_id=1,
    center=(12.0, 13.0, 10.0),
    size=(20.0, 20.0, 20.0),
    spacing=0.375,
)

# 6. Docking
job = amdock.docking.run(
    engine="vina",
    receptor_id=1,
    ligand_ids="current",
    exhaustiveness=8,
    num_modes=9,
)

# 7. Post-análisis
amdock.docking.interactions(results="last")
amdock.analysis.cluster(source="docking:last", method="fingerprint")
```

## 6. Workflow canónico: reverse docking

```python
amdock = AMDockVSRuntime()

amdock.loader.load_ligands(files=["query_ligand.sdf"])
amdock.loader.load_receptors(files=["panel/*.pdb"])

amdock.structure.small_generate_3d(ligand_ids="current")
amdock.docking.prepare_ligands(engine="vina", ligand_ids="current")
amdock.docking.prepare_receptors(engine="vina", receptor_ids="all")

amdock.docking.set_grid(receptor_id=1, center=(...), size=(...), spacing=0.375)
amdock.docking.set_grid(receptor_id=2, center=(...), size=(...), spacing=0.375)

job = amdock.docking.reverse(
    engine="vina",
    ligand_id=1,
    receptor_ids="all",
)
```

## 7. Workflow canónico: redocking

```python
amdock = AMDockVSRuntime()

amdock.loader.load_receptors(files=["complex_receptor.pdb"])
amdock.loader.load_ligands(files=["native_ligand.sdf"])

amdock.docking.prepare_ligands(engine="vina", ligand_ids="current")
amdock.docking.prepare_receptors(engine="vina", receptor_ids="current")
amdock.docking.set_grid(receptor_id=1, center=(...), size=(...), spacing=0.375)

job = amdock.docking.redock(
    engine="vina",
    receptor_id=1,
    ligand_id=1,
    randomize_pose=True,
)

amdock.analysis.pose_rmsd(result="last", reference="native")
```

## 8. Workflow canónico: QSAR simple

```python
amdock = AMDockVSRuntime()

# 1. Carga de ligandos
amdock.loader.load_ligands(files=["training_set.sdf"])

# 2. Curación
amdock.ligands.filter(by=["valid", "organic"])
amdock.ligands.deduplicate(method="canonical_smiles")

# 3. Carga de actividades
amdock.qsar.load_activities(file="activities.csv", ligand_key="name", target="IC50")

# 4. Features
amdock.qsar.compute_descriptors(family="rdkit_2d")
amdock.qsar.compute_fingerprints(kind="morgan", radius=2, n_bits=2048)

# 5. Dataset y entrenamiento
dataset = amdock.qsar.create_dataset(
    ligands="with_activity",
    features=["rdkit_2d", "morgan"],
    target="pIC50",
)

model = amdock.qsar.train(
    dataset=dataset,
    model="random_forest",
    validation="kfold",
)

# 6. Predicción y análisis
amdock.qsar.predict(model=model, ligands="all")
amdock.qsar.evaluate(model=model)
amdock.analysis.pca(source=dataset)
```

## 9. Workflow canónico: QSAR antes de docking

```python
amdock = AMDockVSRuntime()

amdock.loader.load_ligands(files=["library.sdf"])
amdock.ligands.filter(by=["valid", "organic"])

amdock.qsar.compute_descriptors(family="rdkit_2d")
amdock.qsar.predict(model="admet_v1", ligands="current")

amdock.ligands.filter(by=["pred_admet_ok", "pred_activity > threshold"])

amdock.structure.small_generate_3d(ligand_ids="current")
amdock.docking.prepare_ligands(engine="vina", ligand_ids="current")
amdock.docking.run(engine="vina", receptor_id=1, ligand_ids="current")
```

## 10. Workflow canónico: modelado estructural

```python
amdock = AMDockVSRuntime()

amdock.structure.protein_modeling(
    engine="esmfold",
    sequences=["MKT..."],
)

amdock.structure.protein_minimize(receptor_ids="last")
amdock.docking.prepare_receptors(engine="vina", receptor_ids="last")
```

## 11. Recomendación concreta para la implementación

La propuesta recomendada es esta:

```python
amdock = AMDockVSRuntime()

amdock.loader.load_ligands(...)
amdock.loader.load_receptors(...)

amdock.ligands.filter(...)
amdock.ligands.deduplicate(...)

amdock.structure.small_generate_3d(...)
amdock.structure.small_minimize(...)

amdock.docking.prepare_ligands(...)
amdock.docking.prepare_receptors(...)
amdock.docking.set_grid(...)
amdock.docking.run(...)
amdock.docking.reverse(...)
amdock.docking.redock(...)

amdock.qsar.load_activities(...)
amdock.qsar.compute_descriptors(...)
amdock.qsar.compute_fingerprints(...)
amdock.qsar.train(...)
amdock.qsar.predict(...)

amdock.analysis.cluster(...)
amdock.analysis.pca(...)
```

Razón:

- reduce la sobrecarga conceptual de `AMDockRuntime`;
- agrupa semánticamente las operaciones;
- deja una API más fácil de usar desde notebook;
- alinea backend, runtime y UI con los mismos dominios;
- permite que cada subcomponente crezca sin volver ilegible la fachada principal.

## 12. Consecuencia arquitectónica

Sí: esto implica que `AMDockRuntime` debería tener pocos métodos directos nuevos.

En vez de crecer horizontalmente, debería exponer facades o toolkits:

- `LoaderAPI`
- `LigandAPI`
- `ReceptorAPI`
- `DockingAPI`
- `QSARAPI`
- `StructureAPI`
- `AnalysisAPI`

Eso segmenta el código y hace más explícita la separación por dominio.

También alinea bien con la estructura de módulos ya propuesta:

- `chemistry/`
- `docking/`
- `qsar/`
- `analysis/`
- `io/`
- `jobs/`

## 13. Tareas derivadas

### Fase 1. Fijar la fachada pública

- [ ] Definir el shape base de `AMDockRuntime`
- [ ] Añadir subcomponentes `loader`, `ligands`, `receptors`, `docking`, `qsar`, `structure`, `analysis`
- [ ] Decidir si esos subcomponentes son atributos persistentes o factories ligeras

### Fase 2. Alinear el loader

- [ ] Mantener `load_ligands(...)` y `load_receptors(...)`
- [ ] Diseñar `load_files(..., as_=...)` como API opcional, no obligatoria
- [ ] Decidir qué metadatos y propiedades básicas se calculan al cargar

### Fase 3. Alinear docking

- [ ] Separar preparación global de preparación específica por motor
- [ ] Definir contrato común para `set_grid(...)`
- [ ] Definir `run(...)`, `reverse(...)` y `redock(...)`
- [ ] Implementar preparación real a `PDBQT` para Vina/AutoDock

### Fase 4. Alinear QSAR

- [ ] Definir carga de actividades
- [ ] Definir cálculo de descriptores y fingerprints por familias
- [ ] Definir dataset/model/prediction como artefactos explícitos

### Fase 5. Alinear estructura y análisis

- [ ] Definir `structure.small_*` y `structure.protein_*`
- [ ] Definir `analysis.cluster`, `analysis.pca`, `analysis.similarity`
- [ ] Evitar que análisis transversales terminen escondidos dentro de `ligands` o `qsar`

## 14. Esquema SQL mínimo para ChartDB

Este bloque no intenta cubrir todo AMDock. Muestra la parte de modelo que mejor
encaja con:

- docking vs QSAR;
- splits dinámicos de QSAR;
- complejos de referencia para redocking;
- actividades separadas de la identidad química;
- conjuntos materializados para evitar filtros ambiguos;
- estados moleculares mínimos para preparación y ejecución.

```sql
CREATE TABLE ligands (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    stored_path TEXT NOT NULL DEFAULT '',
    canonical_smiles TEXT NOT NULL DEFAULT '',
    input_format TEXT NOT NULL DEFAULT '',
    n_atoms INTEGER NOT NULL DEFAULT 0,
    mw REAL,
    logp REAL,
    hbd INTEGER,
    hba INTEGER,
    tpsa REAL,
    rotatable_bonds INTEGER,
    is_valid INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE receptors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    stored_path TEXT NOT NULL DEFAULT '',
    input_format TEXT NOT NULL DEFAULT '',
    n_atoms INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE receptor_sets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    is_snapshot INTEGER NOT NULL DEFAULT 1,
    filter_spec_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE receptor_set_items (
    set_id INTEGER NOT NULL,
    receptor_id INTEGER NOT NULL,
    PRIMARY KEY (set_id, receptor_id),
    FOREIGN KEY (set_id) REFERENCES receptor_sets(id),
    FOREIGN KEY (receptor_id) REFERENCES receptors(id)
);

CREATE INDEX idx_receptor_set_items_receptor_id_set_id
    ON receptor_set_items(receptor_id, set_id);

CREATE TABLE molecule_states (
    id INTEGER PRIMARY KEY,
    entity_kind TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    state_kind TEXT NOT NULL DEFAULT 'raw',
    parent_state_id INTEGER,
    has_hs INTEGER NOT NULL DEFAULT 0,
    has_3d INTEGER NOT NULL DEFAULT 0,
    is_minimized INTEGER NOT NULL DEFAULT 0,
    conformer_count INTEGER NOT NULL DEFAULT 0,
    selected_conformer INTEGER,
    binary_ref TEXT NOT NULL DEFAULT '',
    build_signature TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (parent_state_id) REFERENCES molecule_states(id)
);

CREATE INDEX idx_molecule_states_entity_kind_entity_id
    ON molecule_states(entity_kind, entity_id);

CREATE TABLE activities (
    id INTEGER PRIMARY KEY,
    ligand_id INTEGER NOT NULL,
    assay_name TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    relation TEXT NOT NULL DEFAULT '=',
    transform_kind TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (ligand_id) REFERENCES ligands(id)
);

CREATE TABLE ligand_sets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    is_snapshot INTEGER NOT NULL DEFAULT 1,
    filter_spec_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE ligand_set_items (
    set_id INTEGER NOT NULL,
    ligand_id INTEGER NOT NULL,
    PRIMARY KEY (set_id, ligand_id),
    FOREIGN KEY (set_id) REFERENCES ligand_sets(id),
    FOREIGN KEY (ligand_id) REFERENCES ligands(id)
);

CREATE INDEX idx_ligand_set_items_ligand_id_set_id
    ON ligand_set_items(ligand_id, set_id);

CREATE TABLE qsar_datasets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL DEFAULT '',
    activity_transform TEXT NOT NULL DEFAULT '',
    split_strategy TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE qsar_dataset_items (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL,
    ligand_id INTEGER NOT NULL,
    activity_id INTEGER,
    role TEXT NOT NULL DEFAULT 'train',
    FOREIGN KEY (dataset_id) REFERENCES qsar_datasets(id),
    FOREIGN KEY (ligand_id) REFERENCES ligands(id),
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);

CREATE INDEX idx_qsar_dataset_items_dataset_role
    ON qsar_dataset_items(dataset_id, role);

CREATE TABLE complexes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    receptor_id INTEGER NOT NULL,
    ligand_id INTEGER NOT NULL,
    reference_state_ref TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (receptor_id) REFERENCES receptors(id),
    FOREIGN KEY (ligand_id) REFERENCES ligands(id)
);

CREATE TABLE complex_sets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    is_snapshot INTEGER NOT NULL DEFAULT 1,
    filter_spec_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE complex_set_items (
    set_id INTEGER NOT NULL,
    complex_id INTEGER NOT NULL,
    PRIMARY KEY (set_id, complex_id),
    FOREIGN KEY (set_id) REFERENCES complex_sets(id),
    FOREIGN KEY (complex_id) REFERENCES complexes(id)
);

CREATE INDEX idx_complex_set_items_complex_id_set_id
    ON complex_set_items(complex_id, set_id);

CREATE TABLE docking_runs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL DEFAULT '',
    receptor_id INTEGER,
    receptor_set_id INTEGER,
    ligand_set_id INTEGER,
    box_center_json TEXT NOT NULL DEFAULT '[]',
    box_size_json TEXT NOT NULL DEFAULT '[]',
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (receptor_id) REFERENCES receptors(id),
    FOREIGN KEY (receptor_set_id) REFERENCES receptor_sets(id),
    FOREIGN KEY (ligand_set_id) REFERENCES ligand_sets(id)
);

CREATE TABLE docking_results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    ligand_id INTEGER NOT NULL,
    receptor_id INTEGER NOT NULL,
    output_path TEXT NOT NULL DEFAULT '',
    score REAL,
    rank INTEGER,
    status TEXT NOT NULL DEFAULT 'completed',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES docking_runs(id),
    FOREIGN KEY (ligand_id) REFERENCES ligands(id),
    FOREIGN KEY (receptor_id) REFERENCES receptors(id)
);

CREATE TABLE redocking_runs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL DEFAULT '',
    complex_set_id INTEGER,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (complex_set_id) REFERENCES complex_sets(id)
);

CREATE TABLE redocking_results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    complex_id INTEGER NOT NULL,
    docking_result_id INTEGER,
    best_rmsd REAL,
    heavy_atom_rmsd REAL,
    is_success INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES redocking_runs(id),
    FOREIGN KEY (complex_id) REFERENCES complexes(id),
    FOREIGN KEY (docking_result_id) REFERENCES docking_results(id)
);
```

Notas:

- `ligand_sets`, `receptor_sets` y `complex_sets` son la pieza que evita mezclar
  roles de workflow dentro de `ligands`, `receptors` o `complexes`;
- `activities` queda separada porque un mismo ligando puede tener múltiples
  mediciones;
- `qsar_dataset_items.role` permite `train`, `test` y `validation` dinámicos;
- `complexes` resuelve el caso de redocking sin duplicar ligandos ni receptores;
- `molecule_states` cubre el mínimo útil para saber si una entidad tiene H,
  3D, minimización o conformeros sin meter todavía trazabilidad detallada.

## 15. Plan de implementación

Esta sección traduce la propuesta anterior a una hoja de ruta concreta.

### 15.1 Regla de implementación

Cada operación pública de AMDock debe seguir este patrón:

1. validar y normalizar parámetros;
2. resolver un `scope` explícito (`ligand_set`, `receptor_set`, `complex_set`, `dataset`);
3. delegar la ejecución real a MolSuite;
4. devolver una referencia persistente (`*Ref`) o un `job_id`.

Eso implica:

- la fachada pública no ejecuta cálculos pesados inline;
- la UI y el notebook llaman la misma fachada;
- los jobs viven en MolSuite y AMDock sólo compone intención de dominio.

### 15.2 Tipos de referencia mínimos

Primera versión recomendada:

- `LigandSetRef(id: int, kind: str = "snapshot")`
- `ReceptorSetRef(id: int, kind: str = "snapshot")`
- `ComplexSetRef(id: int, kind: str = "snapshot")`
- `QSARDatasetRef(id: int)`
- `DockingRunRef(id: int | None = None, job_id: str | None = None)`
- `LigandScope(filters: ..., source_set_id: ... )`
- `ReceptorScope(filters: ..., source_set_id: ... )`

Regla:

- los `*SetRef` son handles ligeros;
- no guardan listas de ids en memoria;
- la membresía real vive en `*_sets` y `*_set_items`.
- los `*Scope` son selecciones perezosas sobre `project.db`;
- `all()` y `filter()` construyen scopes;
- `snapshot()` es el paso explícito de materialización.

### 15.3 `snapshot` vs `dynamic`

Se deben soportar dos modos de selección:

#### `dynamic`

- representa un filtro guardado;
- útil para exploración de notebook/UI;
- no debe usarse como contrato principal de ejecución pesada.

#### `snapshot`

- materializa membresía en `*_set_items`;
- se usa para jobs reproducibles;
- es el modo recomendado antes de ejecutar docking, QSAR o modelado.

Regla operativa:

- una operación exploratoria puede devolver `dynamic`;
- cualquier operación costosa debe materializar primero o exigir `snapshot`.

### 15.4 Fases recomendadas

#### Fase 0. Congelar el contrato público

Objetivo:

- dejar fija la forma de la API antes de mover demasiado código interno.

Tareas:

- [x] definir los `*Ref` mínimos;
- [x] definir el concepto de `scope` explícito;
- [ ] quitar `current` del contrato principal y dejarlo sólo como conveniencia efímera si se mantiene.

Resultado esperado:

- un documento de tipos/refs estable;
- ejemplos de notebook ya alineados con `LigandSetRef`, `ReceptorSetRef`, etc.

#### Fase 1. Implementar sets persistidos

Objetivo:

- introducir el mecanismo base de selección reproducible.

Tareas:

- [x] implementar modelos reales:
  - `LigandSetRecord`
  - `LigandSetItemRecord`
  - `ReceptorSetRecord`
  - `ReceptorSetItemRecord`
  - `ComplexSetRecord`
  - `ComplexSetItemRecord`
- [x] añadir índices de membresía simples a nivel de columnas/PK compuesta;
- [x] implementar helpers mínimos para crear, poblar y consultar sets;
- [ ] revisar si hacen falta índices compuestos adicionales según carga real;
- [ ] integrar estos helpers al runtime/facades públicos.

Resultado esperado:

- cualquier selección relevante puede persistirse como set.

#### Fase 2. Implementar facades mínimos en `AMDockRuntime`

Objetivo:

- convertir el runtime en fachada real de dominio.

Tareas:

- [x] añadir:
  - `amdock.loader`
  - `amdock.ligands`
  - `amdock.receptors`
- `amdock.docking` (hecho, con `run(...)` y `screen(...)`)
- `amdock.qsar` (hecho, con `compute_descriptors(...)`)
  - `amdock.structure` (pendiente)
  - `amdock.analysis` (pendiente)
- [ ] mantener compatibilidad transitoria con algunos métodos legacy del runtime;
- [x] decidir si estos facades son atributos persistentes o wrappers ligeros construidos al vuelo.
- [x] hacer que `all()` y `filter()` construyan scopes perezosos;
- [x] hacer que `snapshot()` materialice explícitamente la selección.

Resultado esperado:

- el notebook puede usar la API nueva sin tocar directamente `jobs/`.

#### Fase 3. Migrar operaciones de selección

Objetivo:

- hacer que el flujo real opere sobre scopes explícitos.

Tareas:

- [ ] `loader.load_ligands(...)` y `loader.load_receptors(...)` deben devolver refs útiles;
- [x] `ligands.filter(...)` debe devolver un `LigandScope` perezoso;
- [x] `ligands.snapshot(...)` debe producir `LigandSetRef`;
- [ ] `ligands.deduplicate(...)` debe devolver `LigandSetRef`;
- [ ] `qsar.create_dataset(...)` debe producir `QSARDatasetRef`.
- [x] `qsar.compute_descriptors(ligand_set=...)` acepta `LigandSetRef` o `LigandScope`.
- [x] `docking.run(ligand_set=..., receptor_set=...)` acepta `LigandSetRef` / `ReceptorSetRef` o `Scope`.

Resultado esperado:

- ya no se depende de un estado implícito como `current`.

#### Fase 4. Hacer que los jobs consuman sets

Objetivo:

- alinear el backend real con el modelo de selección persistida.

Tareas:

- [x] cambiar jobs para aceptar `ligand_set_id`, `receptor_set_id`, `complex_set_id` o `dataset_id`;
- [x] reducir el uso de filtros globales del estilo `only_active` como mecanismo principal;
- [x] quitar `only_active` / `only_selected` del contrato público de `qsar` y `docking`.

Resultado esperado:

- MolSuite ejecuta trabajos sobre scopes explícitos y reproducibles.

#### Fase 5. Alinear QSAR con datasets explícitos

Objetivo:

- modelar el pipeline QSAR sin contaminar `ligands`.

Tareas:

- [x] implementar `activities`;
- [x] implementar `qsar_datasets` y `qsar_dataset_items`;
- [x] soportar `train/test/validation/predict` a nivel de dataset o role;
- [x] implementar artefactos mínimos de modelo si AMDock va a persistirlos por su cuenta.

Resultado esperado:

- el flujo QSAR puede ejecutarse antes o después de docking sin ambigüedad.

#### Fase 6. Alinear docking y redocking con estados moleculares

Objetivo:

- capturar preparación y ejecución sin romper la simplicidad del modelo.

Tareas:

- [x] usar `molecule_states` para registrar al menos:
  - [x] raw
  - [ ] prepared_3d
  - [x] prepared_for_vina
  - [x] docked_pose
- [x] añadir `ligand_state_id` y `receptor_state_id` en resultados si se requiere reproducibilidad estricta;
- [ ] conectar `complexes` y `complex_sets` al flujo de redocking.

Resultado esperado:

- docking y redocking pueden reproducir qué estructura preparada se usó realmente.

### 15.5 Orden recomendado de ejecución

Orden práctico:

1. `*SetRef` + modelos `*_sets`
2. facades mínimos del runtime
3. filtros/selecciones que devuelvan sets
4. jobs que consuman `set_id`
5. datasets QSAR
6. estados moleculares conectados a docking/redocking

Razón:

- primero se estabiliza el contrato público;
- luego se introduce el mecanismo de scope;
- después se adapta la ejecución real en MolSuite;
- por último se endurece trazabilidad estructural.

### 15.6 Qué no hacer durante esta migración

- no meter lógica pesada dentro de los facades;
- no depender de `current` como contrato persistente;
- no guardar listas grandes de ids en JSON;
- no volver a meter semántica de workflow en `ligands` o `receptors`;
- no duplicar rutas de ejecución entre notebook y UI.

### 15.7 Criterio de éxito

Se puede considerar lograda la primera iteración cuando:

- un notebook puede hacer:

```python
amdock = AMDockVSRuntime()

screening = amdock.loader.load_ligands(files=[...])
filtered = amdock.ligands.filter(screening, by=["valid", "organic"])
prepared = amdock.docking.prepare_ligands(engine="vina", ligand_set=filtered)
job = amdock.docking.run(
    engine="vina",
    receptor_set=receptors,
    ligand_set=prepared,
    grid=grid_ref,
)
```

y ese flujo:

- no depende de `current`;
- materializa scopes persistidos;
- delega a MolSuite;
- puede ser llamado igual desde notebook o desde UI.
