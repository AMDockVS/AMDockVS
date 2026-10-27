# Inventario inicial de capacidades computacionales de AMDock

Este documento define `AMDock` primero como motor de cálculo. No organiza pantallas ni docks; organiza qué cálculos existen, qué familias de métodos son comunes en CADD y qué piezas parecen transversales.

La intención es:

- fijar el alcance científico/técnico del backend;
- separar métodos comunes de métodos específicos;
- preparar una estructura de módulos y artefactos que después la UI solo refleje.

## 1. Cálculos que AMDock hará de forma explícita

### 1.1 Docking

Incluye:

- preparación de receptor;
- preparación de ligando;
- generación/ajuste de conformaciones 3D si hace falta;
- definición del sitio de unión;
- docking convencional;
- rescoring si aplica;
- análisis de interacciones;
- ranking y comparación de poses.

### 1.2 Redocking

Incluye todo lo de docking, con matices adicionales:

- extracción del ligando nativo/co-cristalizado;
- randomización o re-generación de la pose inicial;
- redocking al mismo sitio;
- comparación contra la pose cristalográfica;
- RMSD de pose;
- evaluación de capacidad de reproducción de pose;
- eventualmente cross-docking como extensión natural.

### 1.3 QSAR simple

Incluye:

- conjunto de ligandos;
- actividad/endpoint;
- cálculo de descriptores;
- cálculo de fingerprints;
- limpieza/selección de features;
- entrenamiento de modelos básicos;
- validación;
- predicción.

## 2. Operaciones y métodos comunes transversales

Estas piezas no pertenecen a un único workflow. Son infraestructura científica reutilizable.

### 2.1 Ingesta y normalización química

- leer archivos: `SDF`, `SMILES`, `MOL`, `MOL2`, `PDB`, `PDBQT`, etc.;
- parseo robusto y materialización;
- estandarización;
- neutralización / normalización de cargas;
- remoción o manejo de sales/solventes;
- canonicalización;
- deduplicación;
- asignación de identidad química y procedencia.

### 2.2 Preparación molecular

- protonación / estado de ionización;
- tautomerización / selección de tautómero;
- adición de H;
- asignación de bond orders;
- generación de geometría 3D;
- optimización geométrica;
- generación de conformeros;
- alineamiento;
- cálculo de carga parcial;
- preparación específica para motor de docking.

### 2.3 Representaciones químicas

- estructura 2D;
- estructura 3D;
- conformer ensemble;
- descriptores 1D/2D/3D;
- fingerprints topológicos;
- fingerprints circulares tipo ECFP/Morgan;
- fingerprints farmacofóricos;
- shape descriptors;
- interaction fingerprints.

### 2.4 Comparación estructural

- similitud química;
- búsqueda por subestructura;
- búsqueda por scaffold;
- RMSD;
- shape overlap;
- alineamiento de ligandos;
- comparación de farmacóforos;
- comparación de poses y/o interacciones.

### 2.5 Selección y reducción

- filtros fisicoquímicos;
- filtros estilo Lipinski / Veber / drug-likeness;
- filtrado por alertas o reglas;
- selección de descriptores;
- reducción de dimensionalidad;
- clustering;
- diversity picking.

### 2.6 Validación y análisis

- métricas de pose;
- métricas de ranking/enrichment;
- validación de modelos QSAR;
- análisis de error;
- interpretabilidad básica;
- visualización 2D/3D/interacciones.

## 3. Familias de métodos comunes en CADD/QSAR que conviene contemplar

Esto no significa que AMDock vaya a implementar todo ahora. Es un mapa de familias de métodos frecuentes sobre las que luego se puede decidir prioridad.

### 3.1 Métodos centrados en docking y structure-based design

- rigid receptor / flexible ligand docking;
- ensemble docking;
- induced-fit / flexible receptor docking;
- redocking;
- cross-docking;
- rescoring;
- consensus scoring;
- interaction profiling;
- pose consensus;
- post-docking refinement.

### 3.2 Métodos centrados en ligand-based screening

- similarity search con fingerprints;
- similarity search con descriptores;
- substructure search;
- scaffold search / scaffold hopping;
- fingerprint clustering;
- diversity selection;
- nearest-neighbor / analogue search.

### 3.3 Métodos centrados en farmacóforo

- ligand-based pharmacophore;
- structure-based pharmacophore;
- pharmacophore screening;
- pharmacophore + docking;
- pharmacophore + similarity filtering.

### 3.4 Métodos centrados en QSAR/QSPR

- descriptor-based QSAR;
- fingerprint-based QSAR;
- 3D-QSAR;
- clasificación binaria/multiclase;
- regresión;
- selección de variables;
- validación interna y externa;
- applicability domain.

### 3.5 Métodos centrados en dinámica y energía libre

- minimización y relajación;
- molecular dynamics;
- trayectoria y análisis conformacional;
- MM/PBSA o MM/GBSA;
- free energy perturbation / alchemical methods;
- binding free-energy refinement.

### 3.6 Métodos centrados en IA reciente

- modelos generativos de ligandos;
- docking asistido por ML;
- scoring con redes neuronales;
- predicción directa de pose desde secuencia/SMILES/estructura;
- surrogate models para priorización.

## 4. Métodos concretos que hoy parecen más comunes y razonables para AMDock

Esta lista es el punto útil para empezar a diseñar módulos.

### 4.1 Núcleo cercano

- lectura y materialización de moléculas;
- preparación básica de ligandos;
- preparación básica de receptores;
- generación de 3D / conformeros;
- docking convencional;
- redocking;
- RMSD de pose;
- análisis de interacciones;
- descriptores 2D básicos;
- fingerprints circulares;
- similitud y subestructura;
- QSAR básico con pocos algoritmos;
- PCA / clustering / selección visual de features.

