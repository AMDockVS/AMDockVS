# Tareas AMDockVS

Lista de trabajo derivada de [`revision_2026-08-14.md`](revision_2026-08-14.md). La revisión
explica el **porqué** con evidencia `archivo:línea`; esta lista es el **qué**, en orden, para ir
marcando. Cada tarea cita la sección de la revisión donde está el razonamiento.

Estado: `[ ]` pendiente · `[x]` hecho (con fecha) · `[~]` en curso · `[-]` descartado o fuera de
alcance.

---

## A. Zona auxiliar y contrato de las tools — §0

Es por donde se empieza: el resto de la UI depende de que exista un contrato de entrada/salida.

- [x] **A0. Refrescar solo lo visible** *(2026-08-15)* — `refresh_open_view` salta lo que no está
  en pantalla y anota el `view_id`; `_on_current_tab_changed` recarga solo lo anotado. Mismo
  criterio en la auxiliar: Details no sigue la selección mientras está oculto. §0.2 nota 1
- [x] **A1. Splitter + zona auxiliar** *(2026-08-15)* — `QSplitter(Vertical)` con el `QTabWidget`
  arriba y un `QStackedWidget` abajo, oculto cuando no hay ocupante (Qt esconde el handle con él).
  Toggle global "Panel" a la derecha de la barra superior. §0.2
- [x] **A2. Details a la auxiliar** *(2026-08-15)* — deja de ser pestaña; una sola instancia que
  sigue la selección de cualquier tabla; su botón sale del rail izquierdo. §0.1
- [x] **A3. Prepared a la auxiliar** *(2026-08-15)* — *Prep Status* deja de ser pestaña
  contextual; Docking Studio ocupa el hueco mientras está abierto y lo devuelve a Details al
  cerrarse (`_TOOL_AUX_VIEWS`). §0.2
- [x] **A4. `set_toolbar_actions` en `ms_table`** *(2026-08-15)* — API con clave, igual que
  `set_external_filters`: `set_toolbar_actions(key, actions)` inserta al final de la zona
  izquierda, re-empujar la misma clave reemplaza, lista vacía = `clear_toolbar_actions(key)`.
  Respeta el estado colapsado y construye la barra si la config no pedía ninguna.
  Forwarder en `BoundTableWidget` (`catalog/common.py`). Self-check en
  `molsuite_components/ms_table/test_toolbar.py`. **Cambio en `ms_components`.** §0.4
- [x] **A5. `push_scope` / `pop_scope`** *(2026-08-15)* — en `BoundTableWidget`: una sola llamada
  con `filters` + `clause` + `actions` (A4) + estado vacío, bajo una clave; `pop_scope(key)`
  devuelve las cuatro y **restaura los `default_filters` del catálogo**, no "sin filtro" (antes
  docking tenía que recordar a mano que Ligands filtra `usage_class=general`). Docking migrado:
  `_SCOPE_KEY = "docking"`, `_release_*` bajó a una línea. Test `test/test_table_scope.py`. §0.4
- [x] **A6. Pocket Detection al mismo mecanismo** *(2026-08-16)* — desaparecen `PocketReceptorWidget`
  y el `QSplitter` con la tabla de predicciones. Al abrir la tool: foco en Receptors +
  `push_scope("pockets", …)` (solo receptores, menos los que ya traen ligando de referencia);
  las predicciones son `BindingSitesWidget`, vista propia en la zona auxiliar que **sigue al
  receptor seleccionado** leyendo de `pockets.list_predictions`. Profile pasa a ser global de la
  corrida. Accesor nuevo `window.aux_view(view_id)`: construye la página aunque esté oculta, y es
  **la página** la que aplaza la carga hasta mostrarse (A0), en vez de que la tool tenga que mirar
  si el panel está abierto. Abrir una tool con auxiliar **enciende el panel** y al cerrarla se
  devuelve el estado que encontró: su salida no vive detrás de un toggle que hay que descubrir.
  §0.1, §0.4
- [x] **A7. Pivotes de Docking Results** *(2026-08-16)* — off-target y redocking dejan de ser vistas
  y pasan a ser pivotes de la misma tabla (`ui/tools/docking/results_pivot.py`), con **condicional
  de poblado** (no de corrida) y **deshabilitado con el motivo escrito, nunca oculto**; el predicado
  es una sola consulta (`queries.pivot_availability`) y se recalcula en `job_finished`. Cada pivote
  se construye la primera vez que se elige. Test `test/test_results_pivot.py`. §0.3
