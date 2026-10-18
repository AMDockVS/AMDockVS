from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

from amdockvs.core.configuration import (
    DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS,
    MAX_2D_PREVIEW_HEAVY_ATOMS_PATH,
)
from amdockvs.core.payloads import molecule_payload_id, payload_store
from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.common.job_follower import JobFollower
from ms_components.ms_table import FilterOperator, FilterSpec, TableConfig, SmartTableView, ToolbarAction
from ms_components.tool_panel import ActionBar

# How long a finished run's outcome stays under the table before the strip hides.
_JOB_BAR_LINGER_MS = 4000


_PREVIEW_WIDTH = 256
_PREVIEW_HEIGHT = 192
PREVIEW_2D_COLUMN_FIELD = "__preview_2d__"


def _row_raw_object(row_data: dict) -> Any | None:
    return row_data.get("__raw__")


def supports_2d_depiction(record_or_row: Any) -> bool:
    raw = _row_raw_object(record_or_row) if isinstance(record_or_row, dict) else record_or_row
    return str(getattr(raw, "molecule_type", "") or "") == "small_molecule"


def detect_preview_theme() -> str:
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return "light"

    palette = app.palette()
    window_color = palette.color(QPalette.ColorRole.Window)
    brightness = (window_color.red() * 299 + window_color.green() * 587 + window_color.blue() * 114) / 1000
    return "dark" if brightness < 128 else "light"


def _with_preview_frame(svg: str, *, width: int, height: int) -> str:
    if not svg:
        return ""
    rect = (
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
        f'rx="8" ry="8" fill="transparent"/>'
    )
    svg_tag_start = svg.find("<svg")
    if svg_tag_start < 0:
        return svg
    svg_tag_end = svg.find(">", svg_tag_start)
    if svg_tag_end < 0:
        return svg
    return svg[: svg_tag_end + 1] + rect + svg[svg_tag_end + 1 :]


def _molecule_svg_for_path(
    path_text: str,
    width: int = _PREVIEW_WIDTH,
    height: int = _PREVIEW_HEIGHT,
    theme: str = "light",
    max_heavy_atoms: int = DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS,
) -> str:
    path = Path(str(path_text or "")).expanduser().resolve()
    if not path.exists():
        return ""
    try:
        from rdkit import Chem
        from rdkit.Chem.Draw import rdMolDraw2D
    except Exception:
        return ""

    mol = None
    suffix = path.suffix.lower()
    try:
        from amdockvs.io.formats import read_mol

        mol = read_mol(path)

    except Exception:
        mol = None

    if mol is None:
        return ""

    try:
        if mol.GetNumHeavyAtoms() > max_heavy_atoms:
            return ""
        mol = Chem.RemoveHs(mol)
    except Exception:
        return ""

    try:
        from rdkit.Chem import AllChem

        AllChem.Compute2DCoords(mol)
    except Exception:
        pass

    try:
        rdMolDraw2D.PrepareMolForDrawing(mol)
        drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
        options = drawer.drawOptions()
        options.clearBackground = False
        if theme == "dark" or (theme == "auto" and detect_preview_theme() == "dark"):
            try:
                rdMolDraw2D.SetDarkMode(drawer)
            except (AttributeError, TypeError):
                pass
        drawer.DrawMolecule(mol)
        drawer.FinishDrawing()
        return _with_preview_frame(drawer.GetDrawingText(), width=width, height=height)
    except Exception:
        return ""


def _molecule_svg_for_payload(
    data: bytes,
    kind: str,
    width: int = _PREVIEW_WIDTH,
    height: int = _PREVIEW_HEIGHT,
    theme: str = "light",
    max_heavy_atoms: int = DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS,
) -> str:
    """The depiction of one stored structure. The reader takes paths (`io.formats.read_mol` is the
    one reader of the app), so the bytes pass through a scratch file that lives only here."""
    from ms_flow.payloads import scratch_file

    with scratch_file(data, f".{kind or 'sdf'}") as path:
        return _molecule_svg_for_path(str(path), width=width, height=height, theme=theme,
                                      max_heavy_atoms=max_heavy_atoms)


class _PreviewReady(QObject):
    ready = Signal()


# Emitted when a depiction lands in the cache: tables showing the 2D column repaint.
PREVIEW_READY = _PreviewReady()
_PREVIEW_CACHE_SIZE = 1024


