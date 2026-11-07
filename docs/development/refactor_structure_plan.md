# Plan de Refactor de Estructura para AMDockVS

Nota:

- este plan es histórico como guía estructural
- donde diga `AMDockRuntime`, la implementación vigente es `AMDockVSRuntime`
- la ruta pública vigente es `from amdock import AMDockVSRuntime`

Este documento define la reorganizacion recomendada para `AMDockVS` a partir de lo aprendido en la evolucion de `MolSuite`.

La idea central es simple:

- `MolSuite` es el engine reusable
- `AMDockVS` es una app de producto
- la arquitectura de `AMDockVS` debe ser mas delgada, mas orientada a casos de uso y menos duplicada

Tambien hay una decision de lenguaje:

- no forzar nomenclatura tipo `domain`, `infra` o `runtime` si el equipo no habla asi
- preferir nombres simples y literales cuando comuniquen mejor

## 1. Diagnostico actual

La direccion general del paquete `src/amdockvs` es correcta:

- existe una frontera publica de producto en `AMDockRuntime`
- la logica de dominio ya esta separada de la GUI
- los workflows viven como backend interno

Pero el repo completo todavia mezcla varias capas y epocas:

- raiz con material de producto, demo, scratch y legado
- `old/` y `src/amdockvs_old/` conviven con la implementacion activa
- `runtime.py` concentra demasiada responsabilidad
- `domain/specs/` duplica abstracciones que ya existen en `MolSuite`
- algunos imports siguen acoplados a `molsuite.core.*`
- la GUI actual todavia tiene caracter de experimento

## 2. Principios de diseno

Principios que deberian gobernar la estructura objetivo:

- la API publica de AMDockVS es de producto, no de engine
- `AMDockRuntime` es la frontera estable para GUI, scripts y notebooks
- los jobs son backend interno, no interfaz primaria
- `AMDockVS` no debe recrear una mini-version de `MolSuite`
- los detalles de storage y engine deben bajar de nivel
- el repo raiz debe mostrar con claridad cual es la ruta oficial

## 3. Problemas estructurales concretos

### 3.1. Repo raiz ruidoso

Hoy el repo mezcla:

- codigo activo
- codigo legado
- demos
- datos pesados
- notebooks sueltos
- scratch scripts

Eso hace mas dificil:

- entender el entrypoint oficial
- navegar el codigo
- empaquetar la app
- mantener una narrativa clara para nuevos cambios

### 3.2. Runtime demasiado grande

`src/amdockvs/runtime.py` hoy concentra:

- DTO mapping
- advanced API
- layout del proyecto
- submit de jobs
- coordinacion de casos de uso
- waiting y polling
- queries de lectura

Eso no es grave en MVP, pero ya es una señal de que hay que modularizar.

### 3.3. Duplicacion conceptual con MolSuite

`domain/specs/` hoy replica ideas que ya resuelve `MolSuite`:

- input source
- output sink
- workflow spec
- workflow launcher

Eso agrega una capa extra sin aportar una identidad de producto clara.

### 3.4. Acoplamiento innecesario a internals de MolSuite

`AMDockVS` deberia consumir preferentemente la superficie publica de `MolSuite`.

Importar desde `molsuite.core.*` debe quedar restringido a casos realmente inevitables.

## 4. Estructura objetivo recomendada

Estructura objetivo para el paquete activo:

```text
src/amdockvs/
  __init__.py
  __main__.py
  app.py
  constants.py
  api/
    __init__.py
    facade.py
    projects.py
    jobs.py
    queries.py
    _advanced.py
  models.py
  summaries.py
  ingestion/
    __init__.py
    service.py
    job.py
    task.py
  descriptors/
    __init__.py
    service.py
    job.py
    task.py
  docking/
    __init__.py
    service.py
    job.py
    task.py
  tasks/
    __init__.py
    registry.py
    shared.py
  io/
    __init__.py
    project_layout.py
    loaders.py
  ui/
    __init__.py
    main.py
  _internal/
    __init__.py
    adapters.py
```

Alternativa valida si una feature no necesita `task.py` propio:

```text
src/amdockvs/docking/
  __init__.py
  service.py
  job.py
```

La regla no es forzar archivos vacios. La regla es que la feature tenga una carpeta clara.

## 4.1. Por que estructura por feature

Ventajas de esta opcion:

- evita carpetas paralelas con archivos del mismo nombre
- hace mas facil navegar una feature completa
- junta en un mismo lugar la logica de negocio y su wiring de ejecucion
- suena mas natural para un equipo pequeno o mediano

La lectura queda asi:

- `docking/service.py`: logica de negocio de docking
- `docking/job.py`: job del engine para docking
- `docking/task.py`: task importable si hace falta separarla

## 4.2. Regla de naming

Dentro de cada feature:

- `service.py` hace trabajo de negocio reutilizable
- `job.py` define el job o jobs de esa feature
- `task.py` define tasks del engine solo si realmente hace falta

Evitar:

- `services/docking.py` y `jobs/docking.py` en paralelo
- carpetas abstractas si la feature puede explicarse sola
- introducir mas de una ruta “oficial” para la misma feature