- [x] **A8. Filter y Build entrando por Molecules** *(2026-08-16)* — por corrección, no por estética:
  entrando por Ligands/Receptors se escapan moléculas cuyo tipo no cae en el rol. Las dos tools
  toman scope **por tipo molecular** y lo pintan sobre Molecules (`push_scope`/`pop_scope` en
  `showEvent`/`hideEvent`, mismas condiciones que ejecuta el run vía `filtering.sql`). En Build,
  un solo combo de tipo + un combo de Set sustituyen a los dos combos por rol, y los grupos de ops
  que ese tipo no admite quedan **deshabilitados con el motivo en el título**. La raíz estaba en
  `chemistry/api.py`: un scope con `molecule_type` ya no se pisa con `is_ligand`/`is_receptor`. §0.4
- [x] **A9. Regla de vida de las pestañas efímeras** *(2026-08-16)* — patrón *preview* en
  `MainContentWidget`: `register_view(..., preview=True)` marca la vista como efímera (QSAR Models,
  Binding Sites, Complexes) y todas comparten **una sola ranura** — abrir una cierra la anterior.
  Título en cursiva vía `QProxyStyle` sobre el `QTabBar` (Qt no tiene fuente por pestaña).
  Test `test/test_preview_tabs.py`. §0.5

**Decisión abierta:** si una auxiliar sigue **a la selección** (binding sites, poses) o **al
scope** (prepared). Con A6 ya hay una de cada tipo: Details y Binding Sites siguen la selección,
Prep Status sigue a la tool. Sigue sin hacer falta indexar por pestaña porque las tres entran por
`_TOOL_AUX_VIEWS`; se decide cuando Binding Sites deba verse **sin** Pocket Detection abierto
(solo seleccionando un receptor). §0.2

---

## B. Bugs

- [x] **B1. `DockingStudioWidget` reventaba al cerrarse sin proyecto** *(2026-08-15)* — el
  placeholder no construye `flex_box`/`flex_candidates` pero `hideEvent` los tocaba. Flag `_ready`
  + guarda en `show/hideEvent`, y el chrome del tool se retira antes de destruirlo.
- [x] **B1b. LLE/BEI/SEI salían vacías en todos los resultados** *(2026-08-16)* — el worker
  recalculaba los descriptores parseando el fichero del ligando, pero `ligand_source_path` es
  relativa y en el worker no resuelve → `clogp/mw/tpsa` a `None`. Ahora los descriptores del
  import viajan con el par (`_ligand_descriptors_from_row` en `docking/service.py`, campos extra
  en `docking/jobs.py`) y `docking_metrics(descriptors=...)` los prefiere; el parseo queda de
  respaldo. Solo aplica a corridas nuevas: no hay backfill de las métricas ya guardadas.
- [x] **B1c. Docking Results cargaba TODA la tabla de resultados al abrir** *(2026-08-16)* —
  `filtered_hits(limit=5000)` traía cada pose de cada ligando y agrupaba en Python (además
  truncaba en silencio: con ~9 poses/ligando el tope eran ~550 ligandos). Ahora
  `list_ligand_result_summaries` agrupa en SQL (`MIN(r.score)` + el truco de columnas desnudas de
  SQLite ⇒ la fila del mejor pose) y devuelve páginas de 200; las poses se piden por ligando al
  seleccionarlo (`filtered_hits(ligand_id=...)`). Scroll al fondo = página siguiente. En p4:
  0.076 s vs 1.64 s, y ahora es O(página), no O(resultados). Check en
  `test_amdock_runtime.py` (paginación + best-pose + pose_count).
- [x] **B1d. No se veía cuántos dockings se iban a hacer ni cuántos faltaban** *(2026-08-16)* — el
  Docking Studio solo mostraba ligandos y receptores listos (nunca el producto), y Results mostraba
  "intentados/completados" por receptor sin referencia contra la que comparar: en p4, 4UWH tenía
  158 ligandos y los otros 160 y nada lo delataba. Ahora: (a) fila **Dockings** en Run Scope,
  `ligandos x receptores x programas`, con `N to run of M (K already docked)` usando
  `count_docked_pairs` (un `COUNT(DISTINCT receptor, ligand)` en SQL, no el conjunto de pares, que
  solo materializa el guard de skip-existing); (b) columna **Missing** en la tabla By Receptor =
  `expected_ligands - docked`, donde `expected_ligands` es la unión de ligandos dockeados contra
  *cualquier* receptor (subquery escalar en `list_receptor_result_summaries`). Verificado en p4:
  4UWH → Missing 2, resto 0; preview → `2720 to run of 3198 (478 already docked)`. Check en
  `test_amdock_runtime.py`.