class MoleculePreviews:
    """2D depictions for a table's preview column. Painting must not wait, so a cell with no
    depiction yet paints empty while the bytes are read and drawn off the GUI thread; the cell
    repaints when `PREVIEW_READY` fires. Payload ids are never reused: a cached depiction is never stale."""

    def __init__(self, runtime) -> None:
        self._runtime = runtime
        self._svgs: OrderedDict[tuple, str] = OrderedDict()
        self._pending: set[tuple] = set()

    def paint(self, row_data: dict) -> str:
        if not supports_2d_depiction(row_data):
            return ""
        payload_id = molecule_payload_id(_row_raw_object(row_data) or row_data)
        if not payload_id:
            return ""
        max_heavy_atoms = self._runtime.amdock_configuration.get_value(MAX_2D_PREVIEW_HEAVY_ATOMS_PATH)
        theme = detect_preview_theme()
        key = (int(payload_id), theme, int(max_heavy_atoms))
        if key in self._svgs:
            self._svgs.move_to_end(key)
            return self._svgs[key]
        if key not in self._pending:
            self._pending.add(key)
            store = payload_store(self._runtime)

            def draw() -> str:
                data = store.get(key[0])
                kind = store.kinds([key[0]]).get(key[0], "")
                return _molecule_svg_for_payload(data, kind, theme=theme, max_heavy_atoms=key[2])

            run_async(draw, lambda svg: self._landed(key, svg), lambda error: self._landed(key, ""))
        return ""

    def _landed(self, key: tuple, svg: str) -> None:
        self._pending.discard(key)
        self._svgs[key] = svg
        while len(self._svgs) > _PREVIEW_CACHE_SIZE:
            self._svgs.popitem(last=False)
        PREVIEW_READY.ready.emit()


def molecule_2d_preview_paint_for_runtime(runtime):
    return MoleculePreviews(runtime).paint


def molecule_2d_preview_tooltip(row_data: dict) -> str:
    raw = _row_raw_object(row_data)
    name = getattr(raw, "name", "") if raw is not None else row_data.get("name", "")
    return str(name or "").strip()


def project_table(runtime, config: TableConfig, parent: QWidget | None = None) -> SmartTableView:
    """A table bound to the active project's database.

    This module is the one place in ``ui/`` allowed to touch ``project_db``
    (see test_ui_api_boundary); widgets that build their own tables outside a
    BoundTableWidget go through here instead of reaching into the runtime.
    """
    return SmartTableView(db=runtime.workspace.project_db, config=config, parent=parent)


class SelectionStore(QObject):
    """The ids picked with "Select", per role ("ligand" / "receptor"), shared by a window.

    Ligands and Receptors are filters over the same molecules, so one selection is one list of
    molecule ids whichever table shows it: every table bound to a role mirrors it as its
    `id IN [...]` filter (see BoundTableWidget.bind_selection), and that is how a tool finds
    the selection made for another one.
    """

    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ids: dict[str, list[int]] = {}

    def get(self, role: str) -> list[int] | None:
        return self._ids.get(role)

    def set(self, role: str, ids) -> None:
        ids = sorted({int(value) for value in ids or ()}) or None
        if ids == self._ids.get(role):
            return
        if ids is None:
            del self._ids[role]
        else:
            self._ids[role] = ids
        self.changed.emit(role)


