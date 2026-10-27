# Tasks para Refactor de Estructura de AMDockVS

Checklist ejecutable derivado de `docs/refactor_structure_plan.md`, ajustado al estado real actual del repo.

Convenciones de lectura:

- `[x]` ya existe o ya fue resuelto en el repo actual.
- `[ ]` sigue pendiente o requiere una decision explicita para cerrar bien la migracion.
- cuando una tarea toca API publica, debe dejar una sola ruta oficial clara.

## Prioridad alta

### Fase 1. Consolidar la limpieza de la raiz

- [x] `R1-1.1` Consolidar `examples/` como carpeta oficial para demos reproducibles de producto.
- [x] `R1-1.2` Consolidar `notebooks/` y mantener notebooks fuera de la raiz.
- [x] `R1-1.3` Mover `scratch.py` y `ui_scratch.py` a `scripts/` o equivalente no oficial.
- [x] `R1-1.4` Congelar `old/` como legacy y evitar que compita con el arbol activo.
- [x] `R1-1.5` Sacar `amdockvs_old` del arbol activo y dejarlo solo bajo `old/` si todavia se conserva.
- [x] `R1-1.6` Revisar el contenido actual de `examples/`, `scripts/` y `old/` para confirmar que nada ahi se presenta como ruta oficial de producto.
- [x] `R1-1.7` Reducir `data/` a fixtures pequenas y layout documentado; evitar versionar proyectos locales pesados como parte de la narrativa principal.
- [x] `R1-1.8` Mantener un unico entrypoint oficial de consola para el producto: `amdockvs` / `python -m amdockvs`.
- [x] `R1-1.9` Decidir el rol de `src/amdockvs/main.py` y retirarlo si no aporta a la ruta oficial.

### Fase 2. Partir el runtime sin romper la fachada publica

- [x] `R1-2.1` Crear `src/amdockvs/api/` como hogar de la implementacion interna del runtime.
- [x] `R1-2.2` Mover `AMDockRuntime` a `api/facade.py` y retirar `src/amdockvs/runtime.py`.
- [x] `R1-2.3` Mover `AMDockAdvancedAPI` a `api/_advanced.py`.
- [x] `R1-2.4` Extraer apertura, creacion, activacion y resumen de proyectos a `api/projects.py`.
- [x] `R1-2.5` Extraer polling, espera, cancelacion, re-submit y normalizacion de estados de jobs a `api/jobs.py`.
- [x] `R1-2.6` Extraer consultas de lectura y mapping de records a summaries a `api/queries.py`.
- [x] `R1-2.7` Hacer que `api/facade.py` solo coordine casos de uso y delegue implementacion real a `projects.py`, `jobs.py` y `queries.py`.
- [x] `R1-2.8` Dejar `from amdockvs.api import AMDockRuntime` como unica ruta oficial del runtime.

### Fase 3. Alinear el paquete base con la estructura objetivo

- [x] `R1-3.1` Promover `src/amdockvs/domain/summaries.py` a `src/amdockvs/summaries.py`.
- [x] `R1-3.2` Mover `src/amdockvs/domain/models.py` a `src/amdockvs/models.py` y dejar una sola ruta oficial.
- [x] `R1-3.3` Reemplazar `src/amdockvs/infra/io/` por `src/amdockvs/io/`.
- [x] `R1-3.4` Crear `src/amdockvs/io/project_layout.py` para centralizar el layout de proyecto.
- [x] `R1-3.5` Dejar `src/amdockvs/io/loaders.py` como superficie clara para loaders de archivos.
- [x] `R1-3.6` Confirmar que `src/amdockvs/_internal/` no hace falta todavia y no crear carpeta preventiva.
- [x] `R1-3.7` Retirar `src/amdockvs/infra/` y vaciar `src/amdockvs/domain/` de re-exports y modelos duplicados.

### Fase 4. Reorganizar backend por feature

- [x] `R1-4.1` Crear `src/amdockvs/ingestion/`, `src/amdockvs/descriptors/` y `src/amdockvs/docking/`.
- [x] `R1-4.2` Mover `workflows/ingestion.py` a `ingestion/job.py`.
- [x] `R1-4.3` Mover `workflows/descriptors.py` a `descriptors/job.py`.
- [x] `R1-4.4` Mover `workflows/docking.py` a `docking/job.py`.
- [x] `R1-4.5` Mover `domain/services/ingestion.py` a `ingestion/service.py`.
- [x] `R1-4.6` Mover `domain/services/descriptors.py` a `descriptors/service.py`.
- [x] `R1-4.7` Mover `domain/services/docking.py` a `docking/service.py`.
- [x] `R1-4.8` Mover `workflows/steps/*` a `task.py` por feature donde aportaba claridad real.
- [x] `R1-4.9` Crear `src/amdockvs/tasks/registry.py` como registro transversal real de tasks compartidas.
- [x] `R1-4.10` Ajustar imports del runtime, examples y tests para usar la nueva estructura por feature.
- [x] `R1-4.11` Retirar `workflows/` y `domain/services/` al quedar sin consumidores.

### Fase 5. Mantener docs alineadas con la migracion real