- [x] **B1e. El hash del protocolo incluía parámetros que no son protocolo** *(2026-08-17)* —
  `protocol_hash` hasheaba el `config` completo, y el `config` de la UI lleva también los mandos de
  *ejecución*: `num_modes` (cuántas poses se reportan), `vina_backend` (binario vs API) y
  `vina_cpu` (hilos). Ninguno mueve una pose, pero cambiarlos daba un protocolo nuevo, con dos
  consecuencias: el filtro *Protocol* de Results se llenaba de variantes falsas
  (`... | modes=9 | backend=binary`) y el guard de skip-existing —`existing_result_pairs` filtra
  por `metrics.protocol.hash`— dejaba de reconocer lo ya dockeado y volvía a docar todo. Ahora
  `protocols.py` declara `IDENTITY_KEYS = ("scoring_function", "exhaustiveness")` +
  `protocol_identity(config)`, y **hash y label** se construyen solo con ese subconjunto; el
  `config` completo se sigue guardando (es el que lleva los parámetros al job y sirve de auditoría).
  Label nuevo: `AutoDock Vina | sf=vina | exh=8`. Check en `test_amdock_runtime.py`.
  Ojo: los resultados viejos conservan su hash/label largos ⇒ en p4 conviven el protocolo antiguo
  y el nuevo aunque la ciencia sea la misma (no hay backfill; datos de desarrollo).
- [ ] **B2. PDBQT: se fuerza el sufijo equivocado** — receptor `.pdbqt` se guarda como `.pdb`
  (`materializers.py:1010-1016`) y ligando `.pdbqt` como `.sdf` sin datos RDKit. §6
- [ ] **B3. Rama inalcanzable** `elif suffix == ".pdbqt": return Chem.Mol()`
  (`vina_preparation.py:36`): devolvería una molécula vacía en silencio. §14.7
- [ ] **B4. `RECEPTOR_VIEW_ID` duplicado con valores distintos** — `domain_views.py:5` dice
  `workspace.receptor`, `receptors.py:59` dice `workspace.receptors`. §1, §14.5

### Regla de escala (auditoría §15, 2026-08-16)

**Ningún dato que crezca con el screening se lee entero.** Query sobre `molecules`,
`docking_results` o `plip_results` ⇒ paginada (`LIMIT`/`OFFSET`) o agregada en SQL; tabla de UI
que la consuma ⇒ pagina al hacer scroll. Un tope fijo **siempre** se le dice al usuario
("showing X of Y", como hace off-target); truncar en silencio es tan bug como quedarse sin RAM.
Auditado ya: catálogo (ms_table pagina, OK), off-target (topes con aviso, OK), grid box (OK),
resultados de docking / `list_ligands` / `list_receptors` / `list_results` / picker de QSAR
(arreglados, ver B1c y abajo). Lo pendiente son B5–B7.

- [x] **B4b. Fugas menores de "traer todo"** *(2026-08-16)* — `queries.list_ligands` volcaba la
  tabla `molecules` entera **y la imprimía a stdout** (`print(f"{rows = }")`); `list_ligands`,
  `list_receptors` y `list_results` no tenían cota; el picker de ligandos de QSAR
  (`qsar/activities.pick_ligands`) construía un diálogo con todos los ligandos del proyecto.
  Print fuera, `limit`/`offset` en las tres queries, tope de 500 + aviso en el picker. §15
- [ ] **B5. `docking._count_failed_preparations` materializa todos los ids del scope**
  (`ui/tools/docking/docking.py:1327`) y hace queries `IN (...)` de 900 en 900 — se dispara en
  cada cambio de scope del Docking Studio. Debe ser un `COUNT` con join sobre las condiciones del
  scope (`filtering.sql.scope_conditions`). §15
- [ ] **B6. `docking._resolve_receptor_ids(mode="active")` manda la lista completa de ids**
  a los parámetros del job; con miles de receptores el `params` se vuelve enorme. Debería viajar
  el scope, no los ids. §15
- [ ] **B7. `redocking._fetch_results` usa `filtered_hits(limit=5000)`** y agrega por protocolo en
  Python: trunca sin avisar. Prioridad baja (el redocking va sobre complejos de referencia), pero
  la agregación pertenece a SQL. §15
