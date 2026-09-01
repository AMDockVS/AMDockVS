"""Molecule Tools workspace for P2Rank pocket prediction and PyMOL inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.models import BindingSite, MoleculeRecord
from amdockvs.molecule_paths import preferred_molecule_path
from amdockvs.ui.async_query import run_async
from amdockvs.ui.catalog.receptors import RECEPTOR_VIEW_ID
from amdockvs.ui.tools.pymol_ribbon import (
    apply_receptor_atom_coloring,
    set_pymol_scene_context,
)
from ms_components.ms_table import FilterOperator, FilterSpec


POCKET_DETECTION_VIEW_ID = "moltools.pocket_detection"
POCKET_SITES_VIEW_ID = "moltools.binding_sites"
PROFILE_OPTIONS = (
    ("Standard", "default"),
    ("AlphaFold / NMR / cryo-EM", "alphafold"),
)
P2RANK_SCORE_COLORS = (
    "#440154",
    "#482878",
    "#3E4989",
    "#31688E",
    "#26828E",
    "#1F9E89",
    "#35B779",
    "#6DCD59",
    "#B4DE2C",
    "#FDE725",
)


def _score_palette_indices(scores: list[float]) -> list[int]:
    """Map scores to a stable low→high discrete gradient, preserving input order."""
    if not scores:
        return []
    if len(scores) == 1:
        return [len(P2RANK_SCORE_COLORS) - 1]
    ordered_indices = sorted(
        range(len(scores)),
        key=lambda index: (float(scores[index]), index),
    )
    result = [0] * len(scores)
    last_position = len(ordered_indices) - 1
    last_color = len(P2RANK_SCORE_COLORS) - 1
    for position, original_index in enumerate(ordered_indices):
        result[original_index] = round(position * last_color / last_position)
    return result


def _hex_rgb(color: str) -> list[float]:
    parsed = QColor(str(color))
    return [parsed.redF(), parsed.greenF(), parsed.blueF()]


class BindingSitesWidget(QWidget):
    """One receptor's binding sites, in the auxiliary zone under the Receptors table.

    Reads the sites from the project (`pockets.list_predictions`), not from the tool that
    produced them, so it only needs to be told which receptor to show — that is why it can
    be a plain view instead of a panel of Pocket Detection.
    """

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._receptor_id: int | None = None
        self._sites: list[BindingSite] = []
        self._results_token = 0
        self._stale = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.table = QTableWidget(0, 8, self)
        self.table.setHorizontalHeaderLabels(
            ["BS", "Rank", "Score", "Probability", "Profile", "Center", "Box size", "Residues"]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_prediction_selection_changed)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        actions.setContentsMargins(4, 0, 4, 4)
        self.activate_button = QPushButton("Use as Active Docking Site", self)
        self.activate_button.clicked.connect(self._activate_selected)
        self.activate_button.setEnabled(False)
        actions.addWidget(self.activate_button)
        self.status_label = QLabel("Select a receptor to see its predicted sites.", self)
        self.status_label.setWordWrap(True)
        actions.addWidget(self.status_label, 1)
        layout.addLayout(actions)

    def show_receptor(self, receptor_id: int | None) -> None:
        """Follow the Receptors table: whatever row is selected is what this panel shows."""
        receptor_id = int(receptor_id or 0) or None
        if receptor_id == self._receptor_id:
            return
        self._receptor_id = receptor_id
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if self._stale:
            self.refresh()

    def refresh(self) -> None:
        if not self.isVisible():
            # A0: nothing loads off screen. The receptor is remembered; showEvent picks it up.
            self._stale = True
            return
        self._stale = False
        self._results_token += 1
        token = self._results_token
        receptor_id = self._receptor_id
        if receptor_id is None:
            self._fill_results([])
            return
        run_async(
            lambda: self.runtime.pockets.list_predictions(receptor_id=receptor_id),
            lambda sites: self._apply_results(receptor_id, token, sites),
            on_error=lambda exc: self._show_error("Could not load P2Rank results", exc),
            busy=self.table,
        )

    refresh_view = refresh

    def _apply_results(self, receptor_id: int, token: int, sites: list[BindingSite]) -> None:
        if token != self._results_token or receptor_id != self._receptor_id:
            return
        self._fill_results(sites)

    @staticmethod
    def _site_extra(site: BindingSite) -> dict[str, Any]:
        return dict(site.extra_data or {})

    def _fill_results(self, sites: list[BindingSite]) -> None:
        self._sites = list(sites)
        palette_indices = _score_palette_indices(
            [float(self._site_extra(site).get("score") or 0.0) for site in sites]
        )
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(sites))
        for row, site in enumerate(sites):
            extra = self._site_extra(site)
            center = (
                f"{float(site.center_x or 0):.2f}, "
                f"{float(site.center_y or 0):.2f}, "
                f"{float(site.center_z or 0):.2f}"
            )
            size = (
                f"{float(site.size_x or 0):.1f} × "
                f"{float(site.size_y or 0):.1f} × "
                f"{float(site.size_z or 0):.1f}"
            )
            residues = " ".join(str(value) for value in (extra.get("residue_ids") or []))
            values = (
                str(int(site.id or 0)),
                str(int(extra.get("rank") or site.source_ref or 0)),
                f"{float(extra.get('score') or 0):.2f}",
                f"{float(extra.get('probability') or 0):.3f}",
                str(extra.get("profile") or "default"),
                center,
                size,
                residues,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, site)
                elif column == 2:
                    color = QColor(P2RANK_SCORE_COLORS[palette_indices[row]])
                    item.setBackground(QBrush(color))
                    luminance = (
                        color.red() * 299
                        + color.green() * 587
                        + color.blue() * 114
                    ) / 1000
                    item.setForeground(
                        QBrush(QColor("black" if luminance >= 150 else "white"))
                    )
                    item.setToolTip(
                        "Pocket color in PyMOL. Viridis scale: violet = lower score, "
                        "yellow = higher score."
                    )
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        if sites:
            # Nothing preselected on purpose: the receptor's own view lands after this load, so
            # a preselected row would look shown without being shown, and clicking it — the
            # first thing anyone tries — would change no selection and do nothing.
            self.status_label.setText(f"{len(sites)} predicted site(s) — click one to see it in PyMOL.")
        else:
            self._clear_pymol_pockets()
            self.status_label.setText(
                "No predicted sites for this receptor yet — run Pocket Detection."
                if self._receptor_id is not None
                else "Select a receptor to see its predicted sites."
            )
        self.activate_button.setEnabled(False)  # nothing selected yet; the selection turns it on

    def _selected_sites(self) -> list[BindingSite]:
        selected: list[BindingSite] = []
        for index in self.table.selectionModel().selectedRows(0):
            item = self.table.item(index.row(), 0)
            value = item.data(Qt.UserRole) if item is not None else None
            if isinstance(value, BindingSite):
                selected.append(value)
        return selected

    def _current_site(self) -> BindingSite | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        value = item.data(Qt.UserRole) if item is not None else None
        return value if isinstance(value, BindingSite) else None

    def _on_prediction_selection_changed(self) -> None:
        sites = self._selected_sites()
        self.activate_button.setEnabled(bool(sites))
        if sites:
            self._show_sites_in_pymol(sites)
        else:
            self._clear_pymol_pockets()

    def _pymol_command(self):
        dock = getattr(self.window(), "pymol_dock", None)
        return dock, getattr(dock, "cmd", None) if dock is not None else None

    def _clear_pymol_pockets(self) -> None:
        _dock, cmd = self._pymol_command()
        if cmd is None:
            return
        try:
            cmd.delete("amdock_p2rank_*")
        except Exception:
            pass

    def _show_sites_in_pymol(self, sites: list[BindingSite]) -> None:
        dock, cmd = self._pymol_command()
        if cmd is None:
            self.status_label.setText("PyMOL is not available in this session.")
            return
        focus = self._current_site() or sites[0]
        try:
            receptor = self.runtime.pockets.get_receptor(int(focus.molecule_id))
            receptor_path = preferred_molecule_path(receptor)
            if receptor_path is None or not receptor_path.is_file():
                raise FileNotFoundError(
                    f"Receptor {focus.molecule_id} has no readable structure."
                )
            dock.show()
            self._clear_pymol_pockets()
            receptor_obj = "amdock_p2rank_receptor"
            pocket_selection = "amdock_p2rank_pockets"
            cmd.load(str(receptor_path), receptor_obj)
            apply_receptor_atom_coloring(cmd, receptor_obj)
            cmd.show_as("cartoon", receptor_obj)

            palette_indices = _score_palette_indices(
                [float(self._site_extra(site).get("score") or 0.0) for site in self._sites]
            )
            color_by_site = {
                id(site): palette_indices[index]
                for index, site in enumerate(self._sites)
            }
            by_points_path: dict[Path, list[BindingSite]] = {}
            for site in sites:
                points_path = Path(
                    str(self._site_extra(site).get("points_path") or "")
                ).expanduser()
                if not points_path.is_file():
                    raise FileNotFoundError(f"Points file not found: {points_path}")
                by_points_path.setdefault(points_path.resolve(), []).append(site)

            selections: list[str] = []
            site_number = 0
            for index, (points_path, path_sites) in enumerate(by_points_path.items(), start=1):
                points_obj = f"amdock_p2rank_points_{index}"
                cmd.load(str(points_path), points_obj)
                cmd.hide("everything", points_obj)
                for site in path_sites:
                    site_number += 1
                    rank = int(
                        self._site_extra(site).get("rank")
                        or site.source_ref
                        or 0
                    )
                    site_selection = f"amdock_p2rank_site_{site_number}"
                    cmd.select(
                        site_selection,
                        f"{points_obj} and resn STP and resi {rank}",
                    )
                    palette_index = color_by_site.get(
                        id(site),
                        len(P2RANK_SCORE_COLORS) - 1,
                    )
                    color_name = f"amdock_p2rank_score_{palette_index}"
                    cmd.set_color(
                        color_name,
                        _hex_rgb(P2RANK_SCORE_COLORS[palette_index]),
                    )
                    cmd.color(color_name, site_selection)
                    selections.append(site_selection)

            cmd.select(
                pocket_selection,
                " or ".join(f"({selection})" for selection in selections),
            )
            cmd.show("spheres", pocket_selection)
            cmd.set("sphere_scale", 0.4, pocket_selection)
            cmd.set("sphere_transparency", 0.1, pocket_selection)
            center_names: list[str] = []
            for index, site in enumerate(sites, start=1):
                extra = self._site_extra(site)
                palette_index = color_by_site.get(
                    id(site),
                    len(P2RANK_SCORE_COLORS) - 1,
                )
                color_name = f"amdock_p2rank_score_{palette_index}"
                center_name = f"amdock_p2rank_center_{index}"
                center_names.append(center_name)
                cmd.pseudoatom(
                    center_name,
                    pos=[
                        float(site.center_x or 0),
                        float(site.center_y or 0),
                        float(site.center_z or 0),
                    ],
                    name=f"P{int(extra.get('rank') or site.source_ref or 0)}",
                )
                cmd.show_as("spheres", center_name)
                cmd.set("sphere_scale", 0.6, center_name)
                cmd.color(color_name, center_name)
            center_expression = " or ".join(center_names)
            cmd.zoom(f"({pocket_selection} or {center_expression})", 5)
            set_pymol_scene_context(
                dock,
                "binding_site",
                target=f"({pocket_selection} or {center_expression})",
                selections={
                    "receptor": receptor_obj,
                    "pockets": pocket_selection,
                    "centers": center_expression,
                    # The PyMOL object names are the same for every pocket, so without this the
                    # scene memory keys all sites alike and replays one view over every zoom.
                    "site": f"{int(focus.molecule_id)}:"
                    + ",".join(str(int(site.id or 0)) for site in sites),
                },
                default_preset="amdockvs.binding_points",
            )
        except Exception as exc:
            self._show_error("Could not display P2Rank pockets", exc)

    def _activate_selected(self) -> None:
        site = self._current_site()
        if site is None:
            return
        try:
            self.runtime.docking.set_active_binding_site(
                molecule_id=int(site.molecule_id),
                binding_site_id=int(site.id or 0),
            )
            receptor = self.runtime.pockets.get_receptor(int(site.molecule_id))
            handler = getattr(self.window(), "_show_binding_site_from_details", None)
            if callable(handler):
                handler(receptor, site)
            self.status_label.setText(
                f"Binding site #{int(site.id or 0)} is now active for docking."
            )
        except Exception as exc:
            self._show_error("Could not activate binding site", exc)

    def _show_error(self, title: str, exc: Exception) -> None:
        self.status_label.setText(str(exc))
        QMessageBox.critical(self, title, str(exc))


class PocketDetectionWidget(QWidget):
    """P2Rank runner. It owns no table: the receptors it works on are the catalog's, borrowed
    while the tool is open, and its output is the Binding Sites panel in the auxiliary zone."""

    # This tool's scope key on any table it borrows (see BoundTableWidget.push_scope).
    _SCOPE_KEY = "pockets"

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._receptors: dict[int, dict[str, Any]] = {}
        self._defined_ligand_ids: set[int] = set()
        self._focused_receptor_id: int | None = None
        self._pending_job_ids: set[str] = set()
        self._job_signal_connected = False
        self._bound_receptor_table = None
        self._tool_ready = False
        # Without a project this widget is a label: show/hideEvent must not touch the rest.
        self._ready = False

        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to predict receptor pockets.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return

        # Not an installer: tools are installed in one place (Settings > External tools).
        self.dependency_box = QGroupBox("P2Rank Runtime", self)
        dependency_layout = QHBoxLayout(self.dependency_box)
        self.tool_status = QLabel("Checking P2Rank and Java…", self.dependency_box)
        self.tool_status.setWordWrap(True)
        dependency_layout.addWidget(self.tool_status, 1)
        self.open_settings_button = QPushButton("Open Settings", self.dependency_box)
        self.open_settings_button.clicked.connect(self._open_tool_settings)
        dependency_layout.addWidget(self.open_settings_button)
        outer.addWidget(self.dependency_box)

        prediction_box = QGroupBox("Prediction", self)
        prediction_layout = QVBoxLayout(prediction_box)

        filter_row = QHBoxLayout()
        self.exclude_defined_checkbox = QCheckBox(
            "Exclude receptors with defined ligands",
            prediction_box,
        )
        self.exclude_defined_checkbox.setChecked(True)
        self.exclude_defined_checkbox.setToolTip(
            "Hide receptors whose import metadata contains one or more reference ligands."
        )
        self.exclude_defined_checkbox.toggled.connect(self._on_exclusion_toggled)
        filter_row.addWidget(self.exclude_defined_checkbox)
        filter_row.addStretch(1)
        prediction_layout.addLayout(filter_row)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile", prediction_box))
        self.profile_combo = QComboBox(prediction_box)
        for label, value in PROFILE_OPTIONS:
            self.profile_combo.addItem(label, value)
        self.profile_combo.setToolTip(
            "P2Rank model used for the whole run. Changing it replaces the current "
            "predictions of the receptors in scope once the new calculation succeeds."
        )
        profile_row.addWidget(self.profile_combo, 1)
        prediction_layout.addLayout(profile_row)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Scope", prediction_box))
        self.scope_combo = QComboBox(prediction_box)
        self.scope_combo.addItem("Active (all eligible receptors)", "active")
        self.scope_combo.addItem("Selected (marked in table)", "selected")
        self.scope_combo.addItem("Filtered (all matching table filters)", "filtered")
        self.scope_combo.currentIndexChanged.connect(self._update_scope_label)
        scope_row.addWidget(self.scope_combo, 1)
        scope_row.addWidget(QLabel("Threads", prediction_box))
        self.threads_spin = QSpinBox(prediction_box)
        self.threads_spin.setRange(1, 128)
        self.threads_spin.setValue(1)
        scope_row.addWidget(self.threads_spin)
        self.run_button = QPushButton("Run P2Rank", prediction_box)
        self.run_button.clicked.connect(self._run_prediction)
        scope_row.addWidget(self.run_button)
        prediction_layout.addLayout(scope_row)

        self.scope_label = QLabel("Receptor scope unresolved.", prediction_box)
        self.scope_label.setWordWrap(True)
        prediction_layout.addWidget(self.scope_label)
        outer.addWidget(prediction_box)
        outer.addStretch(1)

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self._ready = True
        self._connect_job_signal()
        self.refresh()

    def _connect_job_signal(self) -> None:
        if self._job_signal_connected:
            return
        bridge = getattr(self.window(), "monitor_bridge", None)
        if bridge is not None:
            bridge.job_finished.connect(self._on_job_finished)
            self._job_signal_connected = True

    def refresh(self) -> None:
        run_async(
            lambda: (self.runtime.pockets.tool_status(), self.runtime.pockets.list_receptors()),
            self._apply_refresh,
            on_error=lambda exc: self._show_error("Refresh failed", exc),
            busy=self,
        )

    refresh_view = refresh

    def _apply_refresh(self, result) -> None:
        tool, receptors = result
        self._tool_ready = bool(tool.installed and (tool.java_version or 0) >= 17)
        self.tool_status.setText(
            tool.message if self._tool_ready
            else f"{tool.message}\nInstall it from Settings > External tools."
        )
        self.dependency_box.setVisible(not self._tool_ready)
        self._receptors = {int(row["id"]): dict(row) for row in receptors}
        self._defined_ligand_ids = {
            int(row["id"])
            for row in receptors
            if bool(row.get("has_defined_ligands"))
        }
        self._sync_receptor_scope()
        self.run_button.setEnabled(bool(receptors) and not self._pending_job_ids)
        self._update_scope_label()

    def _open_tool_settings(self) -> None:
        opener = getattr(self.window(), "open_settings", None)
        if callable(opener):
            opener()

    def _on_exclusion_toggled(self, _checked: bool) -> None:
        self._sync_receptor_scope()
        self._update_scope_label()

    # --- The borrowed Receptors table ----------------------------------------
    def _catalog_receptor_widget(self):
        """The catalog Receptors tab, if it is open — this tool's receptor table."""
        central = getattr(self.window(), "central_widget", None)
        if central is None:
            return None
        try:
            return central.open_view(RECEPTOR_VIEW_ID)
        except Exception:  # noqa: BLE001 - a missing/failed view must not break refresh
            return None

    def _sync_receptor_scope(self) -> None:
        """Push what this tool works on onto the catalog Receptors table: receptors only,
        minus the ones whose import metadata already defines a reference ligand (their site
        is known, so predicting it is wasted work)."""
        widget = self._catalog_receptor_widget()
        if widget is None:
            return
        excluded = sorted(self._defined_ligand_ids) if self.exclude_defined_checkbox.isChecked() else []
        filters = [FilterSpec("is_receptor", FilterOperator.EQ, True, label="role_receptor")]
        if excluded:
            filters.append(
                FilterSpec("id", FilterOperator.NOT_IN, excluded, label="without_defined_ligands")
            )
        widget.push_scope(
            self._SCOPE_KEY,
            filters=filters,
            empty_message="No receptors left to predict in this scope" if excluded else None,
            show_action=not excluded,
        )
        self._bind_receptor_table_signals(widget)
        if self._focused_receptor_id in set(excluded):
            self._focus_receptor(None)
        else:
            self._adopt_table_selection()

    def _adopt_table_selection(self) -> None:
        """Opening the tool over a row that is already selected fires no signal, and the panel
        would sit empty next to a highlighted receptor."""
        table = self._bound_receptor_table
        if table is None:
            return
        selected = next(iter(table.get_selected_objects() or []), None)
        if selected is not None:
            # Only ever adopts; a reload that drops the selection must not blank the panel.
            self._focus_receptor(int(getattr(selected, "id", 0) or 0))

    def _bind_receptor_table_signals(self, widget) -> None:
        # The catalog tab outlives this tool and can be closed/reopened, so bind per widget
        # instance and only once.
        table = getattr(widget, "table", None)
        if table is None or table is self._bound_receptor_table:
            return
        table.row_clicked.connect(self._on_receptor_clicked)
        table.selection_changed.connect(self._on_receptor_selection_changed)
        table.refresh_clicked.connect(self.refresh)
        table.data_refreshed.connect(self._on_receptor_table_refreshed)
        self._bound_receptor_table = table

    def _release_receptor_table(self) -> None:
        widget = self._catalog_receptor_widget()
        if widget is not None:
            widget.pop_scope(self._SCOPE_KEY)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._ready:
            return
        opener = getattr(self.window(), "open_or_focus_view", None)
        if callable(opener):
            # The tool has no table of its own: the receptors it works on are the catalog's.
            opener(RECEPTOR_VIEW_ID)
        self._sync_receptor_scope()

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self._ready:
            return
        self._release_receptor_table()

    def _on_receptor_table_refreshed(self, _total: int) -> None:
        # Rows arrive async, so the selection to follow may only exist after the load.
        self._update_scope_label()
        self._adopt_table_selection()

    def _on_receptor_clicked(self, receptor: MoleculeRecord) -> None:
        self._focus_receptor(int(receptor.id or 0))

    def _on_receptor_selection_changed(self, receptors: list[object]) -> None:
        receptor = next(iter(receptors or []), None)
        self._focus_receptor(int(getattr(receptor, "id", 0) or 0))
        self._update_scope_label()

    def _sites_view(self):
        """The auxiliary Binding Sites panel, if it is on screen (it is the tool's output)."""
        getter = getattr(self.window(), "aux_view", None)
        return getter(POCKET_SITES_VIEW_ID) if callable(getter) else None

    def _focus_receptor(self, receptor_id: int | None) -> None:
        receptor_id = int(receptor_id or 0) or None
        self._focused_receptor_id = receptor_id
        view = self._sites_view()
        if view is not None:
            view.show_receptor(receptor_id)

    def _reload_sites(self) -> None:
        """New predictions landed: reload the panel showing them, if it is open."""
        view = self._sites_view()
        if view is not None:
            view.refresh()

    def _scope_mode(self) -> str:
        return str(self.scope_combo.currentData() or "active")

    def _eligible_receptor_ids(self) -> list[int]:
        excluded = self._defined_ligand_ids if self.exclude_defined_checkbox.isChecked() else set()
        return sorted(
            receptor_id
            for receptor_id in self._receptors
            if receptor_id not in excluded
        )

    def _scope_receptor_ids(self) -> list[int]:
        mode = self._scope_mode()
        if mode == "selected":
            receptor_id = int(self._focused_receptor_id or 0)
            return [receptor_id] if receptor_id in set(self._eligible_receptor_ids()) else []
        if mode == "filtered":
            widget = self._catalog_receptor_widget()
            table = getattr(widget, "table", None) if widget is not None else None
            if table is None:
                return []
            return sorted({int(value) for value in table.all_filtered_ids() if int(value) > 0})
        return self._eligible_receptor_ids()

    def _update_scope_label(self, *_args) -> None:
        if not hasattr(self, "scope_label"):
            return
        count = len(self._scope_receptor_ids())
        hidden = len(self._defined_ligand_ids) if self.exclude_defined_checkbox.isChecked() else 0
        hidden_text = f" · {hidden} with defined ligands hidden" if hidden else ""
        self.scope_label.setText(
            f"Scope «{self._scope_mode()}»: {count} receptor(s){hidden_text}"
        )

    def _run_prediction(self) -> None:
        receptor_ids = self._scope_receptor_ids()
        if not receptor_ids:
            QMessageBox.information(
                self,
                "P2Rank",
                "The selected scope contains no receptors.",
            )
            return
        if not self._tool_ready:
            tool = self.runtime.pockets.tool_status()
            QMessageBox.information(
                self,
                "P2Rank is not installed",
                f"{tool.message}\n\nInstall it from Settings > External tools, then run again.",
            )
            self._open_tool_settings()
            return
        self._submit_prediction()

    def _submit_prediction(self) -> None:
        receptor_ids = self._scope_receptor_ids()
        if not receptor_ids:
            return
        # One profile for the whole run; plan_prediction still takes it per receptor, so
        # mixing profiles later is a UI change, not an API one.
        profile = str(self.profile_combo.currentData() or "default")
        profiles = {receptor_id: profile for receptor_id in receptor_ids}
        try:
            plan = self.runtime.pockets.plan_prediction(
                receptor_ids=receptor_ids,
                profiles=profiles,
            )
            if not plan.recalculate_receptor_ids:
                self._reload_sites()
                self.status_label.setText(
                    f"Reused existing P2Rank predictions for "
                    f"{len(plan.reused_receptor_ids)} receptor(s); profiles are unchanged."
                )
                return
            job_id = self.runtime.pockets.predict(
                receptor_ids=receptor_ids,
                profiles=profiles,
                threads=int(self.threads_spin.value()),
            )
        except Exception as exc:
            self._show_error("Could not submit P2Rank", exc)
            return
        normalized_job_id = str(job_id)
        self._pending_job_ids.add(normalized_job_id)
        self.run_button.setEnabled(False)
        reused_text = (
            f"; {len(plan.reused_receptor_ids)} existing result(s) reused"
            if plan.reused_receptor_ids
            else ""
        )
        self.status_label.setText(
            f"P2Rank job {normalized_job_id} submitted for "
            f"{len(plan.recalculate_receptor_ids)} receptor(s){reused_text}."
        )

    def _on_job_finished(self, job_id: str, status: str) -> None:
        normalized_job_id = str(job_id)
        if normalized_job_id not in self._pending_job_ids:
            return
        self._pending_job_ids.discard(normalized_job_id)
        self.run_button.setEnabled(bool(self._receptors) and not self._pending_job_ids)
        normalized_status = str(status or "").strip().lower()
        self.status_label.setText(
            "P2Rank prediction completed."
            if normalized_status == "completed"
            else f"P2Rank job {normalized_status}."
        )
        if normalized_status == "completed":
            self.refresh()
        self._reload_sites()

    def _show_error(self, title: str, exc: Exception) -> None:
        self.status_label.setText(str(exc))
        QMessageBox.critical(self, title, str(exc))


def register_pocket_detection_workspace(window) -> None:
    window.register_main_view(
        POCKET_DETECTION_VIEW_ID,
        "Pocket Detection",
        lambda: PocketDetectionWidget(runtime=window.runtime, parent=window.central_widget),
    )
    # Not a tab: the sites of the selected receptor belong under the Receptors table, in the
    # auxiliary zone (main_window._TOOL_AUX_VIEWS).
    window.register_main_view(
        POCKET_SITES_VIEW_ID,
        "Binding Sites",
        lambda: BindingSitesWidget(runtime=window.runtime, parent=window.central_widget),
    )


__all__ = [
    "POCKET_DETECTION_VIEW_ID",
    "POCKET_SITES_VIEW_ID",
    "BindingSitesWidget",
    "PocketDetectionWidget",
    "register_pocket_detection_workspace",
]
