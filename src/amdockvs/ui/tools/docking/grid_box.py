from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QColor, QBrush, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.models import BindingSite, MoleculeRecord
from amdockvs.ui.async_query import run_async
from amdockvs.ui.resources.icons import icon as load_icon

SITE_COLORS = [
    "#F29CB2",
    "#9FC5E8",
    "#B6D7A8",
    "#FFD966",
    "#C9B6E4",
    "#F6B26B",
    "#A2D9CE",
    "#D5A6BD",
    "#A4C2F4",
    "#B4E197",
    "#FFE599",
    "#D9C2F0",
]

ACTIVE_SITE_ROLE = Qt.UserRole + 1
SITE_COLOR_ROLE = Qt.UserRole + 2

GRID_BOX_LEGEND = (
    "<b>Grid box legend</b><br>"
    "<b>*</b> Unsaved working box<br>"
    "<b>Colored name</b> Box color in the 3D view<br>"
    "<b>Green dot</b> Active docking site<br>"
    "<b>Source</b> Method used to define the site"
)


class _HoverIcon(QWidget):
    """Passive, theme-aware icon: hover tooltip only, with no button semantics."""

    def __init__(self, icon: QIcon, *, tooltip: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._icon = icon
        self.setFixedSize(QSize(22, 22))
        self.setToolTip(tooltip)
        self.setAccessibleName("Grid box legend")

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        self._icon.paint(
            painter,
            self.rect().adjusted(2, 2, -2, -2),
            Qt.AlignCenter,
            QIcon.Normal,
            QIcon.Off,
        )


def _active_indicator_icon() -> QIcon:
    """Match the compact green-dot indicator used for open catalog views."""
    size = 15
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#3fbf73"))
    painter.drawEllipse(2, (size - 8) // 2, 8, 8)
    painter.end()
    return QIcon(pixmap)


@dataclass
class GridBoxState:
    center_x: float = 0.0
    center_y: float = 0.0
    center_z: float = 0.0
    size_x: float = 22.0
    size_y: float = 22.0
    size_z: float = 22.0
    show_faces: bool = True
    show_edges: bool = True
    edge_width: float = 2.0
    opacity: float = 0.35
    source_site_id: int | None = None
    dirty: bool = False


class GridBoxSettingDockWidget(QWidget):
    # Emitted (with the receptor's molecule id) when a binding site is saved or made active —
    # i.e. whenever the active box geometry may have changed. Lets the docking view refresh
    # things that depend on the box, like the in-box flexible-residue candidates.
    binding_site_changed = Signal(int)

    def __init__(self, *, runtime, parent: QWidget | None = None):
        super().__init__(parent)
        self.runtime = runtime
        self._current_molecule: MoleculeRecord | None = None
        self._sites: list[BindingSite] = []
        self._working = GridBoxState()
        self._auto_preview_enabled = False
        self._suspend_editor = False
        self._force_preview_once = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        receptor_row = QHBoxLayout()
        receptor_row.setContentsMargins(0, 0, 0, 0)
        receptor_row.setSpacing(4)
        self.receptor_label = QLabel("No receptor selected", self)
        self.receptor_label.setWordWrap(True)
        receptor_row.addWidget(self.receptor_label)
        receptor_row.addStretch(1)
        self.legend_icon = _HoverIcon(
            load_icon("info.svg"),
            tooltip=GRID_BOX_LEGEND,
            parent=self,
        )
        receptor_row.addWidget(self.legend_icon)
        layout.addLayout(receptor_row)

        self.site_tree = QTreeWidget(self)
        self.site_tree.setHeaderLabels(["", "Name", "Source"])
        self.site_tree.setRootIsDecorated(False)
        self.site_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        header = self.site_tree.header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(15)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.site_tree.setColumnWidth(0, 15)
        self.site_tree.setUniformRowHeights(True)
        self.site_tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        self.site_tree.currentItemChanged.connect(self._on_current_item_changed)
        self.site_tree.setMinimumHeight(10)
        layout.addWidget(self.site_tree, 1)

        self.show_working_box = QCheckBox("Show working box", self)
        self.show_working_box.setChecked(True)
        self.show_working_box.toggled.connect(lambda _checked: self._refresh_preview(force=True))
        layout.addWidget(self.show_working_box)

        editor_box = QGroupBox("Working Box", self)
        editor_layout = QFormLayout(editor_box)

        editor_layout.addRow(QLabel("Center"))
        center_layout = QGridLayout()
        self.cx = self._coord_spinbox()
        self.cy = self._coord_spinbox()
        self.cz = self._coord_spinbox()
        center_layout.addWidget(QLabel("X", editor_box), 0, 0)
        center_layout.addWidget(self.cx, 0, 1)
        center_layout.addWidget(QLabel("Y", editor_box), 0, 2)
        center_layout.addWidget(self.cy, 0, 3)
        center_layout.addWidget(QLabel("Z", editor_box), 0, 4)
        center_layout.addWidget(self.cz, 0, 5)
        editor_layout.addRow(center_layout)

        editor_layout.addRow(QLabel("Size"))
        size_layout = QGridLayout()
        self.sx = self._size_spinbox()
        self.sy = self._size_spinbox()
        self.sz = self._size_spinbox()
        size_layout.addWidget(QLabel("X", editor_box), 0, 0)
        size_layout.addWidget(self.sx, 0, 1)
        size_layout.addWidget(QLabel("Y", editor_box), 0, 2)
        size_layout.addWidget(self.sy, 0, 3)
        size_layout.addWidget(QLabel("Z", editor_box), 0, 4)
        size_layout.addWidget(self.sz, 0, 5)
        editor_layout.addRow(size_layout)

        # Radio group (not two checkboxes): a box with neither faces nor edges is invisible, so
        # the style is always exactly one of these three — the box can never disappear.
        self.style_both = QRadioButton("Faces + Edges", editor_box)
        self.style_faces = QRadioButton("Faces", editor_box)
        self.style_edges = QRadioButton("Edges", editor_box)
        self.style_both.setToolTip(
            "Display style for ALL visible boxes. Faces + Edges is the default; a box with neither would be invisible.")
        self.style_both.setChecked(True)
        self.style_group = QButtonGroup(self)
        for button in (self.style_both, self.style_faces, self.style_edges):
            self.style_group.addButton(button)
        self.style_group.buttonToggled.connect(lambda *_args: self._on_style_changed())
        style_grid = QGridLayout()
        style_grid.addWidget(self.style_both, 0, 0, 1, 4)
        style_grid.addWidget(self.style_faces, 1, 0)
        style_grid.addWidget(self.style_edges, 2, 0)
        style_grid.setColumnStretch(0, 1)
        editor_layout.addRow(QLabel("Style"))

        self.opacity = QDoubleSpinBox(editor_box)
        self.opacity.setRange(0.1, 1.0)
        self.opacity.setDecimals(2)
        self.opacity.setSingleStep(0.05)
        self.opacity.setValue(0.35)
        self.opacity.valueChanged.connect(self._on_style_changed)
        style_grid.addWidget(QLabel("Opacity"), 1, 2)
        style_grid.addWidget(self.opacity, 1, 3)

        self.edge_width = QDoubleSpinBox(editor_box)
        self.edge_width.setRange(1.0, 10.0)
        self.edge_width.setDecimals(1)
        self.edge_width.setSingleStep(0.5)
        self.edge_width.setValue(2.0)
        self.edge_width.valueChanged.connect(self._on_style_changed)
        style_grid.addWidget(QLabel("Edge Width"), 2, 2)
        style_grid.addWidget(self.edge_width, 2, 3)

        for widget in (self.cx, self.cy, self.cz, self.sx, self.sy, self.sz):
            widget.valueChanged.connect(self._on_geometry_changed)

        editor_layout.addRow(style_grid)
        layout.addWidget(editor_box)

        buttons = QGridLayout()
        self.load_active_button = QPushButton("Load Active", self)
        self.load_active_button.clicked.connect(self._load_active_site)
        buttons.addWidget(self.load_active_button, 0, 0)
        self.new_button = QPushButton("New", self)
        self.new_button.clicked.connect(self._new_working_box)
        buttons.addWidget(self.new_button, 0, 1)
        self.save_button = QPushButton("Save", self)
        self.save_button.clicked.connect(self._save_selected_site)
        buttons.addWidget(self.save_button, 0, 2)
        self.save_as_button = QPushButton("Save As New", self)
        self.save_as_button.clicked.connect(self._save_new_site)
        buttons.addWidget(self.save_as_button, 1, 0)
        self.activate_button = QPushButton("Set Active", self)
        self.activate_button.clicked.connect(self._set_active_site)
        buttons.addWidget(self.activate_button, 1, 1)
        self.clear_button = QPushButton("Clear Overlay", self)
        self.clear_button.clicked.connect(self._clear_preview)
        buttons.addWidget(self.clear_button, 1, 2)
        self.auto_box_button = QPushButton("Auto box from ligand…", self)
        self.auto_box_button.setToolTip(
            "Center + cubic box size derived from a reference ligand's radius of gyration."
        )
        self.auto_box_button.clicked.connect(self._auto_box_from_ligand)
        buttons.addWidget(self.auto_box_button, 2, 0, 1, 3)
        self.box_from_selection_button = QPushButton("Box from residue/selection…", self)
        self.box_from_selection_button.setToolTip(
            "Center the box on a surface pseudo-ligand over a PyMOL selection (residues), so the "
            "box hugs the pocket mouth instead of burying itself in the protein."
        )
        self.box_from_selection_button.clicked.connect(self._box_from_selection)
        buttons.addWidget(self.box_from_selection_button, 3, 0, 1, 3)
        # for button in (
        #     self.load_active_button,
        #     self.new_button,
        #     self.save_button,
        #     self.save_as_button,
        #     self.activate_button,
        #     self.clear_button,
        # ):
        #     buttons.addWidget(button)
        layout.addLayout(buttons)

        self._sync_enabled_state()
        self.hide()

    def _style_flags(self) -> tuple[bool, bool]:
        if self.style_faces.isChecked():
            return (True, False)
        if self.style_edges.isChecked():
            return (False, True)
        return (True, True)

    @staticmethod
    def _coord_spinbox() -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setRange(-1000.0, 1000.0)
        widget.setDecimals(2)
        widget.setSingleStep(0.25)
        widget.setAccelerated(True)
        return widget

    @staticmethod
    def _size_spinbox() -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setRange(10.0, 200.0)
        widget.setDecimals(1)
        widget.setSingleStep(0.5)
        widget.setValue(22.0)
        widget.setAccelerated(True)
        return widget

    def set_auto_preview_enabled(self, enabled: bool) -> None:
        self._auto_preview_enabled = bool(enabled)
        if not self._auto_preview_enabled:
            self._force_preview_once = False
            self._clear_preview()
            return
        if self.isVisible():
            self._refresh_preview(force=False)

    def set_molecule(self, molecule: MoleculeRecord | None) -> None:
        new_id = int(getattr(molecule, "id", 0) or 0) if molecule is not None else 0
        old_id = int(getattr(self._current_molecule, "id", 0) or 0) if self._current_molecule is not None else 0
        if new_id != old_id:
            # The temporary working box and its marker belong to the receptor that defined them —
            # discard both so they don't reappear on a different receptor.
            self._clear_pseudo_ligand()
            self._working = GridBoxState()
            self._sync_editor_from_working()
        self._current_molecule = molecule if molecule is not None and bool(
            getattr(molecule, "is_receptor", False)) else None
        self._reload_sites()

    def clear_molecule(self) -> None:
        self._clear_pseudo_ligand()
        self._current_molecule = None
        self._sites = []
        self.site_tree.clear()
        self.receptor_label.setText("No receptor selected")
        self._working = GridBoxState()
        self._sync_editor_from_working()
        self._sync_enabled_state()
        self._clear_preview()

    def focus_binding_site(self, molecule: MoleculeRecord | None, *, site_id: int | None,
                           ensure_selected: bool = True) -> None:
        self._force_preview_once = True
        self.set_molecule(molecule)
        if molecule is None or site_id is None:
            return
        for row in range(self.site_tree.topLevelItemCount()):
            item = self.site_tree.topLevelItem(row)
            payload = item.data(0, Qt.UserRole)
            if isinstance(payload, tuple) and payload[0] == "site" and int(payload[1] or 0) == int(site_id):
                self.site_tree.setCurrentItem(item)
                if ensure_selected:
                    item.setSelected(True)
                self._load_site_into_working(self._site_by_id(int(site_id)))
                self._sync_enabled_state()
                self._refresh_preview(force=True)
                return

    def _reload_sites(self) -> None:
        self.site_tree.blockSignals(True)
        self.site_tree.clear()
        self._sites = []
        molecule = self._current_molecule
        if molecule is None:
            self.receptor_label.setText("No receptor selected")
            self.site_tree.blockSignals(False)
            self._sync_enabled_state()
            self._clear_preview()
            return
        self.receptor_label.setText(f"Receptor: {molecule.name or f'#{int(molecule.id or 0)}'}")
        self._sites = self.runtime.docking.list_binding_sites(molecule_id=int(molecule.id or 0))
        self._populate_tree()
        self.site_tree.blockSignals(False)
        self._sync_enabled_state()
        if self.isVisible() and self._auto_preview_enabled:
            self._refresh_preview(force=False)
        else:
            self._clear_preview()

    def _populate_tree(self) -> None:
        active_id = int(getattr(self._current_molecule, "active_binding_site_id", 0) or 0)
        selected_site_item: QTreeWidgetItem | None = None
        for site in self._sites:
            item = self._make_site_item(site, active=(int(site.id or 0) == active_id))
            self.site_tree.addTopLevelItem(item)
            if int(site.id or 0) == active_id and selected_site_item is None:
                selected_site_item = item
        if self._working.dirty:
            self._ensure_working_item_present(select_if_created=False)
        if selected_site_item is not None:
            self.site_tree.setCurrentItem(selected_site_item)
            selected_site_item.setSelected(True)

    def _make_working_item(self) -> QTreeWidgetItem:
        if self._working.source_site_id is not None:
            state = f"temporary from #{int(self._working.source_site_id)}"
        else:
            state = "temporary"
        item = QTreeWidgetItem(["", "* Working Box", "manual"])
        item.setData(0, Qt.UserRole, ("working", None))
        self._apply_name_color(item, QColor("#FFFFFF"))
        item.setToolTip(1, f"Unsaved working box ({state}).")
        return item

    def _make_site_item(self, site: BindingSite, *, active: bool) -> QTreeWidgetItem:
        color = self._site_color(site)
        item = QTreeWidgetItem(
            [
                "",
                str(site.name or site.source or "site"),
                str(site.source or ""),
            ]
        )
        item.setData(0, Qt.UserRole, ("site", int(site.id or 0)))
        item.setData(0, ACTIVE_SITE_ROLE, active)
        if active:
            item.setIcon(0, _active_indicator_icon())
            item.setToolTip(0, "Active docking site")
        self._apply_name_color(item, color)
        status = "Active docking site" if active else "Saved binding site"
        item.setToolTip(1, f"{status} #{int(site.id or 0)}. Color: {color.name().upper()}.")
        return item

    @staticmethod
    def _apply_name_color(item: QTreeWidgetItem, color: QColor) -> None:
        item.setData(1, SITE_COLOR_ROLE, color.name())
        item.setBackground(1, QBrush(color))
        item.setForeground(1, QBrush(QColor("#202124")))

    def active_box_geometry(self) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
        """(center, size) of the receptor's active binding-site box, or None if unset — used to
        orient the camera onto the box face when a receptor is loaded."""
        molecule = self._current_molecule
        if molecule is None:
            return None
        active_id = int(getattr(molecule, "active_binding_site_id", 0) or 0)
        site = self._site_by_id(active_id) if active_id > 0 else None
        if site is None:
            return None
        values = (site.center_x, site.center_y, site.center_z, site.size_x, site.size_y, site.size_z)
        if any(value is None for value in values):
            return None
        return (
            (float(site.center_x), float(site.center_y), float(site.center_z)),
            (float(site.size_x), float(site.size_y), float(site.size_z)),
        )

    def _site_by_id(self, site_id: int) -> BindingSite | None:
        for site in self._sites:
            if int(site.id or 0) == int(site_id):
                return site
        return None

    @staticmethod
    def _site_color(site: BindingSite) -> QColor:
        try:
            site_id = max(1, int(site.id or 1))
        except Exception:
            site_id = 1
        return QColor(SITE_COLORS[(site_id - 1) % len(SITE_COLORS)])

    def _selected_sites(self) -> list[BindingSite]:
        rows: list[BindingSite] = []
        for item in self.site_tree.selectedItems():
            payload = item.data(0, Qt.UserRole)
            if isinstance(payload, tuple) and payload[0] == "site":
                site = self._site_by_id(int(payload[1] or 0))
                if site is not None:
                    rows.append(site)
        return rows

    def _selected_site(self) -> BindingSite | None:
        current = self.site_tree.currentItem()
        if current is None:
            return None
        payload = current.data(0, Qt.UserRole)
        if isinstance(payload, tuple) and payload[0] == "site":
            return self._site_by_id(int(payload[1] or 0))
        return None

    def _load_site_into_working(self, site: BindingSite | None) -> None:
        if site is None:
            return
        self._working.center_x = float(site.center_x or 0.0)
        self._working.center_y = float(site.center_y or 0.0)
        self._working.center_z = float(site.center_z or 0.0)
        self._working.size_x = float(site.size_x or 20.0)
        self._working.size_y = float(site.size_y or 20.0)
        self._working.size_z = float(site.size_z or 20.0)
        self._working.source_site_id = int(site.id or 0)
        self._working.dirty = False
        self._sync_editor_from_working()

    def _sync_editor_from_working(self) -> None:
        self._suspend_editor = True
        try:
            self.cx.setValue(self._working.center_x)
            self.cy.setValue(self._working.center_y)
            self.cz.setValue(self._working.center_z)
            self.sx.setValue(self._working.size_x)
            self.sy.setValue(self._working.size_y)
            self.sz.setValue(self._working.size_z)
            # Style is a global display choice — deliberately NOT synced from the box, so loading
            # a site doesn't reset the user's Faces/Edges/F+E pick.
            self.edge_width.setValue(float(self._working.edge_width))
            self.opacity.setValue(float(self._working.opacity))
        finally:
            self._suspend_editor = False

    def _auto_box_from_ligand(self) -> None:
        # ponytail: cap the picker — a VS run may have 100k+ ligands; a dropdown of all is useless.
        cap = 500
        choices: list[tuple[str, int]] = []
        try:
            scope = self.runtime.molecules.select(role="ligand")
            for rec in self.runtime.molecules.stream(scope):
                mid = int(getattr(rec, "id", 0) or 0)
                if not mid:
                    continue
                choices.append((f"{getattr(rec, 'name', '') or 'ligand'} (#{mid})", mid))
                if len(choices) >= cap:
                    break
        except Exception as exc:
            QMessageBox.warning(self, "Auto box", f"Could not list ligands: {exc}")
            return
        if not choices:
            QMessageBox.information(self, "Auto box", "No ligands available in this project.")
            return
        label, ok = QInputDialog.getItem(
            self, "Auto box from ligand", "Reference ligand:", [c[0] for c in choices], 0, False
        )
        if not ok:
            return
        ligand_id = dict(choices)[label]
        run_async(
            lambda: self.runtime.docking.suggest_box_from_ligand(ligand_id=ligand_id),
            self._apply_auto_box,
            on_error=lambda exc: QMessageBox.warning(self, "Auto box", str(exc)),
            busy=self,
        )

    def _apply_auto_box(self, box: dict) -> None:
        cx, cy, cz = box["center"]
        sx, sy, sz = box["size"]
        self._working.center_x, self._working.center_y, self._working.center_z = cx, cy, cz
        self._working.size_x, self._working.size_y, self._working.size_z = sx, sy, sz
        self._working.dirty = True
        self._sync_editor_from_working()
        self._ensure_working_item_present(select_if_created=True)
        self._refresh_preview(force=True)

    def _box_from_selection(self) -> None:
        from amdockvs.docking.pockets import pseudo_pocket_box
        from amdockvs.docking.residues import pseudo_ligand_box

        molecule = self._current_molecule
        dock = getattr(self.window(), "pymol_dock", None)
        cmd = getattr(dock, "cmd", None) if dock is not None else None
        if molecule is None or cmd is None:
            QMessageBox.information(self, "Box from selection", "Load a receptor in PyMOL first.")
            return
        selection, ok = QInputDialog.getText(
            self,
            "Box from residue/selection",
            "PyMOL selection of receptor residue(s) (e.g. 'sele', 'chain A and resi 45+50'):",
            text="sele",
        )
        selection = str(selection or "").strip()
        if not ok or not selection:
            return
        receptor_obj = f"receptor_{int(molecule.id or 0)}"
        try:
            # iterate_state (not get_coords) so we also pull per-atom vdW radii for the pocket grid.
            receptor_atoms: list[tuple[float, float, float, float]] = []
            cmd.iterate_state(
                1, receptor_obj, "receptor_atoms.append((x, y, z, vdw))",
                space={"receptor_atoms": receptor_atoms},
            )
            selection_coords = cmd.get_coords(f"({selection})")
        except Exception as exc:
            QMessageBox.warning(self, "Box from selection", f"Could not read coordinates: {exc}")
            return
        if selection_coords is None or len(selection_coords) == 0:
            QMessageBox.information(self, "Box from selection", f"Selection '{selection}' matched no atoms.")
            return
        receptor_xyz = [(a[0], a[1], a[2]) for a in receptor_atoms]
        receptor_vdw = [a[3] for a in receptor_atoms]
        selection_list = [tuple(float(v) for v in p) for p in selection_coords]

        def compute() -> tuple[dict, str]:
            # LIGSITE pocket detection when possible; fall back to the bounded-push heuristic if
            # scipy is missing or no accessible pocket is found near the selection. Runs off the
            # GUI thread — a large receptor's voxel grid can take ~1s.
            try:
                box = pseudo_pocket_box(receptor_xyz, receptor_vdw, selection_list)
                return box, ("accessible surface (no deep pocket)" if box.get("fallback") else "detected pocket")
            except Exception:
                return pseudo_ligand_box(receptor_xyz, selection_list), "centroid push (fallback)"

        run_async(
            compute,
            lambda result: self._apply_box_from_selection(cmd, result, len(selection_list)),
            on_error=lambda exc: QMessageBox.warning(self, "Box from selection", str(exc)),
            busy=self,
        )

    def _apply_box_from_selection(self, cmd, result: tuple[dict, str], n_selected: int) -> None:
        box, method = result
        # Draw the opaque blob/center FIRST, then the box (below via _refresh_preview): the box's
        # translucent faces must be drawn LAST or they write depth and hide the spheres behind
        # them. The marker is amdock_pseudolig*, which _refresh_preview does NOT clear.
        self._draw_pseudo_ligand(cmd, box["points"], box["center"])
        # Box-from-selection moves ONLY the center; the box size stays at its current value
        # (default 22) — a selection snap must not resize the box.
        cx, cy, cz = box["center"]
        self._working.center_x, self._working.center_y, self._working.center_z = cx, cy, cz
        self._working.dirty = True
        self._sync_editor_from_working()
        self._ensure_working_item_present(select_if_created=True)
        self._refresh_preview(force=True)
        moved = box.get("moved")
        moved_line = f"\nSnapped {moved:.1f} A from the selection to the pocket." if moved is not None else ""
        QMessageBox.information(
            self,
            "Box from selection",
            f"{n_selected} selected atom(s) — method: {method}.\n"
            f"Box center: ({cx:.2f}, {cy:.2f}, {cz:.2f})  size: {self._working.size_x:.1f} A cube (unchanged).{moved_line}\n"
            f"Blue blob = detected pocket; orange sphere = box center (cavity-volume centroid).",
        )

    def _draw_pseudo_ligand(self, cmd, points, center) -> None:
        # NOT amdock_grid_* on purpose: these must survive _clear_preview so they stay visible
        # while the user adjusts the box. Cleared on receptor change / panel hide.
        blob = "amdock_pseudolig"
        center_obj = "amdock_pseudolig_center"
        try:
            # Real-time depth-sorted transparency, so the translucent box faces don't cull the
            # opaque blob/center inside them (the reason the spheres vanished with Faces on).
            cmd.set("transparency_mode", 1)
            cmd.delete(blob)
            cmd.delete(center_obj)
            for index, point in enumerate(points):
                cmd.pseudoatom(blob, pos=[float(point[0]), float(point[1]), float(point[2])], name=f"P{index}", vdw=0.7)
            cmd.show_as("spheres", blob)
            cmd.set("sphere_scale", 0.5, blob)
            cmd.color("marine", blob)  # opaque: solid geometry survives the transparent faces
            # The box center from LIGSITE is the cavity-volume centroid, which does NOT coincide
            # with the blob's geometric center — so show it explicitly.
            cmd.pseudoatom(center_obj, pos=[float(center[0]), float(center[1]), float(center[2])], vdw=1.4)
            cmd.show_as("spheres", center_obj)
            cmd.set("sphere_scale", 0.6, center_obj)
            cmd.color("orange", center_obj)
        except Exception:
            return

    def _clear_pseudo_ligand(self) -> None:
        dock = getattr(self.window(), "pymol_dock", None)
        cmd = getattr(dock, "cmd", None) if dock is not None else None
        if cmd is None:
            return
        try:
            cmd.delete("amdock_pseudolig")
            cmd.delete("amdock_pseudolig_center")
        except Exception:
            return

    def _on_tree_selection_changed(self) -> None:
        self._sync_enabled_state()
        self._refresh_preview(force=True)

    def _on_current_item_changed(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:
        del previous
        if current is None:
            return
        payload = current.data(0, Qt.UserRole)
        if isinstance(payload, tuple) and payload[0] == "site":
            # Don't clobber unsaved edits: if the working box is dirty, just preview the clicked
            # site as reference and keep the temp geometry. Discard edits via New or Save.
            if not self._working.dirty:
                self._load_site_into_working(self._site_by_id(int(payload[1] or 0)))
        elif isinstance(payload, tuple) and payload[0] == "working":
            self._sync_editor_from_working()
        self._refresh_preview(force=True)

    def _on_geometry_changed(self) -> None:
        if self._suspend_editor:
            return
        self._working.center_x = float(self.cx.value())
        self._working.center_y = float(self.cy.value())
        self._working.center_z = float(self.cz.value())
        self._working.size_x = float(self.sx.value())
        self._working.size_y = float(self.sy.value())
        self._working.size_z = float(self.sz.value())
        self._working.dirty = True
        self._ensure_working_item_present(select_if_created=True)
        self._refresh_preview(force=True)

    def _on_style_changed(self) -> None:
        if self._suspend_editor:
            return
        self._working.show_faces, self._working.show_edges = self._style_flags()
        self._working.edge_width = float(self.edge_width.value())
        self._working.opacity = float(self.opacity.value())
        self._refresh_preview(force=True)

    def _ensure_working_item_present(self, *, select_if_created: bool) -> None:
        if not self._working.dirty:
            self._remove_working_item()
            return
        for row in range(self.site_tree.topLevelItemCount()):
            item = self.site_tree.topLevelItem(row)
            payload = item.data(0, Qt.UserRole)
            if isinstance(payload, tuple) and payload[0] == "working":
                if self._working.source_site_id is not None:
                    state = f"temporary from #{int(self._working.source_site_id)}"
                else:
                    state = "temporary"
                item.setToolTip(1, f"Unsaved working box ({state}).")
                if select_if_created:
                    self.site_tree.setCurrentItem(item)
                    item.setSelected(True)
                return
        working_item = self._make_working_item()
        self.site_tree.addTopLevelItem(working_item)
        if select_if_created:
            self.site_tree.setCurrentItem(working_item)
            working_item.setSelected(True)

    def _remove_working_item(self) -> None:
        for row in range(self.site_tree.topLevelItemCount() - 1, -1, -1):
            item = self.site_tree.topLevelItem(row)
            payload = item.data(0, Qt.UserRole)
            if isinstance(payload, tuple) and payload[0] == "working":
                self.site_tree.takeTopLevelItem(row)
                return

    @staticmethod
    def _box_points(center_x: float, center_y: float, center_z: float, size_x: float, size_y: float, size_z: float):
        hx = float(size_x) / 2.0
        hy = float(size_y) / 2.0
        hz = float(size_z) / 2.0
        return (
            (float(center_x) - hx, float(center_y) - hy, float(center_z) - hz),
            (float(center_x) + hx, float(center_y) + hy, float(center_z) + hz),
        )

    def _refresh_preview(self, *, force: bool) -> None:
        dock = getattr(self.window(), "pymol_dock", None)
        cmd = getattr(dock, "cmd", None) if dock is not None else None
        if cmd is None:
            return
        if not self.isVisible():
            self._clear_preview()
            return
        should_render = bool(force or self._auto_preview_enabled or self._force_preview_once)
        if not should_render:
            self._clear_preview()
            return
        self._force_preview_once = False
        self._clear_preview()

        # Style (Faces / Edges / F+E) is a global display choice applied to EVERY visible box,
        # not just the working one.
        show_faces, show_edges = self._style_flags()
        selected_sites = self._selected_sites()
        drawn_indices = set()
        for site in selected_sites:
            self._draw_reference_site(
                cmd, site, active=(site == self._selected_site()),
                name=f"amdock_grid_bs_{int(site.id or 0)}",
                show_faces=show_faces, show_edges=show_edges,
            )
            drawn_indices.add(int(site.id or 0))
        # The temporary/working box persists (drawn whenever checked) regardless of tree
        # selection or clicks in the viewport — only receptor change / New / Save clears it.
        # When it's an edit of a saved site, also draw that origin box faded so it's obvious
        # what's changing.
        if self.show_working_box.isChecked() and self._working.dirty:
            source_id = self._working.source_site_id
            if source_id is not None and int(source_id) not in drawn_indices:
                source_site = self._site_by_id(int(source_id))
                if source_site is not None:
                    self._draw_reference_site(
                        cmd, source_site, active=False, name="amdock_grid_origin",
                        show_faces=show_faces, show_edges=show_edges,
                    )
            self._draw_working(cmd, show_faces=show_faces, show_edges=show_edges)

    def _draw_reference_site(
            self, cmd, site: BindingSite, *, active: bool, name: str,
            show_faces: bool = True, show_edges: bool = True,
    ) -> None:
        if None in (site.center_x, site.center_y, site.center_z, site.size_x, site.size_y, site.size_z):
            return
        points = self._box_points(
            float(site.center_x), float(site.center_y), float(site.center_z),
            float(site.size_x), float(site.size_y), float(site.size_z),
        )
        qcolor = self._site_color(site)
        color = qcolor.getRgbF()[:3]
        edge_qcolor = qcolor.lighter(125) if active else qcolor.darker(105)
        edge_color = edge_qcolor.getRgbF()[:3]
        try:
            cmd.draw_box(
                points=points,
                show_face=bool(show_faces),
                face_color_x=color,
                face_color_y=color,
                face_color_z=color,
                show_edge=bool(show_edges),
                edge_style="line",
                edge_color=edge_color,
                edge_width=2.5 if active else 1.8,
                face_opacity=0.22 if active else 0.14,
                obj_name=name,
            )
        except Exception:
            return

    def _draw_working(self, cmd, *, show_faces: bool = True, show_edges: bool = True) -> None:
        points = self._box_points(
            self._working.center_x,
            self._working.center_y,
            self._working.center_z,
            self._working.size_x,
            self._working.size_y,
            self._working.size_z,
        )
        try:
            cmd.draw_box(
                points=points,
                show_face=bool(show_faces),
                face_color_x=(1.0, 1.0, 1.0),
                face_color_y=(1.0, 1.0, 1.0),
                face_color_z=(1.0, 1.0, 1.0),
                show_edge=bool(show_edges),
                edge_style="line",
                edge_color=(1.0, 1.0, 1.0),
                edge_width=float(self._working.edge_width),
                face_opacity=float(self._working.opacity),
                obj_name="amdock_grid_working",
            )
        except Exception:
            return

    def _load_active_site(self) -> None:
        molecule = self._current_molecule
        if molecule is None:
            return
        active_id = int(getattr(molecule, "active_binding_site_id", 0) or 0)
        if active_id <= 0:
            return
        self.focus_binding_site(molecule, site_id=active_id, ensure_selected=True)

    def _new_working_box(self) -> None:
        show_faces, show_edges = self._style_flags()
        self._working = GridBoxState(
            center_x=0.0,
            center_y=0.0,
            center_z=0.0,
            size_x=22.0,
            size_y=22.0,
            size_z=22.0,
            show_faces=show_faces,
            show_edges=show_edges,
            edge_width=float(self.edge_width.value()),
            opacity=float(self.opacity.value()),
            source_site_id=None,
            dirty=True,
        )
        self._ensure_working_item_present(select_if_created=True)
        self._sync_editor_from_working()
        self._refresh_preview(force=True)

    def _save_selected_site(self) -> None:
        molecule = self._current_molecule
        site = self._selected_site()
        if site is None and self._working.source_site_id is not None:
            site = self._site_by_id(int(self._working.source_site_id))
        if molecule is None or site is None:
            return
        try:
            self.runtime.docking.save_binding_site(
                molecule_id=int(molecule.id or 0),
                binding_site_id=int(site.id or 0),
                name=str(site.name or f"Site {int(site.id or 0)}"),
                source=str(site.source or "manual"),
                source_ref=str(site.source_ref or ""),
                center=(self.cx.value(), self.cy.value(), self.cz.value()),
                size=(self.sx.value(), self.sy.value(), self.sz.value()),
                set_active=int(getattr(molecule, "active_binding_site_id", 0) or 0) == int(site.id or 0),
            )
        except Exception as exc:
            QMessageBox.critical(self, "Save Binding Site", str(exc))
            return
        self._working.dirty = False
        self._working.source_site_id = int(site.id or 0)
        self._reload_sites()
        self.focus_binding_site(molecule, site_id=int(site.id or 0), ensure_selected=True)
        self.binding_site_changed.emit(int(molecule.id or 0))

    def _save_new_site(self) -> None:
        molecule = self._current_molecule
        if molecule is None:
            return
        try:
            site = self.runtime.docking.save_binding_site(
                molecule_id=int(molecule.id or 0),
                name="Manual Site",
                source="manual",
                center=(self.cx.value(), self.cy.value(), self.cz.value()),
                size=(self.sx.value(), self.sy.value(), self.sz.value()),
                set_active=False,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Save Binding Site", str(exc))
            return
        self._working.dirty = False
        self._working.source_site_id = int(site.id or 0)
        self._reload_sites()
        self.focus_binding_site(molecule, site_id=int(site.id or 0), ensure_selected=True)
        self.binding_site_changed.emit(int(molecule.id or 0))

    def _set_active_site(self) -> None:
        molecule = self._current_molecule
        site = self._selected_site()
        if molecule is None or site is None:
            return
        try:
            self.runtime.docking.set_active_binding_site(
                molecule_id=int(molecule.id or 0),
                binding_site_id=int(site.id or 0),
            )
            molecule.active_binding_site_id = int(site.id or 0)
        except Exception as exc:
            QMessageBox.critical(self, "Set Active Binding Site", str(exc))
            return
        self._reload_sites()
        self.focus_binding_site(molecule, site_id=int(site.id or 0), ensure_selected=True)
        self.binding_site_changed.emit(int(molecule.id or 0))

    def _clear_preview(self) -> None:
        dock = getattr(self.window(), "pymol_dock", None)
        cmd = getattr(dock, "cmd", None) if dock is not None else None
        if cmd is None:
            return
        try:
            for name in cmd.get_names("all"):
                if str(name).startswith("amdock_grid_"):
                    cmd.delete(str(name))
        except Exception:
            return

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._auto_preview_enabled or self._force_preview_once:
            self._refresh_preview(force=False)

    def hideEvent(self, event) -> None:
        self._clear_preview()
        self._clear_pseudo_ligand()
        super().hideEvent(event)

    def _sync_enabled_state(self) -> None:
        has_receptor = self._current_molecule is not None
        has_selected_site = self._selected_site() is not None
        can_save_existing = has_selected_site or self._working.source_site_id is not None
        self.site_tree.setEnabled(has_receptor)
        self.show_working_box.setEnabled(has_receptor)
        self.load_active_button.setEnabled(has_receptor)
        self.new_button.setEnabled(has_receptor)
        self.save_button.setEnabled(can_save_existing)
        self.save_as_button.setEnabled(has_receptor)
        self.activate_button.setEnabled(has_selected_site)
        self.box_from_selection_button.setEnabled(has_receptor)
        self.clear_button.setEnabled(True)


__all__ = ["GridBoxSettingDockWidget"]