- [ ] **B8. `ui/lazy_table.py` (`LazyFetchTableModel`) no lo usa nadie** — está escrito justo para
  las tablas de resultados (`canFetchMore`/`fetchMore`, `_Pager` con su check). O se adopta en la
  tabla de ligandos (implica pasar de `QTableWidget` a `QTableView` + modelo, y portar
  `_MetricColumns`) o se borra. §15
- [ ] **B9. "¿esto ya está hecho?" — guard de re-ejecución transversal, y avisar al usuario**
  *(revisado 2026-08-17, sin tocar nada: es un patrón para aplicarlo de una vez a todos los jobs)*.
  Estado real hoy, job por job:
  - **docking.run / redock** — sí hay guard, pero *silencioso* y a nivel de par:
    `skip_existing=True` ⇒ `existing_result_pairs(engine, run_kind, protocol_hash)`; con
    `skip_existing=False` se borran los resultados previos de esos receptores en vez de duplicar.
    Nada avisa: si los 3198 pares ya están, el job se envía igual, emite 0 chunks útiles y termina
    "bien". Falta el "0 pares nuevos ⇒ no envío / confirma re-dock".
  - **redocking** (`ui/tools/docking/redocking.py::_submit`) — **no manda `protocol_metadata`** ⇒
    hash `""` ⇒ el skip casa con *cualquier* redock previo de ese par: cambiar `exhaustiveness` y
    volver a lanzar no vuelve a docar nada, en silencio. Es el fallo inverso al de B1e.
  - **prepare_ligands / prepare_receptors** — sí: `_pending_rows` deja fuera lo que ya tiene
    `prepared_engine_path` existente en disco; `force=True` lo salta. Tampoco avisa ("0 pendientes").
  - **chemistry / descriptores / fingerprints** — `write_mode="upsert"` ⇒ re-ejecutar no duplica
    filas, pero recalcula todo; solo los fingerprints tienen gate de "missing".
  - **pockets** — re-ejecutar *reemplaza* los sitios del receptor (`replaced_receptors`).
  - **import** — sin guard: el mismo fichero dos veces entra dos veces.
  - **job en vuelo, solo docking** — el Docking Studio sí lo comprueba antes de enviar
    (`_docking_signature` + `_check_docking_conflict`): job idéntico vivo ⇒ error y no envía; otro
    docking vivo ⇒ pregunta. Dos límites: (i) las firmas viven en `self._docking_job_sigs`, un dict
    en memoria del widget, y desde que el studio se cierra al lanzar (C0b) se pierde ⇒ al reabrir
    solo queda el aviso genérico; debería derivarse de los params del job en la BD del executor.
    (ii) la firma hashea el `config` completo, así que sigue distinguiendo `num_modes`/backend —
    debería usar `protocol_identity` como el hash (B1e).
  - **nivel job** (`runtime.submit_job`) — no hay deduplicación genérica: solo encadena
    dependencias (`resolve_job_dependencies`). Fuera de docking, dos submits idénticos se encolan y
    corren los dos; el skip se materializa al arrancar cada job, así que solapados se pisan.
  Lo que falta, en una pieza reutilizable: (a) un "¿qué queda por hacer?" por job (el par/entidad
  pendiente ya se sabe calcular en docking y prepare) evaluado **antes de enviar**; (b) aviso al
  usuario con el reparto *nuevo / ya hecho* y la opción de forzar; (c) chequeo de que no haya ya un
  job activo del mismo tipo y ámbito (`active_jobs` + firma de params ~ el hash del protocolo).
  Ligado a [[mf-domain-idempotency-contract]] y a `docking-skip-existing`.

---

## C. Deuda de UI

- [x] **C0. Columnas de métricas seleccionables en las tablas de resultados** *(2026-08-16)* —
  `_MetricColumns` (`ui/workspace.py`): cada columna es `(label, getter, visible_por_defecto)` y
  el menú contextual del header la enciende. Ligands: Ligand/Best/LE/LLE/Poses visibles,
  pKi/Ki/FQ/BEI/SEI opcionales. Poses: Pose/Score/RMSD ref visibles, LE/pKi/Ki opcionales (dentro
  de un mismo ligando reordenan igual que Score). La columna 0 nunca se oculta. Sin persistencia
  de la selección todavía. Test `test/test_metric_columns.py`.
