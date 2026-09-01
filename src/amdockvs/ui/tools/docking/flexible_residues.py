from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget, QGridLayout,
)
from amdockvs.ui.async_query import run_async
from amdockvs.ui.widgets import right_aligned, split_button
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.ui.catalog.ligands import LIGANDS_VIEW_ID
from amdockvs.ui.catalog.receptors import RECEPTOR_VIEW_ID
from amdockvs.ui.catalog.binding_sites import BINDING_SITES_VIEW_ID
from amdockvs.ui.tools.molecules.build import BUILD_ID
from amdockvs.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.docking.protocols import PROTOCOL_SCHEMA, protocol_hash, protocol_identity
from amdockvs.docking.programs import GNINA_PROGRAM, VINA_PROGRAM, list_docking_programs
from amdockvs.models import EngineState
from amdockvs.vocab import MoleculeType
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    FilterOperator,
    FilterSpec,
    SortSpec,
    TableConfig,
    TableLoadMode,
)
from ms_components.ms_stepper import Orientation, QStepper

DOCKING_VIEW_ID = "workspace.docking"
PREP_STATUS_VIEW_ID = "workspace.prep_status"
DEFAULT_PROGRAM = VINA_PROGRAM.key
MAX_REDOCKING_PROTOCOLS = 12
# Non-terminal job statuses — a docking job in any of these is "live" for duplicate detection.
_ACTIVE_JOB_STATUSES = ("pending", "running", "staging", "cancel_requested")

def _spinbox(*, minimum: int, maximum: int, value: int) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget


class _WheelGuard(QObject):
    """Swallow wheel events on combos/spinboxes unless they have focus.

    Inside a scroll area, the wheel otherwise changes the value under the cursor
    instead of scrolling the page. With StrongFocus + this filter, the widget only
    reacts to the wheel after you click into it; otherwise the wheel scrolls.
    """

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Wheel and not obj.hasFocus():
            return True
        return super().eventFilter(obj, event)




