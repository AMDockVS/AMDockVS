from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

from amdockvs.configuration import (
    DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS,
    MAX_2D_PREVIEW_HEAVY_ATOMS_PATH,
)
from amdockvs.molecule_paths import preferred_molecule_path
from ms_components.ms_table import TableConfig, SmartTableView


_PREVIEW_WIDTH = 256
_PREVIEW_HEIGHT = 192
PREVIEW_2D_COLUMN_FIELD = "__preview_2d__"


def _row_raw_object(row_data: dict) -> Any | None:
    return row_data.get("__raw__")


def display_project_relative_path(path_text: Any) -> str:
    raw = str(path_text or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_absolute():
        return raw
    parts = path.parts
    if "data" in parts:
        return str(Path(*parts[parts.index("data") :]))
    return raw


def supports_2d_depiction(record_or_row: Any) -> bool:
    raw = _row_raw_object(record_or_row) if isinstance(record_or_row, dict) else record_or_row
    return str(getattr(raw, "molecule_type", "") or "") == "small_molecule"


def resolve_molecule_display_path(record_or_row: Any) -> Path:
    target = _row_raw_object(record_or_row) if isinstance(record_or_row, dict) else record_or_row
    resolved = preferred_molecule_path(target if target is not None else record_or_row)
    return resolved if resolved is not None else Path()


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
        if suffix == ".sdf":
            supplier = Chem.SDMolSupplier(str(path), sanitize=True, removeHs=False)
            mol = next((item for item in supplier if item is not None), None)
        elif suffix in {".mol", ".mdl"}:
            mol = Chem.MolFromMolFile(str(path), sanitize=True, removeHs=False)
        elif suffix == ".mol2":
            mol = Chem.MolFromMol2File(str(path), sanitize=True, removeHs=False)
        elif suffix == ".pdb":
            mol = Chem.MolFromPDBFile(str(path), sanitize=True, removeHs=False)

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


@lru_cache(maxsize=1024)
def _cached_molecule_svg(
    path_text: str,
    mtime_ns: int,
    width: int = _PREVIEW_WIDTH,
    height: int = _PREVIEW_HEIGHT,
    theme: str = "auto",
    max_heavy_atoms: int = DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS,
) -> str:
    del mtime_ns
    return _molecule_svg_for_path(
        path_text,
        width=width,
        height=height,
        theme=theme,
        max_heavy_atoms=max_heavy_atoms,
    )


def molecule_2d_preview_paint(
    row_data: dict,
    *,
    max_heavy_atoms: int = DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS,
) -> str:
    if not supports_2d_depiction(row_data):
        return ""
    path = resolve_molecule_display_path(row_data)
    if not path.exists():
        return ""
    try:
        stat = path.stat()
    except OSError:
        return ""
    return _cached_molecule_svg(
        str(path),
        stat.st_mtime_ns,
        theme=detect_preview_theme(),
        max_heavy_atoms=max_heavy_atoms,
    )


def molecule_2d_preview_paint_for_runtime(runtime):
    def paint(row_data: dict) -> str:
        max_heavy_atoms = runtime.amdock_configuration.get_value(MAX_2D_PREVIEW_HEAVY_ATOMS_PATH)
        return molecule_2d_preview_paint(row_data, max_heavy_atoms=max_heavy_atoms)

    return paint


def molecule_2d_preview_tooltip(row_data: dict) -> str:
    raw = _row_raw_object(row_data)
    name = getattr(raw, "name", "") if raw is not None else row_data.get("name", "")
    current_path = getattr(raw, "current_path", "") if raw is not None else row_data.get("current_path", "")
    stored_path = getattr(raw, "stored_path", "") if raw is not None else row_data.get("stored_path", "")
    lines = [str(name or "").strip()]
    if current_path:
        lines.append(f"Current: {current_path}")
    if stored_path:
        lines.append(f"Stored: {stored_path}")
    return "\n".join(line for line in lines if line)


def project_table(runtime, config: TableConfig, parent: QWidget | None = None) -> SmartTableView:
    """A table bound to the active project's database.

    This module is the one place in ``ui/`` allowed to touch ``project_db``
    (see test_ui_api_boundary); widgets that build their own tables outside a
    BoundTableWidget go through here instead of reaching into the runtime.
    """
    return SmartTableView(db=runtime.molsuite.project_db, config=config, parent=parent)


class BoundTableWidget(QWidget):
    # Subclasses set this to enable row deletion ("molecule" / "complex" / "result").
    delete_kind: str | None = None

    # Re-emits the embedded table count in case a host needs it; the visible counter is the
    # one in the ms_table toolbar (show_record_count).
    records_changed = Signal(int)
    selection_count_changed = Signal(int)

    def __init__(self, *, runtime, config: TableConfig, empty_text: str, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._table: SmartTableView | None = None
        self._base_clauses: dict[str, object] = {}
        # What pop_scope() puts back where a scope overrode a filter (absent field = remove).
        self._default_filters = {f.field: deepcopy(f) for f in (config.default_filters or [])}
        self._scopes: dict[str, set[str]] = {}

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

        self._table = SmartTableView(db=runtime.molsuite.project_db, config=config, parent=self)
        self._table.data_refreshed.connect(self.records_changed)
        self._table.selection_changed.connect(lambda objs: self.selection_count_changed.emit(len(objs)))
        # Restore this table's saved view prefs (visible columns + sort), then persist
        # any interactive changes back to AMDock's own config, keyed by the widget class.
        self._apply_saved_view_state()
        self._table.view_state_changed.connect(self._persist_view_state)
        layout.addWidget(self._table)
        # Delete key removes the selected rows on any table that opts in via delete_kind.
        delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._table)
        delete_shortcut.activated.connect(self.delete_selected)

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

    def delete_selected(self) -> None:
        if self.delete_kind is None or self._table is None:
            return
        from amdockvs.ui.catalog.deletion import delete_selected_objects

        delete_selected_objects(self, kind=self.delete_kind)

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
        self.set_base_clause(key, clause)
        self.set_toolbar_actions(key, actions)
        self.set_empty_state(empty_message, show_action=show_action)

    def pop_scope(self, key: str) -> None:
        """Hand the table back: the catalog's own default filters return, not "no filter"."""
        if self._table is None:
            return
        for field in self._scopes.pop(key, ()):
            self.set_base_filter(field, self._default_filters.get(field))
        self.set_base_clause(key, None)
        self.set_toolbar_actions(key, ())
        self.set_empty_state(None)

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