- [x] **C0b. El paso 4 (Preview & Run) abre Docking Results** *(2026-08-16)* — el mapa
  paso→vista de `_on_step_changed` solo cubría los pasos 2 y 3 (Ligands/Receptors), así que al
  llegar a Preview & Run la zona central se quedaba en Receptors. Ahora `3: COMPLEXES_VIEW_ID`:
  el paso previo al run muestra dónde va a aterrizar. Y al lanzar (`_on_docking_submitted`,
  solo en el camino de éxito) el studio se retira: abre Docking Results y cierra el dock de
  tools — ya no queda nada que configurar y la pantalla es para los resultados.
- [x] **C0c. "Selected Result" pasa a la zona auxiliar** *(2026-08-16)* — era un panel dentro de
  la pestaña, debajo de las tablas, justo lo que la zona auxiliar existe para mostrar. Ahora una
  vista central puede reclamar el slot exponiendo `aux_panel()`: `_set_aux_occupant` calcula
  *vista en pantalla → tool activo → Details* y se dispara también en `current_view_changed`. El
  panel lo sigue construyendo `DockingResultsWidget` (está cableado a sus tablas) y solo se
  reparenta al stack auxiliar; al salir de la pestaña, cambiar de pivot (off-target/redocking no
  tienen panel) o cerrarla, el slot vuelve al auxiliar del tool. §0
- [x] **C6. Sacar PLIP del panel "Selected Result" — una sola fuente: el JSON de ms_contactmap**
  *(decidido y hecho 2026-08-16/17)* — fuera los dos split-buttons de PLIP (`Compute selected pose`
  / `Compute filtered active poses` + sus "Render 2D diagram"). Además de estar fuera de sitio,
  desde que el panel vive en la zona auxiliar pasaban `busy=self` = overlay "Loading…" **sobre la
  pestaña de resultados**, no sobre el panel del botón: se apagaban las tablas y se comían los
  clics mientras el botón pulsado no daba señal ninguna — se lee como app congelada. `pldiagram`
  pasa a ser `ms_contactmap`, módulo independiente de MolSuite: computa las interacciones **y** el
  diagrama en una pasada y las deja en JSON junto a la pose. Reparto final:
  - **Selected Result (auxiliar)** = dueño de la pose ⇒ dueño de la acción. Lista las interacciones
    leyendo ese JSON (`pose_interactions`, `json.load` pelado: ni PLIP en BD ni import de
    pldiagram) y tiene el botón **Build diagram**, que corre detección + `solve_layout` en worker,
    guarda junto a la pose, recarga la lista y le pide `reload()` al dock.
    Más adelante, clic en una interacción ⇒ resaltarla en PyMOL: mismo JSON, sin segunda pasada.
  - **Dock 2D** = render-only, ajeno a poses/receptores/PLIP: dibuja el documento guardado y ya.
    Es lo que necesita `ms_contactmap` para ser reutilizable; la app standalone pondrá su propio
    botón. Un botón en el componente actuaría sobre una selección que el componente no conoce.
  - **Lote** ("Compute filtered active poses") no es detalle de una pose: se va a la barra de
    Docking Results, junto al resto de acciones sobre el conjunto filtrado. **Pendiente.**
