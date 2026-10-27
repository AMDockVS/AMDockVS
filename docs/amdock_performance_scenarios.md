# AMDock Performance Scenarios

## Objetivo

Validar AMDock con escenarios del dominio, no con jobs sintéticos aislados.

La carga relevante para esta app es:

- ingestión de muchos ligandos desde varios archivos
- cálculo de descriptores sobre tablas grandes del proyecto
- docking mock con muchos pares ligando-receptor y muchos outputs en disco
- ejecución concurrente de varios jobs dentro del mismo proyecto

## Escenarios canónicos

### 1. Small interactive

- decenas de ligandos
- 1 receptor
- feedback rápido
- uso típico: depuración o exploración manual

Política recomendada: `latency`

### 2. Balanced workstation

- cientos de ligandos
- 1-2 receptores
- mezcla razonable entre feedback y throughput
- uso típico: flujo diario de escritorio

Política recomendada: `balanced`

### 3. Throughput screening

- cientos o miles de ligandos
- múltiples archivos de entrada
- 2 o más receptores
- resultados voluminosos en `docking_results`

Política recomendada: `throughput`

### 4. Cancellation under load

- job largo de import o docking
- cancelación pedida desde monitor o runtime
- verificación de visibilidad operativa y cierre limpio

Estado actual:

- el escenario está definido y cubierto a nivel de contrato operativo del monitor
- todavía no hay una prueba pesada dedicada solo a cancelación de screening

## Políticas ajustadas

El banco de pruebas define tres perfiles en `tools/screening_benchmark.py` (fuera del paquete
desde 2026-08-29: ningun job de AMDock los declara, solo los mide el benchmark):

- `latency`
  - `import_batch_size=250`
  - `descriptor_batch_size=250`
  - `docking_batch_size=64`
- `balanced`
  - `import_batch_size=1000`
  - `descriptor_batch_size=1000`
  - `docking_batch_size=128`
- `throughput`
  - `import_batch_size=2000`
  - `descriptor_batch_size=1500`
  - `docking_batch_size=256`

## Criterio de tuning

Las decisiones de AMDock a nivel app se concentran en `batch_size`.

No se introdujo una política propia de flush o retention en la app porque:

- los sinks ya están resueltos por MolSuite
- la retención durable pertenece al contrato del executor, no al dominio de AMDock
- cambiar flush/retention en AMDock sin presión real de datos duplicaría knobs del engine

En esta fase, el ajuste serio y defendible fue:

- fijar perfiles de batching explícitos
- validarlos con benchmark de screening real
- usar el perfil `throughput` en la prueba e2e de carga más pesada

## Validación actual

- benchmark de screening con conteos y throughput medidos
- importación concurrente desde dos SDF
- screening más pesado con perfil `throughput`

Eso deja la app en un punto razonable para seguir con dominio/UI sin improvisar tamaños de lote.