Decision para desarrollo activo:

- usar `amdockvs.api` como unica ruta oficial del runtime
- mantener `AMDockRuntime` como nombre de la fachada publica

## 5. Reglas por capa

### 5.1. `api/`

Responsabilidad:

- exponer la API publica de producto
- coordinar casos de uso
- devolver DTOs y estados amigables

No debe contener:

- definiciones reales de jobs
- logica quimica pesada
- SQL directo disperso

Nota:

- si `AMDockRuntime` se conserva como nombre publico, puede vivir dentro de `api/facade.py`

### 5.2. `models.py`, `summaries.py` y features (`ingestion/`, `descriptors/`, `docking/`)

Responsabilidad:

- tipos de negocio
- logica pura o casi pura
- servicios reutilizables por jobs y runtime
- wiring local de cada feature cuando mejora la navegacion

No debe contener:

- widgets
- polling de jobs
- wiring del engine

Nota:

- el wiring de job puede vivir dentro de la carpeta de feature si eso evita saltos innecesarios

### 5.3. `tasks/`

Responsabilidad:

- registro compartido de tasks o helpers comunes del engine
- piezas transversales que no pertenecen a una sola feature

No debe ser:

- la superficie publica principal del producto
- una segunda capa paralela a las features sin necesidad real

### 5.4. `io/`

Responsabilidad:

- loaders de archivos
- layout de carpetas
- adaptadores de IO

### 5.5. `ui/`

Responsabilidad:

- interfaz
- coordinacion de eventos de usuario
- consumo de `AMDockRuntime`

No debe contener:

- reglas de negocio
- lectura directa de tablas del proyecto si el runtime ya cubre el caso

### 5.6. `_internal/`

Responsabilidad:

- adaptadores internos
- piezas que no son parte de la API oficial

Regla:

- cualquier cosa en `_internal/` se asume no estable

## 6. Decision importante: eliminar `domain/specs/`

Recomendacion:

- no seguir expandiendo `domain/specs/`
- migrar los workflows activos a definiciones directas con `molsuite.api`, `molsuite.job_templates` o `molsuite.tasking`
- dejar que `MolSuite` sea la unica capa de especificacion de workflows

Motivo:

- evita duplicacion
- reduce acoplamiento conceptual
- simplifica onboarding

Recomendacion de nomenclatura:

- si esa carpeta sigue viva un tiempo, renombrarla luego a algo menos abstracto o directamente eliminarla
- no introducir nuevas carpetas llamadas `specs` salvo que sean realmente parte del vocabulario del producto

## 7. Decision importante: separar producto, demo y legado

Recomendacion de raiz:

- mover demos a `examples/`
- mover notebooks a `notebooks/`
- mover scripts temporales a `scripts/` o eliminarlos
- sacar `old/` y `src/amdockvs_old/` del arbol principal de trabajo
- no versionar datasets pesados dentro del repo de producto salvo muestras pequenas

## 8. Fases recomendadas

### Fase 1. Limpiar la narrativa del repo

Objetivo:

- hacer evidente cual es la implementacion oficial

Incluye:

- catalogar y aislar legacy
- mover demos y scratch fuera de la ruta principal
- crear entrypoints consistentes

### Fase 2. Partir el runtime

Objetivo:

- mantener `AMDockRuntime` como fachada, pero dividir internamente por responsabilidad

Incluye:

- sacar advanced API
- sacar queries de lectura
- sacar coordinacion de proyectos y jobs
- mover la implementacion interna a `api/`
- usar `amdockvs.api` como unica ruta oficial del runtime
- retirar `runtime.py`

### Fase 3. Reorganizar workflows por feature

Objetivo:

- dejar claro que el backend interno se organiza por feature y no por carpetas paralelas

Incluye:

- mover `workflows/ingestion.py` a `ingestion/job.py`
- mover `workflows/descriptors.py` a `descriptors/job.py`
- mover `workflows/docking.py` a `docking/job.py`
- mover la logica de negocio asociada a `service.py` dentro de cada feature
- dejar un `tasks/registry.py` solo para piezas compartidas si sigue haciendo falta

### Fase 4. Eliminar capas duplicadas

Objetivo:

- reducir abstracciones paralelas a `MolSuite`

Incluye:

- retirar `domain/specs/`
- migrar sus consumidores a primitivas publicas de `MolSuite`

### Fase 5. Endurecer el boundary UI-runtime

Objetivo:

- hacer que la UI dependa solo del runtime

Incluye:

- definir contrato de operacion de jobs para GUI
- usar helpers publicos de `MolSuite` a traves de `AMDockRuntime`

## 9. Criterios de exito

La reorganizacion se considera lograda cuando:

- el repo muestra una unica ruta oficial de producto
- `AMDockRuntime` sigue siendo la fachada publica, pero con implementacion modular en `api/`
- `domain/specs/` deja de ser necesario
- cada feature vive en su propia carpeta y no en dos arboles paralelos
- la GUI deja de depender de wiring experimental
- los scripts y notebooks consumen la misma API de producto
- los nombres del arbol principal suenan naturales para el equipo