- [x] `R1-5.1` Reescribir `README.md` para describir el runtime publico como frontera oficial y marcar la migracion interna.
- [x] `R1-5.2` Documentar la ruta oficial vigente: entrypoint y `from amdockvs.api import AMDockRuntime`.
- [x] `R1-5.3` Revisar `docs/runtime_api.md`, `docs/official_flow.md` y docs auxiliares para evitar que describan como presente una estructura que todavia no existe.
- [x] `R1-5.4` Actualizar este checklist cuando se cierre cada fase para que no vuelva a quedar desfasado del repo.

## Prioridad media

### Fase 6. Eliminar capas duplicadas con MolSuite

- [x] `R2-1.1` Mapear consumidores reales de `src/amdockvs/domain/specs/` dentro de `src/`, `test/`, `examples/` y `scripts/`.
- [x] `R2-1.2` Dejar de expandir `domain/specs/` inmediatamente; no agregar nuevos consumidores ni nuevas abstracciones ahi.
- [x] `R2-1.3` Reemplazar `domain/specs/workflow.py` por uso directo de primitivas publicas de `MolSuite`.
- [x] `R2-1.4` Reemplazar `domain/specs/input.py`, `output.py` y `processor.py` por tipos publicos de `MolSuite` o helpers locales de feature cuando haga falta.
- [x] `R2-1.5` Eliminar `domain/specs/` cuando ya no tenga consumidores.

### Fase 7. Bajar acoplamiento a internals de MolSuite

- [x] `R2-2.1` Revisar imports desde `molsuite.core.*` y bajar a superficie publica cuando exista alternativa.
- [x] `R2-2.2` Sacar del runtime el uso directo de `ProjectContext` y `ProjectRepository` si se puede resolver via API publica de `MolSuite`.
- [x] `R2-2.3` Revisar tests que hoy monkeypatchean `molsuite.core.settings.*` y moverlos a hooks publicos o fixtures menos acoplados.
- [x] `R2-2.4` Confirmar que jobs y tasks usan solo APIs publicas vigentes de `MolSuite` para submit, cancelacion, re-submit, polling y capabilities.
- [x] `R2-2.5` Dejar en el runtime un contrato claro para UI: listar jobs, cancelar, re-lanzar y consultar progreso sin tocar internals.

### Fase 8. Endurecer la frontera UI-runtime

- [ ] `R2-3.1` Auditar `src/amdockvs/ui/` para detectar logica de negocio mezclada con widgets o estado local experimental.
- [ ] `R2-3.2` Tratar la UI actual como cliente en migracion, no como contrato estable del backend.
- [ ] `R2-3.3` Eliminar de la UI almacenamiento local o datos hardcodeados que dupliquen el proyecto real.
- [ ] `R2-3.4` Hacer que la UI consuma solo `AMDockRuntime`, summaries publicos y view models derivados de esa API.
- [ ] `R2-3.5` Definir un adapter de GUI para operaciones de jobs, polling y refresh de vistas si hace falta.

## Prioridad baja

### Documentacion y DX

- [ ] `R3-1.1` Actualizar `README.md` nuevamente cuando la estructura objetivo ya este implementada, no solo la migracion.
- [ ] `R3-1.2` Crear `docs/quickstart_runtime.md` con la happy path oficial.
- [ ] `R3-1.3` Crear `docs/jobs_ui_contract.md` para la futura GUI.
- [ ] `R3-1.4` Documentar la estructura por feature: `service.py`, `job.py`, `task.py`.
- [ ] `R3-1.5` Documentar la politica final de imports y modulos publicos oficiales.

### Testing de estructura

- [ ] `R3-2.1` Ajustar tests para que usen el runtime publico como primera opcion.
- [ ] `R3-2.2` Agregar tests de regresion para cancelacion y re-submit de jobs desde `AMDockRuntime`.
- [ ] `R3-2.3` Agregar tests que verifiquen que la UI no necesita imports desde `molsuite.core.*`.
- [ ] `R3-2.4` Agregar tests de import para confirmar que la ruta publica oficial sigue siendo `amdockvs.api`.

## Orden sugerido de ejecucion

1. Consolidar la limpieza de la raiz y cerrar decisiones pendientes sobre entrypoints y narrativa oficial.
2. Partir el runtime y mover la implementacion a `api/`.
3. Alinear `models`, `summaries`, `io` y `_internal` con la estructura objetivo.
4. Reorganizar `workflows/` y `domain/services/` por feature.
5. Retirar `domain/specs/`.
6. Reducir acoplamiento a internals de `MolSuite`.
7. Endurecer la frontera con la UI.
8. Cerrar documentacion final y tests sobre la ruta publica oficial.

## Criterios de cierre

- [x] El repo tiene una ruta oficial clara para producto, demos, scripts y legacy.
- [x] `AMDockRuntime` sigue siendo la fachada publica y su implementacion vive modularizada en `api/`.
- [x] La estructura por feature reemplaza a `workflows/` y `domain/services/` como backend interno.
- [x] `models.py` y `summaries.py` tienen una unica ruta oficial clara y `domain/` ya no forma parte del paquete activo.
- [x] `io/` refleja la estructura objetivo y `infra/` deja de ser necesario; `_internal/` solo se crea si aparece una necesidad real.
- [x] `domain/specs/` ya no existe o esta totalmente deprecado sin consumidores reales.
- [ ] La UI depende del runtime y no de internals de `MolSuite`.
- [ ] README y docs reflejan el estado real en cada etapa y la estructura final al cierre.
