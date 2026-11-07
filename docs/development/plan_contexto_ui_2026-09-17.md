# Contexto (tool, tab) → visualización / zona auxiliar

Inventario de las relaciones **que existen hoy en el código** y de las **que faltan o están
mal conectadas**, para revisarlas antes de declarar el contexto global.

## 0. Las piezas

| Zona | Quién la gobierna hoy | Ocupantes |
|---|---|---|
| Panel izquierdo (tool) | `shell/tool_coordinator.py` (`active_tool`, 1 a la vez, dock EXCLUSIVE) | Filter, Diversity, Build, Pocket Detection, Docking Studio |
| Centro (tabs) | `shell/main_content.py` (`current_view_id()`, 1 slot de *preview*) | 7 tablas de catálogo + Docking Results + QSAR Models + Predictions + Workflow + Import |
| Derecha arriba (EXCLUSIVE) | `visualization/chart_controller.py` + `viewers/molecular_viewer.py` | PyMOL · Distribution (charts) · Glowing molecule |
| Derecha abajo (EXCLUSIVE) | `catalog/results.py` → `diagram_dock` | 2D Interactions |
| Panel lateral de PyMOL | `viewers/molecular_viewer.py` + `shell/auxiliary_panel.py` | Grid Box |
| Zona auxiliar (bajo la tabla) | `shell/auxiliary_panel.py` (1 ocupante + 1 toggle global) | Details · panel de la tab · auxiliar de la tool |

Único punto donde hoy se cruzan tool y tab: `main_window._on_current_view_changed()`
(línea 440) y `tool_coordinator.open_tool()` → `aux.set_occupant()`.

## 1. Tool → Tab (quién ancla a quién) — HOY

| Tool | Tab que enfoca | Cómo | Nota |
|---|---|---|---|
| Filter | Molecules | `showEvent` → `open_or_focus_view` | además botón → Ligands |
| Build | Molecules **o** Shards | `showEvent` + al cambiar de sub-pestaña | ancla dinámica (`_ligand_view_id`) |
| Pocket Detection | Receptors | `showEvent` | |
| Docking Studio | Ligands/Shards (paso 1), Receptors (paso 2), Complexes (paso 3) | cambio de etapa del stepper | ancla que **se mueve** |
| Docking (al lanzar Run) | Docking Results + cierra el panel de tools | `run_panel.py:1052` | |
| **Diversity** | **ninguna** | — | ⚠️ falta; es la que motivó todo esto |

Ninguna tool declara su ancla: cada una la ejecuta desde su `showEvent`. No hay
`tool_anchor_view` legible desde el shell, y **ninguna deshace nada al cerrarse**.

## 2. Tab → Visualización — HOY

| Tab | PyMOL | Chart | 2D Interactions |
|---|---|---|---|
| Molecules / Receptors / Ligands / Shards / Binding Sites / Complexes | `show_active_in_pymol()` → carga fila 0 | — | — |
| Activity | `cmd.delete("all")` | `refresh()` → histograma **y fuerza el dock visible** | — |
| Docking Results | fila 0 (pose) | — | `show_hit()` al seleccionar |
| QSAR Models / Predictions | `cmd.delete("all")` | el chart se queda como estaba (rama `return`) | — |
| Workflow / Import | `cmd.delete("all")` | oculta chart+glow y **enciende PyMOL** | — |
| cualquier otra | idem | `toggle("pymol", True)` incondicional | — |

`chart_controller.sync_to_active_view` es quien decide, y su rama por defecto contiene el
`self.w.dock_manager.toggle("pymol", True)` que reabre PyMOL aunque el usuario lo haya cerrado.

## 3. Tab → Zona auxiliar — HOY

Prioridad en `auxiliary_panel.set_occupant()`: **panel de la tab** > **auxiliar de la tool** > **Details**.

| Contexto | Ocupante |
|---|---|
| Docking Results (pivot Hits) | "Selected Result" (lo presta la tab) |
| Docking Results (otros pivots) | cae al de la tool / Details |
| \* con Docking Studio abierto | Prep Status (rol ligando o receptor según la etapa) |
| \* con Pocket Detection abierto | Pocket Sites |
| cualquier tabla, sin tool | Details (sigue la selección; sólo consulta la DB si está visible) |