class BoundTableWidget(QWidget):
    # Subclasses set this to enable row deletion ("molecule" / "complex" / "result").
    delete_kind: str | None = None
    # The SelectionStore role this table's `id IN [...]` filter mirrors. See bind_selection.
    selection_role: str | None = None
    # Subclasses set this to True to offer "Select" - turning the highlighted rows into an
    # `id IN [...]` column filter. See _install_select.
    selectable: bool = False
    # Subclasses set this to True to offer "Edit…" on small molecules (Ketcher, ui/viewers/ketcher_editor).
    editable: bool = False

    # Re-emits the embedded table count in case a host needs it; the visible counter is the
    # one in the ms_table toolbar (show_record_count).
    records_changed = Signal(int)
    selection_count_changed = Signal(int)

    def __init__(self, *, runtime, config: TableConfig, empty_text: str, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._table: SmartTableView | None = None
        self._base_clauses: dict[str, object] = {}
        self._base_context_actions: dict = dict(config.context_menu_actions or {})
        # What pop_scope() puts back where a scope overrode a filter (absent field = remove).
        self._default_filters = {f.field: deepcopy(f) for f in (config.default_filters or [])}
        self._scopes: dict[str, set[str]] = {}
        self._job_bar: ActionBar | None = None
        self._jobs: JobFollower | None = None
        self._selection_store: SelectionStore | None = None
        self._selection_adopted = False
        self._mirrored_ids: list[int] | None = None  # what this table and the store last agreed on

        layout = QVBoxLayout(self)
        # No wrapper margins: Qt's default ~11px would inset the whole table relative to the
        # surrounding components (e.g. the Scope row / tab pane), which reads as an extra top
        # margin and a misaligned table.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # active_context = getattr(runtime, "active_context", None)
        # if active_context is None:
        #     label = QLabel(empty_text, self)
        #     label.setAlignment(Qt.AlignCenter)
        #     layout.addWidget(label)
        #     return

        self._table = SmartTableView(db=runtime.workspace.project_db, config=config, parent=self)
        self.record_count: int | None = None  # rows of the last load; None before the first
        self._table.data_refreshed.connect(lambda total: setattr(self, "record_count", int(total)))
        self._table.data_refreshed.connect(self.records_changed)
        self._table.data_refreshed.connect(self._publish_selection)
        self._show_first_pending = False  # activated before its rows landed: show row 1 when they do
        self._table.data_refreshed.connect(self._show_first_when_loaded)
        self._table.selection_changed.connect(lambda objs: self.selection_count_changed.emit(len(objs)))
        # Restore this table's saved view prefs (visible columns + sort), then persist
        # any interactive changes back to AMDock's own config, keyed by the widget class.
        self._apply_saved_view_state()
        self._table.view_state_changed.connect(self._persist_view_state)
        if any(column.field == PREVIEW_2D_COLUMN_FIELD for column in config.columns):
            PREVIEW_READY.ready.connect(self._table.repaint_rows)  # a depiction landed
        layout.addWidget(self._table)
        # Delete key removes the selected rows on any table that opts in via delete_kind.
        delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._table)
        delete_shortcut.activated.connect(self.delete_selected)
        if self.selectable:
            self._install_select(config)
        if self.editable:
            config.context_menu_actions = {**dict(config.context_menu_actions or {}), "Edit…": self._edit_rows}
        if self.delete_kind == "molecule":
            config.context_menu_actions = {**dict(config.context_menu_actions or {}),
                                           "Export structures (ZIP)…": self._export_structures}
        if self.delete_kind is not None:  # last in the menu, the one that cannot be undone
            config.context_menu_actions = {**dict(config.context_menu_actions or {}), "Delete…": self.delete_selected}

    # --- Jobs filling this table ------------------------------------------------------
    def follow_jobs(self, job_ids, *, noun: str, stage: str) -> None:
        """A progress strip under the table while the jobs that fill it run; hides after.

        Built on first use, so a table nobody follows carries no bar. Same ActionBar +
        JobFollower as the tool panels: progress, Cancel, and a link to the Jobs monitor.
        """
        job_ids = [str(job_id) for job_id in job_ids or () if job_id]
        if not job_ids:
            return
        if self._job_bar is None:
            self._job_bar = ActionBar(self)
            self.layout().addWidget(self._job_bar)
            self._jobs = JobFollower(self, self._job_bar, noun=noun)
            self._jobs.finished.connect(lambda _status: QTimer.singleShot(_JOB_BAR_LINGER_MS, self._hide_job_bar))
        self._jobs.noun = noun
        self._jobs.follow([(stage, job_id) for job_id in job_ids])
        self._job_bar.show()

    def _hide_job_bar(self) -> None:
        if self._job_bar is not None and not self._job_bar.is_running:  # a newer run keeps it
            self._job_bar.hide()

    # --- Scope = what the table shows -----------------------------------------------
    # There is no scope object anywhere: a tool acts on the rows its table is showing, and
    # narrowing the table is how you set that. "Select" is just a shortcut for typing the
    # highlighted ids into the ID column filter by hand, so it shows up as an ordinary
    # filter — visible in the header, cleared from the header.
    def _install_select(self, config: TableConfig) -> None:
        config.context_menu_actions = {
            "Select": self._select_rows,
            **dict(config.context_menu_actions or {}),
        }

    def _select_rows(self, objects) -> None:
        ids = sorted({int(getattr(obj, "id", 0) or 0) for obj in objects or ()} - {0})
        if not ids or self._table is None:
            return
        role = type(self).selection_role
        store = self._store() if role and not self.selection_role else None
        if store is not None:
            # Lent to a tool that set the selection aside (bind_selection(None)): the rows still
            # become the role's selection; whether it narrows this table is the tool's call.
            store.set(role, ids)
            return
        self._table.set_filter(FilterSpec("id", FilterOperator.IN, ids))

    # --- The selection, shared between tables ----------------------------------------
    # ms_table has no "filters changed" signal; every filter change reloads, so data_refreshed
    # is where the table's id filter is read back into the store.
    def _store(self) -> SelectionStore | None:
        if self._selection_store is None:
            store = getattr(self.window(), "selection", None)
            if isinstance(store, SelectionStore):
                self._selection_store = store
                store.changed.connect(self._on_selection_changed)
        return self._selection_store

    def _selected_ids(self) -> list[int] | None:
        spec = next((f for f in self._table._builder.active_filters if f.field == "id"), None)
        if spec is None or spec.op != FilterOperator.IN:
            return None
        return sorted(int(value) for value in spec.value or ()) or None

    def _publish_selection(self, *_args) -> None:
        store = self._store()
        if store is None or not self.selection_role or self._REVEAL_SCOPE in self._scopes:
            return  # a revealed row is a look, not the role's selection
        if not self._selection_adopted or store.get(self.selection_role) != self._mirrored_ids:
            # First load, or the store moved and another listener reloaded this table before
            # our own _on_selection_changed ran: the store wins, the stale filter is not echoed.
            self._adopt_selection()
        else:
            self._mirrored_ids = self._selected_ids()
            store.set(self.selection_role, self._mirrored_ids)

    def _adopt_selection(self) -> None:
        store = self._store()
        if store is None or not self.selection_role or self._table is None or self._REVEAL_SCOPE in self._scopes:
            return
        self._selection_adopted = True
        ids = self._mirrored_ids = store.get(self.selection_role)
        if ids != self._selected_ids():
            self.set_base_filter("id", FilterSpec("id", FilterOperator.IN, ids) if ids else None)

    def _on_selection_changed(self, role: str) -> None:
        if role == self.selection_role:
            self._adopt_selection()

    def bind_selection(self, role: str | None) -> None:
        """Mirror a role's selection as this table's id filter (None lets go of it).

        For a table whose role depends on who borrows it: Molecules is every role at once, so
        the tool that narrows it to one kind also says whose selection that is.
        """
        if role == self.selection_role or self._table is None:
            return
        self.selection_role = role
        if role:
            self._adopt_selection()
        elif self._selected_ids() is not None:
            self.set_base_filter("id", None)  # the selection stays in the store, not here

    def scope_ids(self) -> list[int] | None:
        """The ids of the rows this table shows, or None when it narrows nothing.

        None is not "no rows": it means the user added no filter beyond the catalog's own
        defaults and whatever the calling tool already pushed, so the caller's scope is
        already right and nobody has to materialise every id.

        ponytail: materialises the ids. The scale-free version is the builder's
        to_query_spec() pushed down as a subquery — do that when a tool has to run on 10^5
        filtered rows, not before.
        """
        if self._table is None or not self._is_narrowed():
            return None
        return [int(value) for value in self._table.all_filtered_ids() if int(value) > 0]

    def _is_narrowed(self) -> bool:
        # A tool's own pushed filters don't count: it already applies them to its scope, and
        # treating them as narrowing would materialise the whole catalog on every refresh.
        pushed = {field for fields in self._scopes.values() for field in fields}
        return any(
            spec.field not in pushed and self._default_filters.get(spec.field) != spec
            for spec in self._table._builder.active_filters
        )

    # --- Persisted per-table view preferences --------------------------------
    def _table_pref_path(self) -> str:
        # Stable id per table: the concrete widget class name (dot-free → safe as a config path).
        return f"tables.{type(self).__name__}"

    def _apply_saved_view_state(self) -> None:
        cfg = getattr(self.runtime, "amdock_configuration", None)
        if cfg is None or self._table is None:
            return
        try:
            state = cfg.get_value(self._table_pref_path())
        except Exception:  # noqa: BLE001 - missing entry / no config file: use table defaults
            return
        self._table.apply_view_state(state.model_dump() if hasattr(state, "model_dump") else state)

    def _persist_view_state(self) -> None:
        cfg = getattr(self.runtime, "amdock_configuration", None)
        if cfg is None or self._table is None:
            return
        try:
            cfg.set_value(self._table_pref_path(), self._table.view_state())
        except Exception:  # noqa: BLE001 - config persistence is best-effort, never block the UI
            pass

    @property
    def table(self) -> Optional[SmartTableView]:
        return self._table

    def set_empty_state(self, message: str | None = None, *, show_action: bool | None = None) -> None:
        """Empty-state text for a table narrowed from outside (None restores the default)."""
        if self._table is not None:
            self._table.set_empty_state(message, show_action=show_action)

    def selected_count(self) -> int:
        """Number of selected rows (for the statusbar); 0 if there is no table."""
        return len(self._table.get_selected_objects()) if self._table is not None else 0

    def _edit_rows(self, objects) -> None:
        from amdockvs.ui.viewers.ketcher_editor import edit_molecule

        def done(edited_id: int) -> None:
            # The edit has a new file (or is a new molecule): show it in PyMOL.
            self._table.refresh_preserving_view()
            edited = self.runtime.molecules.get(edited_id)
            if edited is not None:
                self._load_object_in_pymol(edited)

        edit_molecule(self, self.runtime, objects, done=done)

    def _export_structures(self, objects) -> None:
        from amdockvs.ui.catalog.export import export_molecules

        export_molecules(self, self.runtime, objects)

    def delete_selected(self, objects=None) -> None:
        """Delete `objects` (the rows a context menu acts on), or the selected rows."""
        if self.delete_kind is None or self._table is None:
            return
        from amdockvs.ui.catalog.deletion import delete_selected_objects

        delete_selected_objects(self, kind=self.delete_kind, objects=objects)

    # --- PyMOL follows the active tab ----------------------------------------
    def _load_object_in_pymol(self, obj) -> None:
        """Structural views override this to render a row's object in PyMOL."""
        raise NotImplementedError

    def clear_pymol(self) -> None:
        dock = getattr(self.window(), "pymol_dock", None)
        cmd = getattr(dock, "cmd", None) if dock is not None else None
        if cmd is None:
            return
        try:
            cmd.delete("all")
        except Exception:
            pass

    def show_active_in_pymol(self) -> None:
        """Called when this view becomes the active tab: mirror PyMOL to the first row
        (or clear PyMOL when the table is empty / this view has nothing structural)."""
        table = self._table
        obj = None
        if table is not None and table._model.rowCount() > 0:
            obj = table._model.get_raw_object(0)
        self._show_first_pending = obj is None and table is not None
        if obj is None:
            self.clear_pymol()
            return
        try:
            table._table.selectRow(0)
        except Exception:
            pass
        try:
            self._load_object_in_pymol(obj)
        except NotImplementedError:
            self.clear_pymol()

    def _show_first_when_loaded(self, *_args) -> None:
        if self._show_first_pending and self.isVisible() and self._table._model.rowCount() > 0:
            self.show_active_in_pymol()

    def refresh(self) -> None:
        if self._table is not None:
            self._table.refresh()

    def set_base_filter(self, field: str, spec=None) -> None:
        """Replace (spec) or remove (None) a base filter on the embedded table, in-place.

        Lets a host toggle drive what the table shows (the table stays the source of truth)
        instead of a parallel scope the user can't see.
        """
        table = self._table
        if table is None:
            return
        current = next((f for f in table._builder.active_filters if f.field == field), None)
        if current == spec:
            # Re-applying an identical filter still rewinds the builder to page 1, and in
            # INFINITE mode that discards every row scrolled in so far (looks like paging).
            # Hosts re-sync on every refresh, so no-op unless the filter actually changed.
            return
        if spec is None:
            table._builder.remove_filter(field)
        else:
            table._builder.add_filter(spec)
        table._sync_header()  # the column shows it is filtered, as when the user filters it
        table.refresh()

    def set_base_clause(self, key: str, clause=None) -> None:
        """Like set_base_filter, for a condition FilterSpec can't express (a subquery).

        Keyed instead of field-keyed because a clause has no single field, and idempotent
        for the same reason set_base_filter is: hosts re-push on every refresh, and a
        needless rewind throws away everything scrolled in.
        """
        table = self._table
        if table is None:
            return
        if self._base_clauses.get(key) is clause:
            return
        self._base_clauses[key] = clause
        table.set_external_clause(key, clause)

    # --- Scope: what a tool does to a borrowed table, pushed and released as one ------
    def push_scope(self, key: str, *, filters=(), clause=None, actions=(),
                   empty_message: str | None = None, show_action: bool | None = None) -> None:
        """Narrow this table on behalf of a tool, under a single key.

        Filters, clause, toolbar actions and empty state travel together because they are one
        statement ("this is the tool's scope"), and because releasing them one by one is how
        a table ends up silently filtered after the tool is gone — pop_scope(key) undoes all
        four. Idempotent: hosts re-push on every refresh and each piece no-ops when unchanged.
        """
        if self._table is None:
            return
        fields = {spec.field for spec in filters}
        # A push that stops overriding a field must give that field back, not just stop caring.
        for stale in self._scopes.get(key, set()) - fields:
            self.set_base_filter(stale, self._default_filters.get(stale))
        for spec in filters:
            self.set_base_filter(spec.field, spec)
        self._scopes[key] = fields
        self._table.set_config_actions_visible(False)
        self.set_base_clause(key, clause)
        self.set_toolbar_actions(key, actions)
        self.set_empty_state(empty_message, show_action=show_action)

    def pop_scope(self, key: str) -> None:
        """Hand the table back: the catalog's own default filters return, not "no filter"."""
        if self._table is None:
            return
        for field in self._scopes.pop(key, ()):
            self.set_base_filter(field, self._default_filters.get(field))
        if not self._scopes:
            self._table.set_config_actions_visible(True)
        self.set_base_clause(key, None)
        self.set_toolbar_actions(key, ())
        self.set_empty_state(None)

    _REVEAL_SCOPE = "reveal"

    def reveal(self, ids, *, label: str) -> None:
        """Show only these rows, the first one highlighted, until the user clicks "Show all".

        Another tab's way of saying "this one" (Docking Results → Ligands): it never becomes
        the role's selection, and "Show all" gives the table back with that selection intact.
        """
        if self._table is None:
            return

        def show_all(_objects=None) -> None:
            # Put the role's selection back while still revealing (that reload must not publish
            # "no filter" as the selection), then release a scope that no longer owns "id".
            store = self._store()
            ids = store.get(self.selection_role) if store is not None and self.selection_role else None
            self._scopes[self._REVEAL_SCOPE] = set()
            self.set_base_filter("id", FilterSpec("id", FilterOperator.IN, ids) if ids
                                 else self._default_filters.get("id"))
            self.pop_scope(self._REVEAL_SCOPE)

        self._table.data_refreshed.connect(self._select_revealed, Qt.SingleShotConnection)
        # Marked before the filter lands: its reload must not publish the id as the selection.
        self._scopes.setdefault(self._REVEAL_SCOPE, set())
        self.push_scope(
            self._REVEAL_SCOPE,
            filters=[FilterSpec("id", FilterOperator.IN, sorted({int(i) for i in ids}))],
            actions=[ToolbarAction(label="Show all", on_click=show_all,
                                   tooltip=f"Showing only {label}. Click to see every row again.")],
            empty_message=f"{label} is hidden by this table's other filters",
        )

    def _select_revealed(self, *_args) -> None:
        if self._table is not None:
            self._table.select_first_row()

    def set_toolbar_actions(self, key: str, actions=()) -> None:
        """Lend this table's toolbar to a tool (empty/None hands it back).

        Companion of set_base_filter/set_base_clause: a tool that narrows the table can also
        act on it, and releases both when it is hidden.
        """
        if self._table is not None:
            self._table.set_toolbar_actions(key, list(actions or []))

    def background_refresh(self) -> bool:
        if self._table is None:
            return True
        return bool(self._table.background_refresh())

    def ensure_viewport_filled(self, force: bool = False) -> bool:
        """Job start-up phase: fill the visible viewport as soon as possible (see main_window)."""
        if self._table is None:
            return True
        return bool(self._table.ensure_viewport_filled(force=force))

    def refresh_counts(self) -> bool:
        """Only the row counter (COUNT), without reloading the table. See main_window:
        during a job the rows are loaded once and afterwards only the total is refreshed."""
        if self._table is None:
            return True
        return bool(self._table.refresh_counts())

