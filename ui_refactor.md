Plan de refactorización

Fase 0 — Congelar el comportamiento actual
Antes de mover código:

1. Ejecutar la suite completa en MSDD_312 y registrar fallos preexistentes. 
2. Añadir pruebas de caracterización para:
- Abrir/cerrar herramientas y tabs. 
- Ocupación del panel auxiliar. 
- Sincronización de toolbar.
- Refresco durante jobs.
- Carga de receptor, complejo y pose en PyMOL.
- Construcción de parámetros de docking.
3. Evitar cambios visuales durante las primeras fases.

Criterio de salida: misma suite y mismos flujos antes y después de cada extracción.

Fase 1 — Limpieza mecánica
Primer PR, pequeño y de bajo riesgo:

- [x] Eliminar el import de amdock_rc.
- [x] Eliminar amdock_rc.py y amdock.qrc.
- [x] Añadir una prueba que cargue todos los iconos mediante resources.icons.icon.
- [x] Reemplazar el import wildcard por nada: ninguno de sus símbolos es necesario.
- [x] Eliminar imports duplicados o sin uso en main_window.py.

Segundo PR:

- [x] Retirar _NullDock.
- [x] Eliminar las llamadas muertas catalog_dock.add_entry() y docking_dock.add_entry().
- [x] Revisar y retirar los docks antiguos sin consumidores:
- [x] catalog/widget.py
- [x] tools/molecules/widget.py
- [x] tools/docking/dock_widget.py
- [x] tools/qsar/widget.py
- [x] Eliminar la ruta legacy workspace.receptor; conservar workspace.receptors, que es la vista activa. 
Criterio de salida: no quedan objetos no-op ni dos sistemas paralelos de navegación.

Fase 2 — Reducir main_window.py
Convertir main_window.py en una composition root. Estructura sugerida:

```bash
ui/
  shell/
    view_coordinator.py
    tool_coordinator.py
    auxiliary_panel.py
    job_feedback.py
  viewers/
    molecular_viewer.py
  visualization/
    chart_controller.py
  main_window.py
```
Extracciones (hechas; `main_window.py` 2038 -> 703 líneas, delegadores conservados):

1. [x] `ViewCoordinator`
- Registro y apertura de vistas.
- Estado de tabs y toolbar.
- Workflow quick access.
2. [x] `ToolCoordinator`
- Montaje y desmontaje de herramientas.
- Sincronización de acciones.
- Scopes contextuales.
3. [x] `AuxiliaryPanelController`
- Details.
- Paneles de tools.
- Panel prestado por la vista activa.
4. [x] `JobFeedbackController`
- Notificaciones.
- Fallos agrupados.
- Estado de jobs y recursos.
- Refresco incremental de vistas.
5. [x] `MolecularViewerController`
- Receptores, moléculas, complejos y poses.
- Colores y contexto de PyMOL.
- Orientación de cámara y grid box.
6. [x] `ChartController`
- Histogramas.
- ROC.
- Heatmaps.
- Universo químico y glowing molecules.
Durante la migración, `AMDockVSMainWindow` debe conservar métodos delegadores como load_hit_in_pymol() para no romper widgets y pruebas existentes.

Objetivo: llevar `main_window.py` a aproximadamente 500–800 líneas.

Fase 3 — Cerrar la frontera UI/API
La UI no debería construir SQL ni abrir sesiones, excepto el adaptador SmartTableView de ms_components.

Mover a APIs AMDock:

- [x] Queries de Details → runtime.molecules.details() y runtime.complexes.details().
- [x] Listado de sets → runtime.molecules.list_sets().
- [x] Conteos y aplicación de filtros → runtime.molecules.evaluate_filter() y apply_filter().
- [x] Estados de preparación → runtime.docking.preparation_summary().
- [x] Resolución de scopes → runtime.molecules y objetos MoleculeSetRef.
- [x] Lookups de moléculas para PyMOL → runtime.molecules.get().
Las APIs continúan usando runtime.molsuite.project_db internamente. Las operaciones químicas y jobs deben seguir 
entrando por MolSuite; run_async queda como adaptador Qt para lecturas ligeras que no deben bloquear la GUI.

Criterio de salida: [x] ningún widget contiene select(...), imports de SQLAlchemy o sesiones explícitas.

Fase 4 — Refactor de Docking Studio
[x] Extraer lógica no visual de docking.py:

```
docking/
  planning.py
  readiness.py
  submission.py

ui/tools/docking/
  studio.py
  protocol_editor.py
  preparation_panel.py
  scope_panel.py
  flexible_residues.py
  run_panel.py
```
Responsabilidades:

- [x] DockingReadinessService: preparados, grids, fallos y pares existentes.
- [x] DockingSubmissionService: construir el plan y enviar run/redock mediante runtime.docking.
- [x] ProtocolEditorWidget: programas y variantes.
- [x] PreparationPanel: ligandos y receptores.
- [x] ScopePanel: selected/filtered/active.
- [x] FlexibleResiduesPanel: candidatos y selección.
- [x] RunPanel: resumen, validación y botón de ejecución.
- [x] DockingStudioWidget: únicamente coordinar pasos y paneles.
Evitar pasar diccionarios anónimos grandes. Introducir solamente modelos necesarios, por ejemplo:
```python
@dataclass(frozen=True)
class DockingRunRequest:
    run_kind: str
    ligand_scope: MoleculeSetRef
    receptor_scope: MoleculeSetRef
    protocols: tuple[DockingProtocol, ...]
    skip_existing: bool
```
Objetivo: [x] `DockingStudioWidget` por debajo de 600 líneas (233 líneas) y sin SQL.

Fase 5 — Dividir módulos heterogéneos
- workspace.py:
- tools/docking/results.py
- tools/docking/metric_columns.py
- tools/qsar/activity_table.py
- Eliminar receptor y helpers legacy.
- filter.py:
- import_prefilter.py
- filter_rules.py
- filter_tool.py
- receptors.py:
- receptor_import.py
- receptors.py conserva tabla y registro.
- pocket_detection.py:
- binding_sites_panel.py
- pocket_detection.py

Fase 6 — Componentes especializados
Prioridad posterior:

- grid_box.py: separar estado/editor de renderer PyMOL.
- diversity.py: extraer caché y estado de previews.
- qsar/panels.py: separar entrenamiento de exploración/evaluación.
- pymol_ribbon.py: dividir coloración, presets, memoria de escena y toolbar.
- workflow_panel.py: separar archivo del grafo y panel, sin modificar el orquestador.

Fase 7 — Verificación final
Para cada PR:

- Tests unitarios del componente extraído.
- Tests Qt offscreen.
- Smoke test de apertura/cierre del proyecto.
- Pruebas de docking y scopes existentes.
- Verificación de que ningún widget toca Qt desde workers.
- Verificación de que jobs químicos siguen pasando exclusivamente por las APIs AMDock/MolSuite.
- Medición final de LOC, métodos por clase y dependencias UI→dominio.
Orden recomendado de implementación: recursos → navegación legacy → main_window → frontera API → lógica de docking → UI de docking → workspace/importadores → módulos secundarios.
