l Cascade Chat Conversation

  Note: _This is purely the output of the chat conversation and does not contain any raw data, codebase snippets, etc. used to generate the output._

### User Input

Revisa amdock en especial la parte de su "backend" y dime 
- valoracion general
- que falta
- complejidad de la estructura de codigo
- complejidad semantica para uso y desarrollo
- problemas de integracion del codigo de workflows de notebook_api_and_workflows.md en notebook y UI con pyside6

*Found code context*

*Viewed [notebook_api_and_workflows.md](file:///home/mario/PycharmProjects/AMDockVS/docs/notebook_api_and_workflows.md) *

*Listed directory [amdock](file:///home/mario/PycharmProjects/AMDockVS/src/amdock) *

*Viewed [__init__.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/__init__.py) *

*Searched filesystem*

*Viewed [loader.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/loader.py) *

*Viewed [inventory.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/inventory.py) *

*Viewed [qsar.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/qsar.py) *

*Viewed [app.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/app.py) *

*Grep searched codebase*

*Viewed [docking.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py) *

*Listed directory [ui](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/ui) *

*Viewed [common.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/common.py) *

*Viewed [main_window.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/ui/main_window.py) *

### Planner Response

Tengo suficiente contexto para dar una evaluación completa.

---

## Valoración General del Backend de AMDock

### 1. Arquitectura y Diseño

**Puntos Positivos:**
- **Separación por dominios bien definida**: `loader`, `ligands`, `receptors`, [docking](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/docking:0:0-0:0), [qsar](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/qsar:0:0-0:0) como facades separadas
- **Integración limpia con MolSuite**: El runtime hereda de `AppRuntime` y delega ejecución a jobs de MolSuite
- **Modelo de sets materializados**: `LigandSet`, `ReceptorSet` con items en tablas separadas (buena práctica para escalabilidad)
- **Scopes perezosos**: [LigandScope](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/common.py:63:0-67:36)/[ReceptorScope](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/common.py:70:0-74:36) permiten construir selecciones sin materializar IDs en memoria

**Problemas Arquitectónicos:**
- `AMDockVSRuntime` tiene ~295 líneas y aún mezcla responsabilidades (job management, queries, project handling)
- No hay `StructureAPI` ni `AnalysisAPI` que el documento de workflows propone
- [create_window()](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/app.py:16:0-19:10) en [app.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/app.py:0:0-0:0) está vacío (solo retorna `None`)
- La UI usa `RibbonMainWindow` de NeoRibbon pero no hay contribuciones de ribbon definidas para workflows científicos

---

### 2. Qué Falta (vs. [notebook_api_and_workflows.md](file:///home/mario/PycharmProjects/AMDockVS/docs/notebook_api_and_workflows.md:0:0-0:0))

| Componente | Estado | Faltante |
|------------|--------|----------|
| `loader` | ✅ 80% | `load_text()`, `load_url()` |
| `ligands` | ✅ 70% | [filter()](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/inventory.py:55:4-68:9) con expresiones ricas, `deduplicate()`, validación química |
| `receptors` | ✅ 60% | Selección de receptor activo, residuos flexibles |
| [docking](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/docking:0:0-0:0) | ⚠️ 50% | `prepare_ligands()`, `prepare_receptors()`, `set_grid()`, `reverse()`, `redock()`, `interactions()` |
| [qsar](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/qsar:0:0-0:0) | ✅ 75% | `compute_fingerprints()`, evaluación cruzada |
| `structure` | ❌ 0% | No existe `StructureAPI` |
| [analysis](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/analysis:0:0-0:0) | ❌ 0% | No existe `AnalysisAPI` |

**Métodos críticos no implementados:**
```python
# En docking.py - no existen:
amdock.docking.prepare_ligands(engine="vina")   # ❌
amdock.docking.prepare_receptors(engine="vina") # ❌
amdock.docking.set_grid(receptor_id=1, ...)     # ❌
amdock.docking.reverse(...)                       # ❌
amdock.docking.redock(...)                      # ❌
amdock.docking.interactions(...)                # ❌

# En ligands.py - no existen:
amdock.ligands.deduplicate(method="canonical_smiles")  # ❌
amdock.ligands.filter(scope, filters={"mw < 700"})     # ⚠️ básico
```

---

### 3. Complejidad de Estructura de Código

**Complejidad: MEDIA-ALTA**

**Métricas:**
- [runtime_facades/docking.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:0:0-0:0): 227 líneas (método [screen()](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:102:4-202:9) solo: ~100 líneas)
- [runtime_facades/qsar.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/qsar.py:0:0-0:0): 754 líneas (muy denso, mezcla SQL + ML + I/O)
- [runtime_facades/inventory.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/inventory.py:0:0-0:0): 219 líneas (bien estructurado)
- `docking/engines.py`: 172 líneas (bien enfocado)

**Problemas de estructura:**
1. **Acoplamiento SQL/ML**: [qsar.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/qsar.py:0:0-0:0) mezcla queries SQLModel, NumPy, scikit-learn en un mismo archivo
2. **Repetición de lógica de batching**: El patrón de chunking se repite en `loader`, [docking](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/docking:0:0-0:0), [qsar](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/qsar:0:0-0:0) sin abstracción común
3. **Falta de capa de servicios**: Los facades hablan directamente a la DB; no hay capa intermedia para lógica de dominio
4. **Import circulares potenciales**: [runtime_facades/__init__.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/__init__.py:0:0-0:0) importa todo, cualquier cambio cascada

**Estructura de archivos actual:**
```
src/amdock/
├── runtime.py           # 295 líneas - coordinador
├── runtime_facades/
│   ├── __init__.py      # exports
│   ├── common.py        # utils compartidas
│   ├── docking.py       # 227 líneas
│   ├── inventory.py     # 219 líneas
│   ├── loader.py        # 134 líneas
│   └── qsar.py          # 754 líneas (!)
├── docking/
│   ├── engines.py       # Vina wrapping
│   ├── preparation.py   # batching de pares
│   └── ...
└── ui/
    ├── main_window.py   # 314 líneas
    └── ...
```

---

### 4. Complejidad Semántica para Uso y Desarrollo

**Para usuarios de notebooks (API pública):**

| Aspecto | Valoración | Notas |
|---------|------------|-------|
| Descubribilidad | ⚠️ Media | [amdock.docking.run()](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:41:4-100:9) existe, pero `prepare_ligands()` no → inconsistencia |
| Consistencia | ⚠️ Media | Algunos métodos retornan `job_id`, otros bloquean, otros retornan objetos |
| Documentación | ❌ Baja | Sin docstrings en métodos públicos de facades |
| Type hints | ✅ Buena | Uso extensivo de Python 3.10+ features |

**Ejemplo de inconsistencia semántica:**
```python
# Algunos métodos retornan job_id (async)
job_id = amdock.docking.run(...)

# Otros bloquean y retornan resultados (sync)
summary = amdock.docking.screen(..., wait=True)  # bloquea

# Otros son completamente sync y retornan refs
dataset_ref = amdock.qsar.create_dataset(...)
```

**Para desarrolladores:**

| Aspecto | Valoración |
|---------|------------|
| Testing | ❌ Difícil - acoplamiento a DB en facades |
| Extensibilidad | ⚠️ Media - agregar nuevo motor de docking requiere tocar 3+ archivos |
| Debugging | ❌ Difícil - jobs async en MolSuite, stack traces fragmentados |

---

### 5. Problemas de Integración en Notebook y UI con PySide6

#### **Problema 1: Doble naturaleza sync/async**

El documento de workflows propone una API síncrona para notebooks:
```python
amdock.docking.run(...)  # -> job_id
amdock.wait_for_job(...)  # explícito
```

Pero [screen()](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:102:4-202:9) es híbrido (`wait=True` opcional). Esto crea confusión sobre cuándo se está ejecutando código vs. schedulando jobs.

#### **Problema 2: PySide6 + Jobs de MolSuite**

En `main_window.py:57`:
```python
self.monitor_bridge = runtime.create_monitor_bridge(poll_ms=500, max_recent_jobs=50)
```

**Problema**: El bridge hace polling cada 500ms en el thread de UI. Si hay muchos jobs, el `list_jobs()` puede bloquear momentáneamente.

#### **Problema 3: No hay adaptador UI-Runtime**

La UI no tiene una capa de adaptación que:
- Convierta clicks en llamadas al runtime
- Maneje estados de "procesando" / "completado"
- Sincronice selecciones visuales con [LigandScope](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/common.py:63:0-67:36)

Actualmente [workspace.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/ui/workspace.py:0:0-0:0) (20k líneas) maneja árboles visuales pero no hay conexión clara con los scopes del backend.

#### **Problema 4: Preparación PDBQT ausente**

El workflow canónico requiere:
```python
amdock.docking.prepare_ligands(engine="vina")
```

Pero en `engines.py` solo hay:
```python
def _require_pdbqt(path: Path, *, kind: str) -> None:
    if path.suffix.lower() != ".pdbqt":
        raise ValueError(f"AutoDock Vina requires {kind} in PDBQT format...")
```

**No hay preparación real** - el código asume que los PDBQT ya existen. Esto rompe cualquier workflow desde cero.

#### **Problema 5: Faltan componentes UI para workflows**

Según el documento, debería haber:
- UI para `set_grid()` (visualizador 3D + caja)
- UI para [ligands.filter()](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/inventory.py:55:4-68:9) (query builder)
- UI para `analysis.cluster()` (visualizador de clusters)

No existen estos widgets. [main_content.py](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/ui/main_content.py:0:0-0:0) tiene solo 2,938 líneas y es básicamente un placeholder.

---

## Resumen Ejecutivo

| Categoría | Puntuación (1-5) | Prioridad de mejora |
|-----------|------------------|---------------------|
| Arquitectura base | 3.5/5 | Media |
| Cobertura de features | 2.5/5 | **Alta** |
| Complejidad de código | 3/5 (alta) | Media |
| Usabilidad notebook | 3/5 | Media |
| Integración UI | 2/5 | **Alta** |

**Recomendación inmediata**: Implementar `prepare_ligands()` y `prepare_receptors()` con Meeko/RDKit para tener un workflow de docking completo funcional. Sin esto, el backend es inusable para docking real desde notebooks o UI.

### User Input

1. podemos hacer un refactoring mas adelante cuando todo este funcional
2. si, siguen faltando cosas
3.1 definitivamente esto no debe pasar, para eso tenemos un api para la db con orm. 
3.2 cual seria esa logica?
3.3 definitivamente esto no puede pasar
4.1 tienes razon, deberia existir dado que cada preparacion depende del software
4.2 Esto es un problema. La fuente de verdad deberia ser la db porque eso nos da la asincronia. Ello porque al trabajar con millones de compuestos y operaciones, no creo que debamos retornar objetos, a menos que sean generadores. EL problema tambien con eso es que tener un generador de salida hace que el metodo bloquee el hilo prinicipal. para notebooks pudiera ser util, en ui se pudiera meter en un qthread, pero no estoy seguro. Hay que homegenizar esto. que propones? 
4.3 los pondremos en el futuro
4.testing se arregla cuando quitemos esa inconsistencia
4.Extensibilidad Esta es una de mis preocupaciones. Implementar algo nuevo debe ser simple. Entiendo por ejemplo que un nuevo algortimo debe tener sus modelos de input, outputs, etc, entonces pudiera requerir varios archivos, el problema es que tan distantes en agrupacion y funciones estan.
5.1 Esto hay que normalizarlo. Ya sea porque los metodos sean hibridos teniendo un argumento wait=True, o teniendo metodos separados o haciendo que todo por naturaleza sea async mediante la delegacion a molsuite. El problema de hacerlo todo sync es que tendria que crear un pool de thread en pyside y se corre el riesgo de retornar objetos grandes que saturen la memoria. que propones?
5.2 hay que solucionarlo. como propones hacerlo?
5.3 eso esta pendiente porque estamos trabajando la ui sin terminar  el runtime. entonces detuvimos el desarrollo de la ui en favor de terminar el runtime. entonces, una vez que el runtime este terminado, procedemos con el bridge entre este y la ui considerando si seran sync o async
5.4  Si, esto falta en el workflow.
5.5 esto lo solucionamos mas adelante.


### Planner Response

Respondo punto a punto con propuestas concretas:

---

## 3.2 ¿Qué lógica separar?

Propongo **tres capas** en lugar de facades mezclados:

```python
# 1. Domain Services (lógica pura, sin DB ni I/O)
class LigandService:
    def deduplicate(self, molecules: Iterable[Mol]) -> Iterator[Mol]: ...
    def validate_chemistry(self, mol: Mol) -> ValidationResult: ...

class DockingService:
    def build_pairs(self, ligands, receptors) -> Iterator[DockingPair]: ...
    def validate_grid(self, box_center, box_size) -> GridValidation: ...

# 2. Data Access (solo ORM/SQLModel)
class LigandRepository:
    def get_by_scope(self, scope: LigandScope) -> Iterator[LigandRecord]: ...
    def save_set(self, name: str, ligand_ids: list[int]) -> LigandSetRef: ...

# 3. Runtime Facade (orquesta 1+2 + jobs)
class LigandAPI:
    def deduplicate(self, scope: LigandScope) -> LigandSetRef:
        # 1. Obtiene stream desde repo
        # 2. Aplica LigandService.deduplicate()
        # 3. Guarda resultado vía repo
        # 4. Retorna ref (ligero)
```

**Regla**: Si usa `session.exec()` va en Repository. Si usa RDKit/NumPy va en Service. Si coordina ambos + jobs, va en Facade.

---

## 4.2 / 5.1 Propuesta: API Dual Sync/Async

Dado el contexto PySide + notebooks, propuso este patrón:

```python
class DockingAPI:
    # Método base SIEMPRE async (retorna job_id inmediato)
    def run_async(self, ..., executor_name="process") -> str:
        return self.runtime.submit_job(docking_job, params=...)
    
    # Conveniencia sync solo para notebooks/scripts simples
    def run(self, *, wait: bool = False, timeout_s: float = 3600.0, 
            poll_s: float = 0.5, **kwargs) -> str | JobResult:
        job_id = self.run_async(**{k: v for k, v in kwargs.items() 
                                    if k not in ('wait', 'timeout_s', 'poll_s')})
        if not wait:
            return job_id  # str
        
        # Solo si wait=True, bloquea
        return self.runtime.wait_for_job(job_id, timeout_s=timeout_s, poll_s=poll_s)
```

**Para UI (PySide6)**:
```python
class DockingWorker(QObject):
    finished = Signal(object)
    progress = Signal(str)  # job_id, status
    
    def __init__(self, runtime: AMDockVSRuntime):
        super().__init__()
        self.runtime = runtime
        self._job_ids: list[str] = []
    
    def run_docking(self, **params):
        # En thread separado
        job_id = self.runtime.docking.run_async(**params)
        self._job_ids.append(job_id)
        
        # Polling no bloqueante vía QTimer en el worker
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._check_status)
        self._poll_timer.start(500)  # 500ms
    
    def _check_status(self):
        for job_id in list(self._job_ids):
            status = self.runtime.get_job_status(job_id)
            self.progress.emit(f"{job_id}:{status.status}")
            if status.status in TERMINAL_STATUSES:
                self._job_ids.remove(job_id)
                if not self._job_ids:
                    self.finished.emit(status)
```

**Ventajas**:
- Backend siempre async (fuente de verdad en DB via MolSuite)
- UI usa QThread + señales, nunca bloquea main thread
- Notebooks pueden usar `wait=True` para simplicidad
- No hay generadores que saturen memoria (streaming vía DB cursors)

---

## 5.2 Solución al Polling en UI

**Problema actual**: `MolSuiteMonitorBridge` hace polling cada 500ms en hilo de UI.

**Propuesta: Push-based via Qt Signals**

```python
class JobMonitorBridge(QObject):
    job_started = Signal(str)      # job_id
    job_progress = Signal(str, dict)  # job_id, metadata
    job_finished = Signal(str, object)  # job_id, result/summary
    
    def __init__(self, molsuite, poll_ms: int = 500):
        super().__init__()
        self.molsuite = molsuite
        self._active_jobs: set[str] = set()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_active)
    
    def track_job(self, job_id: str):
        self._active_jobs.add(job_id)
        self.job_started.emit(job_id)
        if not self._timer.isActive():
            self._timer.start(poll_ms)
    
    def _poll_active(self):
        if not self._active_jobs:
            self._timer.stop()
            return
        
        # Batch query: un solo SELECT WHERE job_id IN (...)
        statuses = self.molsuite.get_jobs_status_batch(self._active_jobs)
        
        for job_id, status in statuses.items():
            if status.is_terminal:
                self._active_jobs.discard(job_id)
                self.job_finished.emit(job_id, status)
            else:
                self.job_progress.emit(job_id, status.to_dict())
```

**En UI**:
```python
class DockingWidget(QWidget):
    def submit_docking(self):
        job_id = self.runtime.docking.run_async(...)
        self.monitor_bridge.track_job(job_id)
        self.monitor_bridge.job_finished.connect(self._on_docking_done)
    
    def _on_docking_done(self, job_id: str, result):
        # Actualiza UI sin polling constante
        self.refresh_results()
```

**Mejora adicional**: Si MolSuite soporta callbacks/webhooks en el futuro, el bridge puede escuchar eventos push en lugar de polling.

---

## Resumen de Decisiones

| Aspecto | Decisión Propuesta |
|---------|------------------|
| Capas | Service → Repository → Facade |
| Async/Sync | Base async, conveniencia `wait=True` para notebooks |
| UI Jobs | QThread + señales push-based, nunca bloquear main thread |
| Datos grandes | Streaming vía DB cursors, nunca listas completas en memoria |
| Preparación PDBQT | Nuevo job `prepare_ligands_job` con Meeko, no bloquea runtime |

¿Te parece bien esta dirección? Si sí, puedo empezar con la implementación de `prepare_ligands()` para Vina usando este patrón.

### User Input

3.2 Me parece bien. sigue separando codigo y creando nuevos archivos, pero creo que es bueno si baja la complejidad y logica.
4.2/5.1 Por que crear una funcion async directamente, si run() tiene un wait? es decir, run(wait=False) hace lo mismo que run_async() solo que con mas argumentos. Entiendo que lo simplifica bastante pero duplica. es decir, que se usa como conveniencia, no? Creo que seria buena idea tener una especie de interfaz para notebooks y scripts con tdqm y otra para ui que se active con un argumento. o seria muy complicado?
En la UI, crees que crear un QObject por job sea lo mejor? ahora mismo tenemos un poll porque esto centraliza todo el monitoreo en un hilo, en cambio si cada job tuviera un qtimer o se pusiera en un qthread seria complejo no? aunque es importante que no se van a ejecutar muchos jobs concurrentes, asi que bien puede ser una opcion. Tener objetos directos puede ser util para todo lo que tiene que ver con las senales directas, perro creo que se puede hacer algo similar con el monitor
5.2 aqui se puede poner un un qthread no y asi no bloquear de ninguna forma el hilo principal?
No modifiques nada todavia


### Planner Response

Respondo tus preguntas:

---

## 4.2/5.1 ¿Por qué `run_async()` separado si [run(wait=False)](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:41:4-100:9) existe?

Tienes razón - son duplicados. Mi error. Mejor opción:

```python
def run(self, *, wait: bool = False, **kwargs) -> str | JobStatus:
    job_id = self._submit_job(...)
    if not wait:
        return job_id  # str, inmediato
    return self.runtime.wait_for_job(job_id)  # JobStatus, bloquea
```

**Un solo método, dos comportamientos** según `wait`.

---

## Interfaces separadas: Notebook vs UI

**Opción A: Context manager (simple)**
```python
# Notebook - tqdm auto-activado
with amdock.notebook_mode():  # o amdockvs.tqdm_mode()
    job = amdock.docking.run(wait=False)
    # automáticamente muestra barra de progreso

# UI - modo silencioso/async
job = amdock.docking.run(wait=False)  # sin tqdm, retorna inmediato
```

**Opción B: Parámetro explícito (más explícito)**
```python
amdock.docking.run(wait=False, progress="tqdm")   # notebook
amdock.docking.run(wait=False, progress="qt")     # UI con señales
amdock.docking.run(wait=False, progress=None)     # silencioso
```

La Opción B es más flexible sin duplicar métodos. ¿Te parece?

---

## ¿QObject por job vs Monitor centralizado?

**Análisis de trade-offs:**

| Opción | Pros | Contras |
|--------|------|---------|
| **Monitor centralizado** (actual) | Un solo QTimer, un solo thread de polling, código centralizado | Señales genéricas, menos granularidad |
| **QObject por job** | Señales específicas por tipo de job, mejor encapsulamiento | Más objetos vivos, potencial overhead si hay 100+ jobs |

**Mi recomendación: Híbrido**

```python
class JobMonitor(QObject):  # Un solo monitor
    job_finished = Signal(str, object)      # job_id, result
    job_progress = Signal(str, int, str)  # job_id, percent, status
    
    def track(self, job_id: str, job_type: str = "generic"):
        # El job_type permite que la UI sepa qué widget actualizar
        self._jobs[job_id] = {"type": job_type, "start_time": time.monotonic()}
```

La UI se suscribe a señales del monitor central, pero filtra por `job_type`:
```python
# En widget de docking
monitor.job_finished.connect(self._on_docking_done)

def _on_docking_done(self, job_id, result):
    if job_id not in self._my_jobs:  # filtra solo mis jobs
        return
    # actualiza UI específica
```

**Ventaja**: Un solo QTimer, pero cada componente de UI escucha solo lo que le interesa.

---

## 5.2 ¿QThread para el polling?

Sí, el polling del monitor debería estar en **QThread separado**, no en el hilo principal:

```python
class MonitorWorker(QObject):
    # Señales que cruzan threads
    statuses_updated = Signal(dict)  # {job_id: status}
    
    def __init__(self, molsuite):
        super().__init__()
        self.molsuite = molsuite
        self._active_jobs: set[str] = set()
        self._running = False
    
    def run(self):  # Se ejecuta en thread separado
        self._running = True
        while self._running:
            if self._active_jobs:
                statuses = self.molsuite.get_jobs_status_batch(self._active_jobs)
                self.statuses_updated.emit(statuses)
            time.sleep(0.5)  # 500ms
    
    def stop(self):
        self._running = False

# En UI principal
self._monitor_thread = QThread()
self._monitor_worker = MonitorWorker(self.runtime.molsuite)
self._monitor_worker.moveToThread(self._monitor_thread)
self._monitor_thread.started.connect(self._monitor_worker.run)
self._monitor_thread.start()

# La UI recibe señales en hilo principal
self._monitor_worker.statuses_updated.connect(self._update_ui)
```

**Esto garantiza**: Nunca bloqueas el main thread ni con polling ni con queries a la DB.

---

## Resumen de acuerdos

| Punto | Decisión |
|-------|----------|
| Separación de capas | ✅ Sí, Service/Repository/Facade |
| Async/Sync | Un solo método [run(wait=False)](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:41:4-100:9), no duplicar |
| Progreso | Parámetro `progress="tqdm"\|"qt"\|None` |
| Monitor | QThread separado + señales, un solo timer central |
| UI por job | Filtrar señales del monitor, no QObject por job |

¿Todo correcto? ¿Algo que ajustar antes de que implemente?

### User Input

Interfaces separadas: Notebook vs UI: no, me referia a tener una especie de atributo del runtime que fuera la interfaz, algo como qt_ui: bool que cuandos es True, entonces siempre se ejecute en modo async y viceversa, a no ser que explicitamente un metodo se llame con wait. Ya veo que esto es contraproducente porque habria que forzar que cada metodo use el atributo... no se si es viable y bueno
Pense que con esto tendria la tdqm bar en scripts y notebooks y que el monitor se encargara en la ui...

### Planner Response

Entiendo. La idea del atributo `qt_ui` es tentadora pero como dices, forzaría que cada método consulte ese flag → código spaghetti.

**Propuesta mejor: Progress Reporter registrado en Runtime**

```python
class AMDockVSRuntime(AppRuntime):
    def __init__(self):
        super().__init__(...)
        self._progress_reporter: ProgressReporter | None = None
    
    def set_progress_reporter(self, reporter: ProgressReporter):
        self._progress_reporter = reporter

class ProgressReporter(ABC):
    @abstractmethod
    def on_job_submitted(self, job_id: str, description: str): ...
    @abstractmethod
    def on_job_progress(self, job_id: str, percent: float, status: str): ...
    @abstractmethod
    def on_job_finished(self, job_id: str, result: Any): ...

class TqdmReporter(ProgressReporter):
    """Para notebooks/scripts - barra de progreso con tqdm"""
    def __init__(self):
        self._bars: dict[str, tqdm] = {}
    
    def on_job_submitted(self, job_id, description):
        self._bars[job_id] = tqdm(desc=description, total=100)
    
    def on_job_progress(self, job_id, percent, status):
        if job_id in self._bars:
            self._bars[job_id].n = percent
            self._bars[job_id].refresh()
    
    def on_job_finished(self, job_id, result):
        if job_id in self._bars:
            self._bars[job_id].close()
            del self._bars[job_id]

class QtReporter(QObject, ProgressReporter):
    """Para UI - emite señales en lugar de tqdm"""
    job_submitted = Signal(str, str)      # job_id, description
    job_progress = Signal(str, float, str) # job_id, percent, status
    job_finished = Signal(str, object)    # job_id, result
    
    def on_job_submitted(self, job_id, description):
        self.job_submitted.emit(job_id, description)
    # ... etc
```

**Uso:**

```python
# Notebook - automático con tqdm
runtime = AMDockVSRuntime()
runtime.set_progress_reporter(TqdmReporter())

job_id = runtime.docking.run(wait=False)  # Aparece barra automáticamente

# UI - señales Qt
runtime.set_progress_reporter(QtReporter())
# Conectar señales a widgets...
```

**Ventaja:** Los métodos ([run](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:41:4-100:9), [screen](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:102:4-202:9), [compute_descriptors](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/qsar.py:333:4-356:9)) **no cambian**. Solo llaman a `self.runtime._progress_reporter.on_job_*()` si existe.

¿Esta dirección resuelve el problema sin complicar cada método?

### User Input

pero esto asume que cada metodo registra el progreso y que hay como senales no?

### User Input

continue

### Planner Response

Tienes razón - eso requeriría modificar cada método. **Mejor aproximación: interceptar en el polling existente**.

El monitor ya hace polling de estados. Podemos transformar cambios de estado en "progreso":

```python
class StateBasedProgressAdapter:
    """Convierte transiciones de estado de MolSuite en progreso"""
    
    STATE_WEIGHTS = {
        "pending": 0,
        "staging": 10,
        "running": 50,
        "completed": 100,
        "failed": 100,
    }
    
    def __init__(self, reporter: ProgressReporter):
        self._reporter = reporter
        self._last_state: dict[str, str] = {}
    
    def on_status_batch(self, statuses: dict[str, JobStatus]):
        for job_id, status in statuses.items():
            last = self._last_state.get(job_id)
            if last != status.status:
                # Estado cambió → reportar
                percent = self.STATE_WEIGHTS.get(status.status, 0)
                self._reporter.on_job_progress(job_id, percent, status.status)
                
                if status.is_terminal:
                    self._reporter.on_job_finished(job_id, status)
                elif last is None:
                    self._reporter.on_job_submitted(job_id, status.description)
                
                self._last_state[job_id] = status.status
```

**Flujo:**
1. `MonitorWorker` hace polling como antes
2. Pasa batch por `StateBasedProgressAdapter`
3. Adapter llama al reporter (tqdm o Qt)

**Ventaja:** Los métodos ([docking.run](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:41:4-100:9), etc.) **no se tocan**. El progreso se infiere del estado del job en MolSuite.

¿Esto resuelve la objeción? Solo el polling central necesita cambios, no cada método.

### User Input

no, porque hay jobs en los que quiero que el progreso sea mas granular... hacerlo por pasos no sirve

### Planner Response

Entiendo - necesitas progreso granular tipo "procesando ligando 450/1000", no solo estados.

**Para progreso granular, el job debe reportar internamente:**

```python
# En el job de docking (corre en proceso separado)
def run_vina_docking_batch(payload: dict):
    pairs = payload["pairs"]
    total = len(pairs)
    
    for i, pair in enumerate(pairs):
        # ... docking del par ...
        
        # Reportar progreso cada N iteraciones
        if i % 10 == 0:
            report_job_progress(
                job_id=context.job_id,
                percent=int(100 * i / total),
                message=f"Docked {i}/{total} pairs"
            )
```

**MolSuite necesitaría:**
- Tabla `job_progress` (job_id, percent, message, timestamp)
- O alternativa: el job escribe a stdout que el executor captura

**El monitor leería:**
```python
class MonitorWorker:
    def _poll_active(self):
        # Estado del job
        statuses = self.molsuite.get_jobs_status_batch(self._active_jobs)
        
        # Progreso granular (si aplica)
        for job_id in self._active_jobs:
            progress = self.molsuite.get_job_progress(job_id)
            if progress:
                self.progress_reporter.on_job_progress(
                    job_id, progress.percent, progress.message
                )
```

**Preguntas de diseño:**
1. ¿MolSuite soporta job progress granular hoy, o necesitaría implementarse?
2. ¿Prefieres progreso por pasos fijos (10%, 20%...) o por items procesados (45/100)?

¿Cómo está MolSuite en este aspecto? ¿Hay mecanismo para jobs reportar progreso?

### User Input

lo hace... molsuite se supone que reporta todo el progreso, chunks, y progreso de tareas internamente a traves del monitor... pero eso no es importante ahora

### Planner Response

Perfecto, entonces el progreso granular ya está resuelto por MolSuite.

**Resumen de lo acordado hasta ahora:**

| Aspecto | Decisión |
|---------|----------|
| Separación de capas | Service → Repository → Facade (sí hacer) |
| Async/Sync | Un solo método [run(wait=False)](file:///home/mario/PycharmProjects/AMDockVS/src/amdock/runtime_facades/docking.py:41:4-100:9), no duplicar |
| Progreso UI/Notebook | `ProgressReporter` registrado en runtime: `TqdmReporter` para notebooks, `QtReporter` para UI |
| Progreso granular | MolSuite ya lo reporta, el monitor consume de ahí |
| Monitor polling | QThread separado, no bloquea main thread |

**Próximos pasos pendientes:**
1. Implementar `prepare_ligands()` / `prepare_receptors()` para Vina (PDBQT)
2. Crear capa Repository (separar SQL de los facades)
3. Refactorizar QSAR para usar las nuevas capas

¿Hay algo más que aclarar antes de que empiece a implementar, o prefieres que proceda con alguno de estos puntos?