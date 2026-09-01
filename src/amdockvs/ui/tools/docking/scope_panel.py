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




class ScopePanel:
    """Selected, filtered and active molecule-scope resolver."""

    def _ligand_scope_mode(self) -> str:
        combo = getattr(self, "ligand_scope_combo", None)
        fallback = "reference" if self._run_kind() == "redocking" else "general"
        return str(combo.currentData() or fallback) if combo is not None else fallback

    def _ligand_scope_options(self) -> list[tuple[str, str]]:
        if self._run_kind() == "redocking":
            return [
                ("Reference", "reference"),
                ("Selected (marked reference ligands)", "selected"),
                ("Filtered (matching reference table filter)", "filtered"),
            ]
        return [
            ("Active (all ligands)", "active"),
            ("Selected (marked general ligands)", "selected"),
            ("Filtered (matching general table filter)", "filtered"),
        ]

    def _sync_ligand_scope_options(self) -> None:
        combo = getattr(self, "ligand_scope_combo", None)
        if combo is None:
            return
        previous = str(combo.currentData() or "")
        options = self._ligand_scope_options()
        allowed = {value for _label, value in options}
        fallback = "reference" if self._run_kind() == "redocking" else "general"
        target = previous if previous in allowed else fallback
        combo.blockSignals(True)
        combo.clear()
        for label, value in options:
            combo.addItem(label, value)
        index = combo.findData(target)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _selection_ligand_ids(self) -> list[int]:
        # The sticky buffer, not the live table — survives prepare/scroll clearing the selection.
        return list(self._selected_ligand_ids)

    def _ligand_scope(self):
        # Single source of truth for both preparation and docking. Reads UI here, then hands
        # off to the pure resolver so a worker thread can rebuild the same scope from captured
        # values without touching widgets.
        return self._resolve_ligand_scope(
            self._ligand_scope_mode(),
            self._selection_ligand_ids(),
            self._filtered_ligand_ids(),
            run_kind=self._run_kind(),
        )

    def _resolve_ligand_scope(
        self,
        mode: str,
        selected_ids: list[int],
        filtered_ids: list[int],
        *,
        run_kind: str = "docking",
    ):
        # Selected/Filtered are constrained by experiment: docking uses general ligands;
        # redocking uses reference ligands. This prevents stale selections crossing modes.
        usage_class = {"general": "general", "reference": "reference"}.get(
            mode,
            "reference" if str(run_kind or "") == "redocking" else "general",
        )
        scope = self.runtime.molecules.select(
            role="ligand",
            molecule_kind=self._ligand_type(),
            workflow=VINA_PROGRAM.workflow_key,
            excluded=False,
            usage_class=usage_class,
        )
        if mode in ("selected", "filtered"):
            ids = selected_ids if mode == "selected" else filtered_ids
            scope = self.runtime.molecules.filter(scope, filters={"id__in": ids or [0]})
        return scope

    def _filtered_ligand_ids(self) -> list[int]:
        widget = self._catalog_ligand_widget()
        table = getattr(widget, "table", None) if widget is not None else None
        if table is None:
            return []
        return [int(value) for value in table.all_filtered_ids() if int(value) > 0]

    # Preparation shares the docking scope (one selector drives both).
    def _prep_ligand_scope(self):
        return self._ligand_scope()

    def _run_lig_mode(self) -> str:
        # Review & Run scope: "all" (all prepared) or "selected" (the marked, prepared ones).
        combo = getattr(self, "run_ligand_scope_combo", None)
        return str(combo.currentData() or "all") if combo is not None else "all"

    def _run_rec_mode(self) -> str:
        combo = getattr(self, "run_receptor_scope_combo", None)
        return str(combo.currentData() or "all") if combo is not None else "all"

    def _run_lig_base_scope(self, mode: str, sel_ids: list[int]):
        # The "population" the card counts against (ready/total). "all" → every ligand (so the
        # card reads prepared/total); "selected" → the marked ligands (so it reads prepared/marked).
        base = self.runtime.molecules.select(
            role="ligand", molecule_kind=self._ligand_type(), workflow=VINA_PROGRAM.workflow_key, excluded=False,
            usage_class="general",
        )
        if mode == "selected":
            return self.runtime.molecules.filter(base, filters={"id__in": sel_ids or [0]})
        return base

    def _receptor_scope(self):
        return self.runtime.molecules.select(
            role="receptor",
            molecule_kind=self._receptor_type(),
            workflow=VINA_PROGRAM.workflow_key,
            excluded=False,
        )

    def _receptor_scope_mode(self) -> str:
        combo = getattr(self, "receptor_scope_combo", None)
        return str(combo.currentData() or "active") if combo is not None else "active"

    def _filtered_receptor_ids(self) -> list[int]:
        table = getattr(self._catalog_receptor_widget(), "table", None)
        if table is None:
            return []
        return [int(value) for value in table.all_filtered_ids() if int(value) > 0]

    def _resolve_receptor_scope_for_label(self, mode: str, receptor_ids: list[int]):
        # Pure non-raising scope for labels/counts: empty selection → empty scope (id 0).
        scope = self._receptor_scope()
        if mode in ("selected", "filtered"):
            scope = self.runtime.molecules.filter(scope, filters={"id__in": receptor_ids or [0]})
        return scope

    def _selected_receptor_scope(self):
        return self._resolve_receptor_scope(self._receptor_scope_mode(), self._effective_receptor_ids())

    def _resolve_receptor_scope(self, mode: str, receptor_ids: list[int]):
        # Pure: raises on an empty non-active selection so prepare/run warn the user.
        if mode == "active":
            return self._receptor_scope()
        if not receptor_ids:
            raise ValueError("Select one or more receptors, or switch the scope to 'Active'.")
        return self.runtime.molecules.filter(
            self._receptor_scope(),
            filters={"id__in": [int(value) for value in receptor_ids]},
        )

    def _effective_receptor_ids(self) -> list[int]:
        return self._resolve_receptor_ids(
            self._receptor_scope_mode(),
            list(self._selected_receptor_ids),
            self._filtered_receptor_ids(),
            self._focused_receptor_id,
        )

    def _resolve_receptor_ids(self, mode, selected_ids, filtered_ids, focused) -> list[int]:
        if mode == "active":
            return sorted(
                {int(value) for value in self.runtime.molecules.stream_ids(self._receptor_scope()) if int(value) > 0}
            )
        if mode == "filtered":
            return sorted({int(value) for value in filtered_ids})
        return sorted({int(value) for value in selected_ids if int(value) > 0})

    def _rows_for_ids_scoped(self, scope, ids: list[int]) -> list[object]:
        # Pure: caller supplies an already-built scope (no widget access).
        if not ids:
            return []
        narrowed = self.runtime.molecules.filter(scope, filters={"id__in": [int(value) for value in ids]})
        return list(self.runtime.molecules.stream(narrowed))
