from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QColor, QBrush, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
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
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget, QTabWidget,
)
from ms_components.theme import color, contrast_ratio

from amdockvs.core.configuration import app_config
from amdockvs.models import BindingSite, MoleculeRecord
from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.view_ids import POCKET_DETECTION_VIEW_ID

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
    "<b>Source</b> Method used to define the site<br>"
    "<b>Blue spheres</b> Pocket found from a selection; <b>orange</b> = box center"
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


# PyMOL selection that mirrors the typed one while the selection editor is open.
_SELECTION_PREVIEW = "amdock_box_selection"


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
        self.site_tree.setMinimumHeight(60)
        # The unsaved working box goes away with Delete or the row's context menu.
        self.site_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.site_tree.customContextMenuRequested.connect(self._show_tree_menu)
        discard = QShortcut(QKeySequence.StandardKey.Delete, self.site_tree, self._discard_working_box)
        discard.setContext(Qt.ShortcutContext.WidgetShortcut)
        layout.addWidget(self.site_tree, 1)

        # Every way to start a box is one visible click; Predict hands over to Pocket Detection,
        # whose sites come back to this list.
        create_row = QHBoxLayout()
        create_row.setSpacing(0)
        self.new_button = self._action_button(
            "New", "file-plus.svg", self._new_working_box, "A default 22 Å box to place by hand.")
        self.ligand_button = self._action_button(
            "Ligand", "ligands.svg", self._auto_box_from_ligand,
            "Center + cubic box size derived from a reference ligand's radius of gyration.")
        self.selection_button = self._action_button(
            "Selection", "pymol.svg", self._toggle_selection_editor,
            "Center the box on the pocket next to a PyMOL selection of receptor residues or "
            "metals, so it hugs the pocket mouth instead of burying itself in the protein.")
        self.selection_button.setCheckable(True)
        self.predict_button = self._action_button(
            "Predict…", "binding_site.svg", self._open_pocket_detection,
            "Open Pocket Detection to predict this receptor's pockets.")
        for button in (self.new_button, self.ligand_button, self.selection_button, self.predict_button):
            create_row.addWidget(button)
        create_row.addStretch(1)
        layout.addLayout(create_row)
        # ponytail: PyMOL emits nothing when its selections change, so poll while shown; a
        # selection signal in ms_components' PyMOL widget would replace this timer.
        self._selection_poll = QTimer(self)
        self._selection_poll.setInterval(500)
        self._selection_poll.timeout.connect(self._sync_selection_button)

        # Selection editor: replaces the save row while open. The selection shows in PyMOL as
        # it is typed; green = receptor residues/metals only, orange = anything else.
        self.selection_row = QWidget(self)
        selection_layout = QHBoxLayout(self.selection_row)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_edit = QLineEdit("sele", self.selection_row)
        self.selection_edit.setPlaceholderText("e.g. chain A and resi 45+50")
        self.selection_edit.textChanged.connect(self._preview_selection)
        self.selection_edit.returnPressed.connect(self._apply_selection)
        self.selection_apply = QPushButton("Build box", self.selection_row)
        self.selection_apply.clicked.connect(self._apply_selection)
        selection_layout.addWidget(self.selection_edit, 1)
        selection_layout.addWidget(self.selection_apply)
        cancel = QShortcut(QKeySequence.StandardKey.Cancel, self.selection_row, self._close_selection_editor)
        cancel.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.selection_row.hide()
        layout.addWidget(self.selection_row)

        self.save_row = QWidget(self)
        save_layout = QHBoxLayout(self.save_row)
        save_layout.setContentsMargins(0, 0, 0, 0)
        self.save_button = QPushButton("Save", self.save_row)
        self.save_button.clicked.connect(self._save_selected_site)
        self.save_as_button = QPushButton("Save as new", self.save_row)
        self.save_as_button.clicked.connect(self._save_new_site)
        self.activate_button = QPushButton("Set Active", self.save_row)
        self.activate_button.clicked.connect(self._set_active_site)
        layout.addWidget(self.save_row)
        # No side padding (the theme's 14 px): in a narrow dock it ate the text, not the frame.
        for row in (self.selection_row, self.save_row):
            row.setStyleSheet("QPushButton { padding: 5px 0px; }")
        # Spare width follows text length, so "Save" never takes room "Save as new" needs.
        for button in (self.save_button, self.save_as_button, self.activate_button):
            save_layout.addWidget(button, button.sizeHint().width())

        self.show_working_box = QCheckBox("Show working box", self)
        self.show_working_box.setChecked(True)
        self.show_working_box.toggled.connect(lambda _checked: self._refresh_preview(force=True))
        layout.addWidget(self.show_working_box)


        self.box_settings = QTabWidget()

        editor_tab = QWidget(self.box_settings)
        self.box_settings.addTab(editor_tab, "Box")

        editor_layout = QFormLayout(editor_tab)
        # editor_layout.setContentsMargins(5, 2, 5, 0)

        editor_layout.addRow(QLabel("Center"))
        center_layout = QGridLayout()
        self.cx = self._coord_spinbox()
        self.cy = self._coord_spinbox()
        self.cz = self._coord_spinbox()
        center_layout.addWidget(QLabel("X", editor_tab), 0, 0)
        center_layout.addWidget(self.cx, 0, 1)
        center_layout.addWidget(QLabel("Y", editor_tab), 0, 2)
        center_layout.addWidget(self.cy, 0, 3)
        center_layout.addWidget(QLabel("Z", editor_tab), 0, 4)
        center_layout.addWidget(self.cz, 0, 5)
        editor_layout.addRow(center_layout)

        editor_layout.addRow(QLabel("Size"))
        size_layout = QGridLayout()
        self.sx = self._size_spinbox()
        self.sy = self._size_spinbox()
        self.sz = self._size_spinbox()
        size_layout.addWidget(QLabel("X", editor_tab), 0, 0)
        size_layout.addWidget(self.sx, 0, 1)
        size_layout.addWidget(QLabel("Y", editor_tab), 0, 2)
        size_layout.addWidget(self.sy, 0, 3)
        size_layout.addWidget(QLabel("Z", editor_tab), 0, 4)
        size_layout.addWidget(self.sz, 0, 5)
        editor_layout.addRow(size_layout)

        style_tab = QWidget(self.box_settings)
        style_layout = QGridLayout(style_tab)
        self.box_settings.addTab(style_tab, "Style")

        # Radio group (not two checkboxes): a box with neither faces nor edges is invisible, so
        # the style is always exactly one of these three — the box can never disappear.
        self.style_both = QRadioButton("Faces + Edges", style_tab)
        self.style_faces = QRadioButton("Faces", style_tab)
        self.style_edges = QRadioButton("Edges", style_tab)
        self.style_both.setToolTip(
            "Display style for ALL visible boxes. Faces + Edges is the default; a box with neither would be invisible.")
        self.style_both.setChecked(True)
        self.style_group = QButtonGroup(self)
        for button in (self.style_both, self.style_faces, self.style_edges):
            self.style_group.addButton(button)
        self.style_group.buttonToggled.connect(lambda *_args: self._on_style_changed())

        style_layout.addWidget(self.style_both, 0, 0, 1, 4)
        style_layout.addWidget(self.style_faces, 1, 0)
        style_layout.addWidget(self.style_edges, 2, 0)
        style_layout.setColumnStretch(0, 1)

        self.opacity = QDoubleSpinBox(style_tab)
        self.opacity.setRange(0.1, 1.0)
        self.opacity.setDecimals(2)
        self.opacity.setSingleStep(0.05)
        self.opacity.setValue(0.35)
        self.opacity.valueChanged.connect(self._on_style_changed)
        style_layout.addWidget(QLabel("Opacity"), 1, 2)
        style_layout.addWidget(self.opacity, 1, 3)

        self.edge_width = QDoubleSpinBox(style_tab)
        self.edge_width.setRange(1.0, 10.0)
        self.edge_width.setDecimals(1)
        self.edge_width.setSingleStep(0.5)
        self.edge_width.setValue(2.0)
        self.edge_width.valueChanged.connect(self._on_style_changed)
        style_layout.addWidget(QLabel("Edge Width"), 2, 2)
        style_layout.addWidget(self.edge_width, 2, 3)

        for widget in (self.cx, self.cy, self.cz, self.sx, self.sy, self.sz):
            widget.valueChanged.connect(self._on_geometry_changed)

        # editor_layout.addRow(style_grid)
        layout.addWidget(self.box_settings)


        self._sync_enabled_state()
        self.hide()

    def _action_button(self, text: str, icon_name: str, slot, tooltip: str) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setIcon(load_icon(icon_name))
        button.setIconSize(QSize(20, 20))
        button.setToolTip(tooltip)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setAutoRaise(True)
        button.setStyleSheet("QToolButton { padding: 2px 0px; }")  # the theme's 6 px sides cut labels
        button.clicked.connect(slot)
        return button

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
            self._close_selection_editor()
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
        self._sites = self.runtime.binding_sites.list_sites(molecule_id=int(molecule.id or 0))
        self._populate_tree()
        self.site_tree.blockSignals(False)
        self._sync_enabled_state()
        if self.isVisible() and self._auto_preview_enabled:
            self._refresh_preview(force=False)
        else:
            self._clear_preview()

    def _populate_tree(self) -> None:
        active_id = self._active_site_id()
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
        ink = max((QColor("#202124"), QColor("#ffffff")), key=lambda c: contrast_ratio(c, color))
        item.setForeground(1, QBrush(ink))  # whichever reads better on the site color

    def active_box_geometry(self) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
        """(center, size) of the receptor's active binding-site box, or None if unset — used to
        orient the camera onto the box face when a receptor is loaded."""
        molecule = self._current_molecule
        if molecule is None:
            return None
        active_id = self._active_site_id()
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

    def _active_site_id(self) -> int:
        """The current receptor's active site (0: none), from the sites it has loaded."""
        return next((int(site.id or 0) for site in self._sites if site.is_active), 0)

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
            lambda: self.runtime.binding_sites.suggest_box_from_ligand(ligand_id=ligand_id),
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

    def _pymol_cmd(self):
        dock = getattr(self.window(), "pymol_dock", None)
        return getattr(dock, "cmd", None) if dock is not None else None

    def _open_pocket_detection(self) -> None:
        tools = getattr(self.window(), "tools", None)
        if tools is not None:
            tools.open_tool(POCKET_DETECTION_VIEW_ID)

    def _pymol_shown(self) -> bool:
        dock = getattr(self.window(), "pymol_dock", None)
        return getattr(dock, "cmd", None) is not None and dock.isVisible()

    def _pymol_selections(self) -> list[str]:
        """Named PyMOL selections with atoms in them, while the PyMOL panel is on screen."""
        if not self._pymol_shown():
            return []
        cmd = self._pymol_cmd()
        try:
            return [name for name in cmd.get_names("public_selections")
                    if not name.startswith("amdock_") and cmd.count_atoms(name)]
        except Exception:
            return []

    def _sync_selection_button(self) -> None:
        editing = not self.selection_row.isHidden()
        if editing and not self._pymol_shown():
            self._close_selection_editor()
            editing = False
        # While editing it stays on, so the user can close the editor with it.
        has_selection = editing or bool(self._pymol_selections())
        self.selection_button.setEnabled(self._current_molecule is not None and has_selection)

    def _toggle_selection_editor(self) -> None:
        if not self.selection_row.isHidden():
            self._close_selection_editor()
            return
        names = self._pymol_selections()
        if names and self.selection_edit.text().strip() == "sele" and "sele" not in names:
            self.selection_edit.setText(names[0])
        self.save_row.hide()
        self.selection_row.show()
        self.selection_button.setChecked(True)
        self.selection_edit.setFocus()
        self.selection_edit.selectAll()
        self._preview_selection(self.selection_edit.text())

    def _close_selection_editor(self) -> None:
        self.selection_row.hide()
        self.save_row.show()
        self.selection_button.setChecked(False)
        cmd = self._pymol_cmd()
        if cmd is not None:
            try:
                cmd.delete(_SELECTION_PREVIEW)
            except Exception:
                pass

    def _check_selection(self, text: str) -> str:
        """Why ``text`` can't center a box, or "" when it is receptor residues/metals only."""
        molecule = self._current_molecule
        cmd = self._pymol_cmd()
        text = text.strip()
        if molecule is None or cmd is None:
            return "Load a receptor in PyMOL first."
        if not text:
            return "Type a PyMOL selection."
        receptor_obj = f"receptor_{int(molecule.id or 0)}"
        try:
            total = cmd.count_atoms(f"({text})")
            inside = cmd.count_atoms(f"({text}) and {receptor_obj} and (polymer or metals)")
            cmd.select(_SELECTION_PREVIEW, f"({text})", enable=1)  # shown live while typing
        except Exception:
            return "Not a valid PyMOL selection."
        if total == 0:
            return "Matches no atoms."
        if inside != total:
            return f"{total - inside} of {total} atoms are not receptor residues or metals."
        return ""

    def _preview_selection(self, text: str) -> None:
        problem = self._check_selection(text)
        if problem:
            cmd = self._pymol_cmd()
            if cmd is not None:
                try:
                    cmd.delete(_SELECTION_PREVIEW)
                except Exception:
                    pass
        hue = color("orange" if problem else "green").name()
        self.selection_edit.setStyleSheet(f"QLineEdit {{ border: 2px solid {hue}; }}")
        self.selection_edit.setToolTip(problem or "Receptor residues/metals — press Build box.")
        self.selection_apply.setEnabled(not problem)

    def _apply_selection(self) -> None:
        selection = self.selection_edit.text().strip()
        if self._check_selection(selection):
            return
        self._close_selection_editor()
        self._box_from_selection(selection)

    def _box_from_selection(self, selection: str) -> None:
        from amdockvs.binding_sites.cavity import pseudo_pocket_box
        from amdockvs.binding_sites.boxes import pseudo_ligand_box

        molecule = self._current_molecule
        cmd = self._pymol_cmd()
        if molecule is None or cmd is None:
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

        geometry = app_config(getattr(self.window(), "runtime", None)).binding_sites

        def compute() -> dict:
            # LIGSITE pocket detection when possible; fall back to the bounded-push heuristic if
            # scipy is missing or no accessible pocket is found near the selection. Runs off the
            # GUI thread — a large receptor's voxel grid can take ~1s.
            try:
                return pseudo_pocket_box(
                    receptor_xyz,
                    receptor_vdw,
                    selection_list,
                    **geometry.residues.model_dump(),
                    padding=geometry.box_padding,
                    max_edge=geometry.box_max_edge,
                )
            except Exception:
                return pseudo_ligand_box(receptor_xyz, selection_list)

        run_async(
            compute,
            lambda box: self._apply_box_from_selection(cmd, box),
            on_error=lambda exc: QMessageBox.warning(self, "Box from selection", str(exc)),
            busy=self,
        )

    def _apply_box_from_selection(self, cmd, box: dict) -> None:
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

    def _show_tree_menu(self, pos) -> None:
        item = self.site_tree.itemAt(pos)
        if item is None or item.data(0, Qt.UserRole) != ("working", None):
            return
        menu = QMenu(self.site_tree)
        menu.addAction("Discard working box", self._discard_working_box)
        menu.exec(self.site_tree.viewport().mapToGlobal(pos))

    def _discard_working_box(self) -> None:
        """Drop the unsaved box and go back to the active site, as if it had never been made."""
        if not self._working.dirty:
            return
        self._working = GridBoxState()
        self._remove_working_item()
        active_id = self._active_site_id()
        if active_id:
            self.focus_binding_site(self._current_molecule, site_id=active_id, ensure_selected=True)
        else:
            self._sync_editor_from_working()
            self._sync_enabled_state()
            self._refresh_preview(force=True)

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
        if molecule is None:
            return
        if site is None:  # nothing saved to overwrite yet: Save means save as new
            self._save_new_site()
            return
        try:
            self.runtime.binding_sites.save_site(
                molecule_id=int(molecule.id or 0),
                binding_site_id=int(site.id or 0),
                name=str(site.name or f"Site {int(site.id or 0)}"),
                source=str(site.source or "manual"),
                source_ref=str(site.source_ref or ""),
                center=(self.cx.value(), self.cy.value(), self.cz.value()),
                size=(self.sx.value(), self.sy.value(), self.sz.value()),
                set_active=bool(site.is_active),
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
            site = self.runtime.binding_sites.save_site(
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
            self.runtime.binding_sites.set_active_site(
                molecule_id=int(molecule.id or 0),
                binding_site_id=int(site.id or 0),
            )
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
        self._selection_poll.start()
        if self._auto_preview_enabled or self._force_preview_once:
            self._refresh_preview(force=False)

    def hideEvent(self, event) -> None:
        self._selection_poll.stop()
        self._close_selection_editor()
        self._clear_preview()
        self._clear_pseudo_ligand()
        super().hideEvent(event)

    def _sync_enabled_state(self) -> None:
        has_receptor = self._current_molecule is not None
        selected = self._selected_site()
        has_selected_site = selected is not None
        active_id = self._active_site_id()
        self.site_tree.setEnabled(has_receptor)
        self.show_working_box.setEnabled(has_receptor)
        for button in (self.new_button, self.ligand_button, self.predict_button, self.save_as_button):
            button.setEnabled(has_receptor)
        self._sync_selection_button()
        self.save_button.setEnabled(has_receptor)  # with no saved site to overwrite, Save saves as new
        self.activate_button.setEnabled(has_selected_site and int(selected.id or 0) != active_id)  # already active: nothing to do


__all__ = ["GridBoxSettingDockWidget"]