- [x] **C7. Filtros de Docking Results: chips aditivos en vez de checkbox+Apply** *(2026-08-17)* —
  la barra vieja pedía dos clics para cualquier filtro (marcar la casilla y pulsar `Apply`), solo
  admitía **un** valor de score y **una** métrica a la vez, y tras editar un campo no había forma
  de saber qué estaba aplicado de verdad. Ahora `ui/filter_chips.py::MetricFilterBar` (local a
  `molsuite_components.ms_table.MetricFilterBar` (el `FilterPanel` del mismo paquete es otra cosa:
  filtra un `TableConfig` por igualdad/ILIKE, no expresa rangos, y está huérfano): en la barra
  solo hay estado — los chips activos y un `+` que abre un diálogo (campo, operador ≥/≤, valor);
  al aceptar aparece el chip mientras las tablas
  recargan solas — lo aplicado es lo que se lee en pantalla. Los chips se llavean por
  `(campo, operador)`, así que `Score ≥ -9.5` y `Score ≤ -9` conviven = **rango**, y varias
  métricas se acumulan (AND). El componente no sabe de SQL ni de sesiones — `conditions()` devuelve
  `[(campo, op, valor)]` y quien lo use decide; eso es lo que lo hace reutilizable (también expone
  `add_condition` / `set_conditions` para restaurar una vista guardada). En AMDock son condiciones
  aditivas en SQL: `_hit_filter_clauses(metric_filters=...)` en `queries.py`, expuesto por
  `docking/api.py` en `ligand_summaries` y `filtered_hits`.
  `Completed only` pasa a **`Hide failed poses`** (es `r.score IS NULL`: la fila del par que falló
  se escribe igual para que el fallo se vea) y aplica al instante; `Apply` desaparece y `Clear`
  limpia chips + casilla. Protocolo sigue siendo combo aparte (aísla programa, no filtra número).
  Verificado en p4: 160 ligandos → 7 con `Score ≤ -9` → mismo 7 al añadir `≥ -9.5`, `Clear` → 160.
- [ ] **C1. `saveState`/`restoreState` del layout** en `closeEvent`/`showEvent`, con clave de
  versión, y **dejar de reimponer el tercio** en cada `open_tool` (hoy pisa el tamaño que el
  usuario arrastró). §8
- [ ] **C2. Details por tipo molecular** — hoy es un visor de diccionarios, no un panel de
  dominio. §9
- [ ] **C3. `Details.show_molecule()` fuera del hilo GUI** — 4 queries bloqueantes; `run_async` ya
  es el patrón del proyecto. §3, §14.3
- [ ] **C4. Respetar el `behavior` declarado en `_infer_and_update_region`** — al arrastrar un dock
  a otra región se le reasigna comportamiento. §11
- [ ] **C5. Exponer `protonate_*` / `standardize_ligands` en Build** e Import en la toolbar: son
  jobs sin acción propia en la UI. §12

---

## D. Limpieza

- [ ] **D1. Borrar `../src/amdockvs/ui.bak/`** — 40.337 líneas, 21 MB, **no ignorado por git**. Antes
  de commitear. §14.1
- [ ] **D2. Borrar `ui/tools/molecules.py` y `ui/tools/mols.py`** — vacíos, y el primero sombrea el
  paquete `molecules/`. §14.4
- [ ] **D3. Subir los helpers de UI de `docking.py` a `docking/api.py`** — el panel tiene 2.825
  líneas. §3
- [ ] **D4. Cerrar Fase 5 o quitar `_NullDock`** — se traga llamadas muertas y oculta errores de
  registro. §14.2
- [ ] **D5. `amdock_rc.py` (21.703 líneas generadas) fuera del repo**, a paso de build. §14.6

---

## E. Tests rotos (preexistentes, ninguno causado por el trabajo de §0)

- [ ] **E1. `test_docking_ligand_picker.py:3`** importa `_docking_ligand_table_config`, que ya no
  existe.
- [ ] **E2. `test_pymol_ribbon.py`** importa `install_pymol_ribbon`, que ya no existe.
- [ ] **E3. `test_2d_preview_settings.py:177`** afirma `menu.settings_button`, atributo que no
  existe en ningún sitio de `src/`.

Suite al 2026-08-15, excluyendo E1 y E2: **159 pasan, 1 falla (E3)**.

---

## F. Fuera de alcance ahora (decidido, no olvidado)

- [-] **F1. `gpu_required` / elegir executor por job** — se trabaja en `ms_flow` primero; no
  tocar AMDock hasta que MF exponga la declaración. Decidido 2026-08-14. §13
- [-] **F2. Workflow** — fuera de esta ronda hasta que todo lo que usa esté funcional. §0.1
- [-] **F3. Unificar descriptores / fingerprints / SMILES** — plomería de datos, no layout, pero es
  lo que dolerá al construir el `feature_source` de QSAR. §0.6
- [-] **F4. Tool de diseño de derivados a partir del Glowing.** §0.6
- [-] **F5. Fusionar Ligands y Receptors** en una tabla con selector de rol — **descartado**:
  docking necesita las dos a la vez. §0.5
- [-] **F6. Versionado por tags (setuptools-scm)** — versioneer estaba muerto en el repo (sin
  `setup.py`, sin `[tool.versioneer]`, sin `_version.py`, sin tags); se borró junto con el
  `.gitattributes` que apuntaba a `src/amdockvs/_version.py` (ruta inexistente). Hoy la versión
  es el literal `version = "0.1.0"` de `pyproject.toml`. Cuando haya primer release etiquetado,
  sustituirlo por: `requires = ["setuptools>=68", "setuptools-scm>=8"]`, `dynamic = ["version"]`
  y `[tool.setuptools_scm] version_file = "src/amdock/_version.py"`; entonces `git tag -a v0.2.0`
  fija la versión sola. Ojo: con `pip install -e .` la versión queda congelada hasta reinstalar.
  Decidido 2026-08-18: esperar, sin tags no ahorra nada.