### 4.2 Extensiones naturales de corto/medio plazo

- ensemble docking;
- consensus scoring;
- rescoring;
- pharmacophore screening;
- 3D descriptors;
- shape similarity;
- analogue search;
- activity cliffs / SAR support;
- validación QSAR más formal;
- applicability domain.

### 4.3 Extensiones de mayor costo

- induced-fit docking;
- docking con modelos ML tipo GNINA/DiffDock-like workflows;
- molecular dynamics;
- MM/GBSA o MM/PBSA;
- free energy perturbation;
- generación molecular / active learning.

## 5. Desglose preliminar por backend: métodos comunes vs. específicos

### 5.1 Métodos comunes reutilizables

Estos deberían vivir como capacidades compartidas del backend, no incrustados dentro de un único workflow.

- `file_ingestion`
- `molecule_standardization`
- `protonation_and_tautomer_handling`
- `conformer_generation`
- `geometry_optimization`
- `descriptor_calculation`
- `fingerprint_calculation`
- `similarity_and_substructure`
- `alignment`
- `rmsd`
- `interaction_extraction`
- `feature_selection`
- `dimensionality_reduction`
- `clustering`

### 5.2 Métodos específicos de docking

- receptor preparation para docking;
- grid / pocket definition;
- pose sampling;
- docking scoring;
- rescoring;
- redocking evaluation;
- pose reproduction metrics;
- interaction maps.

### 5.3 Métodos específicos de QSAR

- actividad / endpoint management;
- dataset split;
- model fitting;
- hyperparameter tuning;
- model validation;
- applicability domain;
- prediction / inference;
- feature importance / explainability básica.

### 5.4 Métodos específicos de redocking

- extracción de pose cristalográfica;
- randomización o regeneración de pose inicial;
- reproducción de pose;
- comparación contra referencia nativa;
- RMSD heavy-atom / scaffold-aware RMSD;
- comparación de interacciones nativas vs. redock.

## 6. Propuesta de métodos “base” para el inventario funcional

No es todavía una estructura de carpetas. Es un inventario funcional mínimo.

### 6.1 Artefactos

- ligands
- receptors
- activities
- descriptors
- fingerprints
- docking_runs
- docking_results
- redocking_runs
- interaction_profiles
- qsar_models
- qsar_predictions

### 6.2 Capacidades

- ingest
- prepare
- represent
- compare
- dock
- redock
- analyze_interactions
- build_qsar
- validate_qsar
- predict_qsar
- reduce_and_cluster

### 6.3 Workflows compuestos

- virtual_screening
- redocking_validation
- ligand_similarity_filtering
- descriptor_filtering
- simple_qsar_training
- pose_analysis

## 7. Métodos comunes en la literatura que justifican este mapa

Resumen inferido de revisiones y guías:

- En docking, el workflow base suele separarse en `sampling` y `scoring`, y luego se extiende con `redocking`, `cross-docking`, `ensemble docking`, `consensus scoring` y `interaction analysis`.
- En QSAR, lo normal es construir modelos a partir de descriptores o fingerprints, con selección de variables, validación y dominio de aplicabilidad.
- En ligand-based virtual screening, fingerprints, similitud estructural y subestructura siguen siendo métodos centrales y muy reutilizables.
- En screening estructural más avanzado, pharmacophore, docking y rescoring se combinan con frecuencia.

## 8. Decisión arquitectónica preliminar sugerida

Antes de pensar la UI, AMDock debería organizarse alrededor de:

- `artefactos persistidos`;
- `capacidades computacionales`;
- `workflows compuestos`;
- `métodos comunes compartidos`.

La UI debería reflejar eso, no definirlo.

## Fuentes usadas

- Receptor–ligand molecular docking review: https://pmc.ncbi.nlm.nih.gov/articles/PMC5425711/
- GNINA 1.0 and docking pipeline overview: https://pmc.ncbi.nlm.nih.gov/articles/PMC8191141/
- Redocking / RMSD protocol example: https://pmc.ncbi.nlm.nih.gov/articles/PMC9116214/
- Ensemble docking review: https://pmc.ncbi.nlm.nih.gov/articles/PMC6129458/
- Consensus and ensemble docking example: https://pmc.ncbi.nlm.nih.gov/articles/PMC8169699/
- Pose/rank consensus in docking-based screening: https://pmc.ncbi.nlm.nih.gov/articles/PMC8965822/
- OECD (Q)SAR Assessment Framework: https://www.oecd.org/content/dam/oecd/en/publications/reports/2023/11/q-sar-assessment-framework-guidance-for-the-regulatory-assessment-of-quantitative-structure-activity-relationship-models-and-predictions_9b064821/d96118f6-en.pdf
- OECD validation guidance for (Q)SAR models: https://www.oecd.org/content/dam/oecd/en/publications/reports/2014/09/guidance-document-on-the-validation-of-quantitative-structure-activity-relationship-q-sar-models_g1ghcc68/9789264085442-en.pdf
- QSAR mathematical methods review: https://pmc.ncbi.nlm.nih.gov/articles/PMC2695261/
- Fingerprint similarity search review: https://pubmed.ncbi.nlm.nih.gov/20838967/
- Overview of fingerprint similarity search in screening: https://pubmed.ncbi.nlm.nih.gov/26558489/
- Pharmacophore modeling concepts and applications: https://pubmed.ncbi.nlm.nih.gov/26703541/
- Drug design by pharmacophore and virtual screening review: https://pubmed.ncbi.nlm.nih.gov/35631472/
