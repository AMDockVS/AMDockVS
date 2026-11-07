# Guía de estructura modular del backend de AMDock

Este documento traduce el inventario de capacidades computacionales a una estructura de programación concreta.

No define todavía la UI. Define cómo debería organizarse el backend para que:

- los cálculos comunes no queden enterrados en jobs;
- las capacidades de dominio no se mezclen entre sí;
- `jobs/` quede como adaptación de ejecución y persistencia;
- la UI consuma una estructura estable.

## 1. Principio rector

AMDock es ante todo un backend de cálculos químicos y computacionales.

Por lo tanto:

- la arquitectura no debe partir de pantallas;
- la arquitectura no debe partir de docks o workspaces;
- la arquitectura debe partir de `métodos comunes`, `capacidades` y `artefactos`.

La UI debe reflejar esa estructura, no definirla.

## 2. Regla de separación

Usar esta regla simple:

- si algo se reutiliza entre varios workflows, va a `chemistry/`;
- si algo define una capacidad de negocio específica, va a su submódulo propio;
- si algo sólo adapta a MolSuite/jobs/persistencia, va a `jobs/`;
- si algo sólo representa o dispara acciones desde GUI, va a `ui/`.

## 3. Estructura objetivo recomendada

```text
src/amdock/
  chemistry/
    io.py
    standardization.py
    protonation.py
    conformers.py
    descriptors.py
    fingerprints.py
    similarity.py
    substructure.py
    alignment.py
    rmsd.py

  docking/
    preparation.py
    engines.py
    scoring.py
    redocking.py
    interactions.py

  qsar/
    activities.py
    datasets.py
    features.py
    modeling.py
    validation.py
    applicability.py

  analysis/
    pca.py
    clustering.py
    diversity.py

  io/
    loaders.py

  jobs/
    ingestion.py
    descriptors.py
    docking.py
    qsar.py

  models.py
  queries.py
  summaries.py
  runtime.py
  ui/
```

## 4. Rol de cada módulo principal

### 4.1 `chemistry/`

Kernel químico compartido.

Aquí van métodos reutilizables por más de una capacidad:

- lectura y normalización de moléculas;
- generación de 3D/conformeros;
- cálculo de descriptores;
- cálculo de fingerprints;
- similitud y subestructura;
- alineamiento;
- RMSD;
- protonación y otras transformaciones generales.

`chemistry/` no debe saber de PyMOL, de workspaces ni de jobs.

### 4.2 `docking/`

Lógica específica de docking y redocking:

- preparación específica para el motor de docking;
- definición de sitio/caja;
- ejecución de docking;
- scoring/rescoring;
- redocking;
- análisis de interacciones.

Puede depender de `chemistry/`, pero no al revés.

### 4.3 `qsar/`

Lógica específica de actividad, datasets y modelado:

- actividad/endpoint;
- ensamblado de matrices de features;
- selección de features;
- entrenamiento;
- validación;
- applicability domain;
- predicción.

Puede depender de `chemistry/`, pero no al revés.

### 4.4 `analysis/`

Capacidades analíticas compartidas sobre resultados ya computados:

- PCA;
- clustering;
- diversidad;
- comparaciones y resúmenes exploratorios.

### 4.5 `jobs/`

Capa de integración con MolSuite/executor.

Aquí debería quedar sólo:

- `Params` de ejecución;
- wiring de `@task` y `@job`;
- lectura/escritura de lotes;
- llamadas a capacidades del backend real.

La ciencia principal no debería vivir aquí.

## 5. Regla práctica de dependencia

La dirección deseada es:

```text
ui -> runtime -> jobs / queries / capacidades
jobs -> docking / qsar / chemistry / io
docking -> chemistry
qsar -> chemistry
analysis -> chemistry / qsar / docking
chemistry -> librerías científicas externas
```

Evitar:

```text
chemistry -> docking
chemistry -> qsar
ui -> jobs directos
ui -> SQL o project_rows directo
jobs -> toda la lógica científica
```

## 6. Qué ya existe y qué conviene hacer primero

Estado actual del árbol:

- ya existen carpetas `chemistry/`, `docking/`, `qsar/`, `analysis/`
- el código real todavía sigue concentrado sobre todo en:
  - `io/loaders.py`
  - `jobs/descriptors.py`
  - `jobs/docking.py`
  - `jobs/ingestion.py`

Por eso la migración recomendada no es reorganizar todo de golpe, sino extraer primero piezas compartidas y estables.

## 7. Primera oleada de migración sugerida

Orden recomendado:

1. `chemistry/descriptors.py`
2. `chemistry/fingerprints.py`
3. `chemistry/conformers.py`
4. `chemistry/rmsd.py`
5. `chemistry/alignment.py`
6. `chemistry/standardization.py`
7. `docking/interactions.py`
8. `docking/redocking.py`
9. `qsar/features.py`
10. `qsar/modeling.py`

## 8. Checklist de migración

### Fase A. Crear el kernel químico reutilizable

- [x] Crear `../src/amdockvs/chemistry/`
- [x] Extraer cálculo de descriptores desde `jobs/descriptors.py`
- [x] Crear `chemistry/fingerprints.py` y centralizar utilidades base de fingerprints
- [x] Crear `chemistry/conformers.py` y centralizar utilidades base de 3D/conformeros
- [x] Crear `chemistry/rmsd.py` y centralizar utilidades base de RMSD
- [x] Crear `chemistry/alignment.py` y centralizar utilidades base de alineamiento
- [x] Crear `chemistry/standardization.py` y centralizar utilidades base de normalización

Nota de estado de Fase A:

- fuera de `descriptors`, no había todavía lógica previa en AMDock para migrar desde otros módulos;
- por eso esta fase deja creado el kernel compartido y exportado desde `amdock.chemistry`;
- la siguiente oleada debe conectar callers reales de `io/`, `docking/` y `qsar/` a estas APIs.

### Fase B. Aislar lógica específica de docking

- [x] Crear `../src/amdockvs/docking/`
- [x] Mover expansión/preparación de pares de docking a `docking/preparation.py`
- [x] Mover ejecución mock de docking a `docking/engines.py`
- [x] Crear punto de entrada explícito para interacciones en `docking/interactions.py`

Nota de estado de Fase B:

- la preparación real de receptor/ligando para un motor concreto todavía no existe en AMDock;
- por eso `docking/preparation.py` contiene hoy la preparación de pares y batching de dominio;
- `docking/interactions.py` queda como contrato estable, pero sin análisis estructural real aún.
- [ ] Mover redocking a `docking/redocking.py`
- [ ] Dejar `jobs/docking.py` como capa fina

### Fase C. Aislar lógica específica de QSAR

- [x] Crear `../src/amdockvs/qsar/`
- [ ] Crear `qsar/activities.py`
- [ ] Crear `qsar/features.py`
- [ ] Crear `qsar/modeling.py`
- [ ] Crear `qsar/validation.py`
- [ ] Crear `qsar/applicability.py`
- [ ] Dejar `jobs/qsar.py` como capa fina cuando aparezca

### Fase D. Crear analítica transversal

- [x] Crear `../src/amdockvs/analysis/`
- [ ] Crear `analysis/pca.py`
- [ ] Crear `analysis/clustering.py`
- [ ] Crear `analysis/diversity.py`

### Fase E. Adelgazar jobs

- [ ] Hacer que `jobs/descriptors.py` sólo parsee parámetros y llame a `chemistry.descriptors`
- [ ] Hacer que `jobs/docking.py` sólo coordine `docking.*` y persistencia
- [ ] Hacer que `jobs/ingestion.py` quede centrado en orquestación, no en química

## 9. Criterio para cerrar cada extracción

Una pieza se considera bien migrada cuando:

- tiene un módulo propio fuera de `jobs/`;
- se puede importar y probar sin GUI;
- no depende de MolSuite para su lógica central;
- `jobs/` sólo la llama, no la implementa;
- el nombre del módulo refleja capacidad científica real.

## 10. Regla importante sobre nombres

No usar `Chem` como módulo interno principal.

Motivo:

- se confunde con `rdkit.Chem`;
- no comunica si el módulo es de utilidades o de backend propio.

Preferir:

- `chemistry/`
- `molecular/`

En AMDock, `chemistry/` parece el nombre más claro.
