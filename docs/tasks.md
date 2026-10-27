# Tasks de AMDockVS

Nota:

- este checklist mezcla hitos históricos y pendientes
- donde diga `AMDockRuntime`, la fachada vigente es `AMDockVSRuntime`

Checklist de trabajo derivado de `docs/observaciones_runtime_y_api.md`, ajustado al estado actual de `MolSuite` tras el cierre de su refactor.

Plan estructural complementario:

- `docs/refactor_structure_plan.md`
- `docs/tasks_refactor_structure.md`

## Prioridad alta

### Runtime publico de dominio

- [x] `P1-1.1` Mantener `AMDockRuntime` como runtime scoped al `APP_ID` de AMDockVS.
- [x] `P1-1.2` Mantener el backend de dominio desacoplado de PySide y reutilizable desde tests/scripts.
- [x] `P1-1.3` Declarar y documentar `AMDockRuntime` como frontera publica oficial de AMDockVS.
- [x] `P1-1.4` Definir la API minima estable de dominio para proyectos, ingestion, descriptores, docking y resultados.
- [x] `P1-1.5` Reemplazar nombres genericos del runtime por casos de uso expresivos como `import_ligands(...)`, `import_receptors(...)`, `calculate_descriptors(...)` y `run_docking(...)`.
- [x] `P1-1.6` Mover `submit_registered_job(...)` a nivel avanzado y sacar esa ruta de la happy path publica.

### Flujos de dominio sobre MolSuite

- [x] `P1-2.1` Definir el flujo oficial de producto: importar ligandos y receptores, calcular descriptores, ejecutar docking y consultar resultados.
- [x] `P1-2.2` Revisar los jobs actuales para adoptar la API simple de `MolSuite` (`run_stream(...)` / `run_batch(...)`) cuando reduzca boilerplate.
- [x] `P1-2.3` Validar el flujo principal end-to-end sin tocar internals de `MolSuite`.
- [x] `P1-2.4` Alinear jobs de dominio con `project_stream(...)`, `db_input_for(...)`, `table_sink(...)` y `file_sink(...)` cuando aplique.

### API publica de resultados

- [x] `P1-3.1` Diseñar DTOs o modelos Pydantic para la salida publica del runtime.
- [x] `P1-3.2` Definir `ProjectSummary`.
- [x] `P1-3.3` Definir `LigandSummary`.
- [x] `P1-3.4` Definir `ReceptorSummary`.
- [x] `P1-3.5` Definir `DescriptorSummary`.
- [x] `P1-3.6` Definir `DockingResultSummary`.
- [x] `P1-3.7` Definir `JobStatus` para estado amigable de jobs.
- [x] `P1-3.8` Ajustar `list_*`, `wait_for_jobs(...)` y helpers de consulta para devolver DTOs en la ruta publica cuando convenga.

## Prioridad media

### Menor acoplamiento a storage

- [ ] `P2-1.1` Revisar jobs de ingestion, descriptores y docking para reducir dependencia directa de `SQLModel`.
- [ ] `P2-1.2` Diseñar una capa de consultas de dominio para ligandos, receptores y resultados.
- [ ] `P2-1.3` Evaluar helpers como `iter_ligand_inputs(...)`, `iter_receptor_inputs(...)` o equivalentes.
- [ ] `P2-1.4` Separar mejor la representacion publica del runtime respecto al schema interno de tablas.

### GUI como cliente del runtime

- [ ] `P2-2.1` Auditar la GUI para detectar logica de negocio que deba vivir en `AMDockRuntime`.
- [ ] `P2-2.2` Mantener la GUI como cliente del runtime y no del engine ni del storage directo.
- [ ] `P2-2.3` Definir un contrato claro entre AMDockVS-qt y `AMDockRuntime`.
- [ ] `P2-2.4` Asegurar que nuevas features de negocio entren primero por runtime y despues por GUI.

### Entry points y experiencia de uso