Además, la **selección de fila** (no la tab) decide Grid Box:
`show_catalog_selection_details()` → Details + `grid_dock.set_molecule()` +
`show/hide_grid_panel()` según `is_receptor` y `grid_preview_enabled`.

## 4. Tool → Visualización — HOY (por empuje, fuera del contexto)

| Tool / vista | Qué empuja | Dónde |
|---|---|---|
| Diversity | universo químico, distribución de tamaños, hover de centroides | `show_diversity_universe`, `show_size_distribution` |
| QSAR panels | ROC, importancias, heatmap, split, dominio de aplicabilidad, glowing molecule | `tools/qsar/panels.py` |
| Activity view | histograma | `catalog/domain_views.py` |
| Docking Studio | receptor + caja en PyMOL al marcar receptor | `focus_receptor_in_pymol` |
| Pocket Detection | sitio en PyMOL | `detection.py:282` |

Todas llaman `_show()` → `toggle(dock, True)`: **encender es siempre una orden, nunca una propuesta**.

---

## 5. Lo que falta o está roto (a revisar antes de declarar el contexto)

1. **Diversity no tiene ancla.** No enfoca Molecules; su chart aparece sin que la tab
   correspondiente esté delante, y al cambiar de tab `sync_to_active_view` hace
   `show_universe([], [], ...)` — **borra el universo aunque la tool siga abierta**.
2. **Rama muerta:** `chart_controller` busca `SELECTION_VIEW_ID` como tab central; desde que
   Diversity vive en el panel izquierdo esa condición no se cumple nunca (`restore_plot` inalcanzable).
3. **`toggle("pymol", True)` incondicional** pisa la decisión explícita del usuario. Mismo
   problema con el histograma de Activity, que se re-enciende en cada `refresh()`.
4. **No hay memoria por contexto.** Sólo `aux` guarda medio estado (`_user_checked`); PyMOL,
   Distribution, Glow, Grid Box y 2D no guardan nada. Falta `visible_panel_by_context`.
5. **Enfoque vía `showEvent`**: se dispara también cuando el dock se re-muestra, y no hay
   simétrico en `hideEvent`. Cerrar una tool no devuelve el contexto anterior (sólo el aux lo hace).
6. **No existe la relación (tool, tab) → visualización.** Hoy sólo hay tab→visual y
   empujones de la tool. Es exactamente lo que hay que declarar: p.ej.
   `(Diversity, Molecules) → Distribution`, `(Docking, Receptors) → PyMOL + Grid Box`.
7. **Grid Box depende de la selección, no del contexto.** Con Docking Studio en la etapa de
   receptor debería estar disponible siempre, no sólo cuando la fila seleccionada es receptor.
8. **2D Interactions nunca se apaga.** Sólo Docking Results lo llena; al irse de esa tab queda
   mostrando un hit viejo.
9. **QSAR y Activity no son tools** (viven como tabs "standing"), pero se comportan como tales
   (empujan charts). O son tools, o el contexto tiene que aceptar "tab protagonista sin tool".
10. **Import no participa**: es una tab que ignora tool, aux y visualización.
11. **Ancla móvil de Docking Studio**: cambia de tab por etapa. El contexto tiene que aceptar
    que la tool reescriba su `tool_anchor_view` (ya lo hace, pero nadie lo registra).

## 6. Tabla que habría que declarar (propuesta a validar)

Por **tool**: `anchor_view` · `aux_page` · `visual` sugerida.

| Tool | anchor_view | aux_page | visual sugerida |
|---|---|---|---|
| Filter | Molecules | Details | PyMOL |
| Build | Molecules / Shards | Details | PyMOL |
| Diversity | Molecules | Details (o resumen de clusters) | Distribution |
| Pocket Detection | Receptors | Pocket Sites | PyMOL + Grid Box |
| Docking Studio | Ligands→Receptors→Complexes | Prep Status / Selected Result | PyMOL + Grid Box; 2D en resultados |

Por **tab**: `pymol_hook` · `aux_panel()` · chart propio (ya existe, sin declarar en un solo sitio).

Regla a fijar: **el contexto propone, el usuario dispone.** Un cambio de contexto puede
cambiar el *contenido* de un panel; sólo puede cambiar su *visibilidad* si el usuario no se
ha pronunciado en ese contexto.