class FlexibleResiduesPanel:
    """Flexible-residue candidates, selection and persistence component."""

    def _build_flex_residues_box(self, page: QWidget) -> QWidget:
        box = QGroupBox("Flexible residues", page)
        lay = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Candidates from", box))
        self.flex_source_combo = QComboBox(box)
        # The source only narrows the *candidate* pool; it isn't the owner of the selection.
        # Only "in box" works today; the others are visible-but-disabled integration points.
        self.flex_source_combo.addItem("Residues in box", "box")
        self.flex_source_combo.addItem("Around reference ligand (soon)", "ligand")
        self.flex_source_combo.addItem("PyMOL pick (soon)", "pymol")
        for i in (1, 2):
            item = self.flex_source_combo.model().item(i)
            if item is not None:
                item.setEnabled(False)
        row.addWidget(self.flex_source_combo, 1)
        self.load_flex_btn = QPushButton("Load", box)
        self.load_flex_btn.clicked.connect(self._load_flex_residues)
        row.addWidget(self.load_flex_btn)
        lay.addLayout(row)

        # Two panels: candidates (narrowed pool) on the left, the actual selection on the right.
        panels = QHBoxLayout()
        cand_col = QVBoxLayout()
        self.flex_cand_label = QLabel("Candidates", box)
        cand_col.addWidget(self.flex_cand_label)
        self.flex_candidates = QListWidget(box)
        self.flex_candidates.setMinimumHeight(160)
        self.flex_candidates.setSelectionMode(QListWidget.ExtendedSelection)
        self.flex_candidates.itemClicked.connect(self._on_flex_item_clicked)
        self.flex_candidates.itemDoubleClicked.connect(
            lambda it: self._mutate_flex(add=[it.data(Qt.UserRole)])
        )
        cand_col.addWidget(self.flex_candidates)
        panels.addLayout(cand_col, 1)

        mid = QVBoxLayout()
        mid.addStretch(1)
        add_btn = QPushButton("→", box)
        add_btn.setToolTip("Add selected candidates to flexible")
        add_btn.setMaximumWidth(40)
        add_btn.clicked.connect(self._add_selected_candidates)
        rem_btn = QPushButton("←", box)
        rem_btn.setToolTip("Remove from flexible")
        rem_btn.setMaximumWidth(40)
        rem_btn.clicked.connect(self._remove_selected_flex)
        mid.addWidget(add_btn)
        mid.addWidget(rem_btn)
        mid.addStretch(1)
        panels.addLayout(mid)

        sel_col = QVBoxLayout()
        self.flex_sel_label = QLabel("Selected (0)", box)
        sel_col.addWidget(self.flex_sel_label)
        self.flex_selected = QListWidget(box)
        self.flex_selected.setMinimumHeight(160)
        self.flex_selected.setSelectionMode(QListWidget.ExtendedSelection)
        self.flex_selected.itemClicked.connect(self._on_flex_item_clicked)
        self.flex_selected.itemDoubleClicked.connect(
            lambda it: self._mutate_flex(remove=[it.data(Qt.UserRole)])
        )
        sel_col.addWidget(self.flex_selected)
        panels.addLayout(sel_col, 1)
        lay.addLayout(panels)

        # Monospace so the chain:resname:resnum residue keys line up. Set once on each list —
        # it applies to all current and future items.
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.flex_candidates.setFont(mono)
        self.flex_selected.setFont(mono)

        self.flex_count_label = QLabel("Focus a receptor and press Load.", box)
        self.flex_count_label.setWordWrap(True)
        lay.addWidget(self.flex_count_label)
        return box

    def _flex_label_for(self, key: str) -> str:
        if known := self._flex_labels.get(key):
            return known
        parts = key.split(":")
        if len(parts) != 3:
            return key
        chain, resname, resnum = parts
        return f"{'-' if chain == '_' else chain} {resname} {resnum}"

    def _on_flex_item_clicked(self, item) -> None:
        # Clicking a residue (in either panel) highlights it on the receptor in PyMOL.
        if item is None or self._flex_receptor_id is None:
            return
        parts = str(item.data(Qt.UserRole) or "").split(":")
        if len(parts) != 3:
            return
        chain, _resname, resnum = parts
        try:
            resi = int(resnum)
        except ValueError:
            return
        self.window().highlight_receptor_residue(self._flex_receptor_id, "" if chain == "_" else chain, resi)

    def _load_flex_residues(self) -> None:
        source = str(self.flex_source_combo.currentData() or "box")
        if source != "box":
            self.flex_count_label.setText("That source isn't available yet — use 'Residues in box'.")
            return
        self._load_box_residues()

    def _load_box_residues(self) -> None:
        rid = self._focused_receptor_id
        if not rid:
            self.flex_count_label.setText("Focus a receptor with an active binding site first.")
            return
        self._flex_loading_id = rid
        self.load_flex_btn.setEnabled(False)
        run_async(
            lambda: (
                self.runtime.docking.list_box_residues(receptor_id=rid),
                self.runtime.docking.get_flexible_residues(receptor_id=rid),
            ),
            lambda res: self._apply_box_residues(rid, res[0], res[1]),
            on_error=lambda _exc: self._apply_box_residues(rid, [], []),
            busy=self.flex_box,
        )

    def _apply_box_residues(self, rid: int, residues: list[dict], selected: list[str]) -> None:
        # Focus may have moved on while loading — drop the stale result so panels match the
        # receptor that's actually focused now.
        if rid != self._focused_receptor_id:
            if rid == self._flex_loading_id:
                self._flex_loading_id = None
            return
        self._flex_loading_id = None
        self.load_flex_btn.setEnabled(True)
        self._flex_receptor_id = rid
        self._flex_candidate_rows = residues
        self._flex_selected_keys = set(selected)
        for r in residues:
            self._flex_labels[r["key"]] = r["label"]
        self._render_flex_lists()
        if not residues:
            self.flex_count_label.setText("No residues in box (no grid, or structure unavailable).")

    def _render_flex_lists(self) -> None:
        sel = self._flex_selected_keys
        # Candidates panel: the current pool minus what's already chosen (no dupes across panels).
        self.flex_candidates.clear()
        for r in self._flex_candidate_rows:
            if r["key"] in sel:
                continue
            
                
            it = QListWidgetItem(r["label"])
            it.setData(Qt.UserRole, r["key"])
            self.flex_candidates.addItem(it)
        # Selected panel: the receptor's persisted truth — shown even if not in the current pool.
        self.flex_selected.clear()
        for key in sorted(sel):
            it = QListWidgetItem(self._flex_label_for(key))
            it.setData(Qt.UserRole, key)
            self.flex_selected.addItem(it)
        self.flex_cand_label.setText(f"Candidates ({self.flex_candidates.count()})")
        self.flex_sel_label.setText(f"Selected ({len(sel)})")
        self.flex_count_label.setText(f"{len(sel)} flexible · {len(self._flex_candidate_rows)} in box")

    def _add_selected_candidates(self) -> None:
        self._mutate_flex(add=[it.data(Qt.UserRole) for it in self.flex_candidates.selectedItems()])

    def _remove_selected_flex(self) -> None:
        self._mutate_flex(remove=[it.data(Qt.UserRole) for it in self.flex_selected.selectedItems()])

    def _mutate_flex(self, *, add: list | None = None, remove: list | None = None) -> None:
        if self._flex_receptor_id is None:
            return
        self._flex_selected_keys |= {str(k) for k in (add or [])}
        self._flex_selected_keys -= {str(k) for k in (remove or [])}
        try:
            self.runtime.docking.set_flexible_residues(
                receptor_id=self._flex_receptor_id,
                residue_keys=sorted(self._flex_selected_keys),
            )
        except Exception as exc:  # persistence failure shouldn't crash the UI
            self.flex_count_label.setText(f"Could not save: {exc}")
            return
        self._render_flex_lists()

    def _sync_flex_for_focus(self, has_grid: bool) -> None:
        # Auto-load (or clear) the flex panel when the focused receptor changes — no second
        # trip to a Load button, and never leave the previous receptor's residues lingering.
        rid = self._focused_receptor_id
        if not has_grid or not rid:
            if self._flex_receptor_id is not None or self._flex_loading_id is not None:
                self._clear_flex_panel()
            return
        if rid in (self._flex_receptor_id, self._flex_loading_id):
            return  # already loaded or loading for this receptor
        self._load_box_residues()

    def _clear_flex_panel(self) -> None:
        self._flex_receptor_id = None
        self._flex_loading_id = None
        self._flex_candidate_rows = []
        self._flex_selected_keys = set()
        self.flex_candidates.clear()
        self.flex_selected.clear()
        self.flex_cand_label.setText("Candidates")
        self.flex_sel_label.setText("Selected (0)")
        self.flex_count_label.setText("Focus a receptor with an active binding site.")