- [ ] `P2-3.1` Separar scripts demo de entrypoints de producto.
- [ ] `P2-3.2` Limpiar rutas hardcodeadas y configuracion de entorno en scripts de demostracion.
- [ ] `P2-3.3` Decidir si AMDockVS tendra CLI real o solo Python API + GUI.
- [ ] `P2-3.4` Si hay CLI, definir comandos minimos para importacion, descriptores y docking.
- [ ] `P2-3.5` Revisar si `amdockvs_old` sigue aportando algo o debe salir del repo principal.

## Prioridad baja

### DX y documentacion

- [ ] `P3-1.1` Crear un quickstart corto para `AMDockRuntime` desde script.
- [ ] `P3-1.2` Crear un quickstart de notebook para el flujo principal.
- [ ] `P3-1.3` Crear recetas separadas para ingestion, descriptores y docking.
- [ ] `P3-1.4` Documentar convenciones de estructura recomendada para AMDockVS sobre `MolSuite`.

### Analisis y exportacion

- [ ] `P3-2.1` Diseñar `get_results_dataframe()` o utilidades equivalentes.
- [ ] `P3-2.2` Diseñar exportacion a CSV o parquet para resultados.
- [ ] `P3-2.3` Preparar recetas de notebook para analisis posterior a docking.

### Observabilidad

- [ ] `P3-3.1` Diseñar como exponer progreso de jobs al runtime de forma amigable para GUI.
- [ ] `P3-3.2` Diseñar helpers para consultar eventos y estado sin tocar internals del engine.
- [ ] `P3-3.3` Revisar si se necesita una abstraccion de polling o suscripcion de alto nivel.

## Criterios de cierre

- [ ] La GUI usa `AMDockRuntime` como backend de negocio.
- [ ] Un notebook puede ejecutar el flujo principal sin tocar detalles del engine.
- [ ] Un script puede reutilizar exactamente la misma API del runtime.
- [ ] La salida publica del runtime tiene tipos de dominio claros.
- [ ] Los `jobs` y `tasks` quedan como backend interno y no como interfaz primaria.



| Caso | wall | chunks/s | util. compute | bloqueo worker por output | lag confirmacion |
|---|---:|---:|---:|---:|---:|
| SQLite serial inline, propiedades en DB | 0.740s | 216.2 | 54% | 1.310s | 0.029s |
| SQLite async 1 writer, propiedades en DB | 0.415s | 385.3 | 96% | 0.001s | 0.003s |
| PostgreSQL async 4 writers, propiedades en DB | 0.931s | 171.9 | 43% | 0.001s | 0.458s |
| PostgreSQL COPY async 1 writer, propiedades en DB | 0.511s | 313.4 | 78% | 0.001s | 0.032s |
| PostgreSQL COPY async 4 writers, propiedades en DB | 0.600s | 266.5 | 67% | 0.001s | 0.031s |
| PostgreSQL async 4 writers, propiedades en archivo | 0.417s | 383.6 | 96% | 0.002s | 0.005s |
| PostgreSQL COPY async 1 writer, propiedades en archivo | 0.415s | 385.2 | 96% | 0.001s | 0.006s |

Benchmark de capa de acceso SQLite: 168k filas por corrida, promedio de 3 corridas.

| Ruta | total | write | filas/s total | relativo |
|---|---:|---:|---:|---:|
| `sqlite3.executemany` | 0.231s | 0.227s | 726.4k | 1.00x |
| SQLAlchemy `exec_driver_sql` | 0.232s | 0.225s | 723.4k | 1.00x |
| SQLAlchemy Core `Connection.execute(table.insert())` | 0.514s | 0.508s | 327.8k | 0.45x |
| SQLAlchemy Core `Session.execute(table.insert())` | 0.500s | 0.495s | 335.9k | 0.46x |
| SQLModel `Session.execute(insert(Model))` | 0.843s | 0.837s | 199.3k | 0.27x |
| SQLModel ORM `add_all(model objects)` | 9.108s | 4.750s | 18.4k | 0.03x |
