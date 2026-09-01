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

from amdockvs.ui.tools.docking.flexible_residues import FlexibleResiduesPanel
from amdockvs.ui.tools.docking.preparation_panel import EngineStatePrepView, PreparationPanel
from amdockvs.ui.tools.docking.protocol_editor import ProtocolEditorWidget
from amdockvs.ui.tools.docking.run_panel import RunPanel
from amdockvs.ui.tools.docking.scope_panel import ScopePanel
from amdockvs.docking.readiness import DockingReadinessService
from amdockvs.docking.submission import DockingSubmissionService

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




class DockingStudioWidget(
    ProtocolEditorWidget,
    PreparationPanel,
    ScopePanel,
    FlexibleResiduesPanel,
    RunPanel,
    QWidget,
):
    # Cap on remembered selected ids per side (ligands/receptors) — a few thousand ints is
    # negligible RAM; beyond this the user almost certainly means "All" anyway.
    _SELECTION_CAP = 2000

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self.readiness_service = DockingReadinessService(runtime)
        self.submission_service = DockingSubmissionService(runtime)
        # Sticky selection memory: dynamic tables drop their Qt selection on scroll/refresh (and
        # preparation clears it), so we remember the marked ids here. Capped to bound RAM.
        self._selected_receptor_ids: list[int] = []
        self._selected_ligand_ids: list[int] = []
        # The catalog Ligands table we're currently listening to (see _bind_ligand_table_signals).
        self._bound_ligand_table = None
        self._bound_receptor_table = None
        self._focused_receptor_id: int | None = None
        # job_id -> payload signature of docking jobs we launched this session, to spot duplicates
        # (same signature = identical settings/molecules) without inspecting each job's payload.
        self._docking_job_sigs: dict[str, str] = {}
        self._protocols: list[dict] = []
        # False while the widget is the no-project placeholder: none of the steps (tables,
        # flex box) exist, so show/hide must not reach for them.
        self._ready = False
        # Monotonic token so a worker's stale result is dropped when a newer refresh started.
        self._refresh_token = 0
        # Receptor whose flexible-residue list is currently loaded in the BS box.
        self._flex_receptor_id: int | None = None
        # Flexible-residue state. Selected keys are the receptor's truth (persisted, box-independent);
        # candidate rows are just the current filter's suggestions; labels caches nice display names.
        self._flex_selected_keys: set[str] = set()
        self._flex_candidate_rows: list[dict] = []
        self._flex_labels: dict[str, str] = {}
        # Receptor a flex load is in flight for — guards against re-dispatching the same load on
        # every refresh tick (optimistic; cleared when the async result lands).
        self._flex_loading_id: int | None = None
        # Single-flight: at most one refresh worker in flight; extra requests coalesce into
        # one pending re-run. Without this, rapid triggers (preview timer + background
        # refresh + selection changes) flood the threadpool → CPU/RAM blowup + GIL thrash.
        self._refresh_inflight = False
        self._refresh_pending = False
        self._refresh_pending_force = False
        # Memoize by (step, inputs): re-entering a step or a redundant refresh with the same
        # inputs is a no-op, so navigation/idle don't re-run the expensive count/check_required
        # scans. force=True (explicit/periodic refresh) bypasses it.
        self._last_refresh_sig = None
        # Debounce the requirement preview so rapid selection changes coalesce into
        # one query cascade instead of hammering the DB on the UI thread per event.
        self._req_preview_timer = QTimer(self)
        self._req_preview_timer.setSingleShot(True)
        self._req_preview_timer.setInterval(200)
        self._req_preview_timer.timeout.connect(self._refresh_requirement_preview)
        # While a job runs, re-count so "prepared / total" climbs live instead of jumping at the
        # end — on a big set that wait is long. Runs only on the Run step; the monitor snapshot
        # switches it on/off, so an idle project never polls.
        self._jobs_active = 0
        self._check_inflight = False
        self._prep_poll_timer = QTimer(self)
        self._prep_poll_timer.setInterval(4000)
        self._prep_poll_timer.timeout.connect(self._check_requirements)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        outer = QVBoxLayout(self)
        # outer.setContentsMargins(6, 6, 6, 4)
        # outer.setSpacing(6)

        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to prepare molecules and run docking.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return

        # Per-step summary cards (no global card row): each card lives in the step it
        # describes. Ligands have no cards — the scope label already states total/prepared
        # (the cards just duplicated it), so the Ligands step shows the count there.

        # Wizard: reusable QStepper (Quasar-style). Non-linear (revisit any step freely),
        # no built-in nav bar (we drive with our own Prepare/Run buttons). Each step embeds
        # the tables it needs (selection happens where you are, not in a global viewer).
        self.stepper = QStepper(
            orientation=Orientation.HORIZONTAL,
            linear=False,
            alternative_labels=True,
            show_navigation=False,
            parent=self,
        )
        self.step_programs = self.stepper.add_step("Programs", "select & configure")
        self.step_programs.add_widget(self._in_scroll(self._build_programs_tab()))
        self.step_ligands = self.stepper.add_step("Ligands", "select & prepare")
        # The scroll area is a safety net, not the layout: both steps are built on splitters
        # whose minimum (~530 / ~710px) is well under a normal viewport, so the outer scrollbar
        # only shows up in a genuinely tiny window instead of always, as it used to.
        self.step_ligands.add_widget(self._in_scroll(self._build_ligands_tab()))
        self.step_receptors = self.stepper.add_step("Receptors", "prepare & grid")
        self.step_receptors.add_widget(self._in_scroll(self._build_receptors_tab()))
        self.step_run = self.stepper.add_step("Preview & Run", "review & launch")
        self.step_run.add_widget(self._in_scroll(self._build_preview_run_tab()))
        self.stepper.step_changed.connect(self._on_step_changed)
        outer.addWidget(self.stepper, 1)

        # One thin status line (Refresh now lives on each table toolbar, Open Results moved to
        # the Run step). No footer layout/margins — it just sits flush under the stepper so the
        # old button row leaves no empty band behind.
        # self.status_label = QLabel("", self)
        # self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # outer.addWidget(self.status_label)

        # Stop the wheel from changing combo/spin values while scrolling the steps.
        self._wheel_guard = _WheelGuard(self)
        for widget in [*self.findChildren(QComboBox), *self.findChildren(QSpinBox)]:
            widget.setFocusPolicy(Qt.StrongFocus)
            widget.installEventFilter(self._wheel_guard)

        # Sync the ligand table to the default scope mode (Active) so its total matches.
        self._ready = True
        self._sync_ligand_table_filter()
        self._sync_receptor_table_filter()
        self.refresh()

    def _in_scroll(self, inner: QWidget) -> QScrollArea:
        # Each step scrolls vertically so tall content (table + prep + engine panel) is never
        # clipped; horizontal scrollbar off so a wide page can't stretch the whole window.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        # scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        return scroll

    def _on_step_changed(self, _step) -> None:
        # Each unprepared filter only applies on its own step.
        self._sync_ligand_table_filter()
        self._sync_receptor_table_filter()
        if self.stepper.current_index != self._PREP_STEP["receptor"]:
            self._reset_receptor_focus()
        # Steps 2/3 work *on* a catalog tab (they have no table of their own), so bring it up.
        # Preview & Run is the step *before* the results, so it opens them: the run lands there.
        from amdockvs.ui.catalog.domain_views import COMPLEXES_VIEW_ID  # circular at import time

        view_id = {1: LIGANDS_VIEW_ID, 2: RECEPTOR_VIEW_ID, 3: COMPLEXES_VIEW_ID}.get(
            self.stepper.current_index
        )
        if view_id is not None:
            opener = getattr(self.window(), "open_or_focus_view", None)
            if callable(opener):
                opener(view_id)
        self._req_preview_timer.start()
        # Entering Preview & Run auto-checks (no manual button); _check_requirements self-gates.
        self._check_requirements()
        self._sync_prep_poll()

    def on_job_finished(self, *_args) -> None:
        """A preparation job landing changes the counts under the Run step. Re-check instead of
        leaving a stale "0 / 20 prepared" until the user clicks something. `_check_requirements`
        self-gates on the step, so this is free while the user is anywhere else."""
        self._check_requirements()

    def on_jobs_snapshot(self, snapshot) -> None:
        """Monitor heartbeat: poll the counts only while something is actually running."""
        self._jobs_active = int(getattr(snapshot, "jobs_active", 0) or 0)
        self._sync_prep_poll()

    def _sync_prep_poll(self) -> None:
        want = self._jobs_active > 0 and self.stepper.current_index == 3
        if want == self._prep_poll_timer.isActive():
            return  # the snapshot ticks every 500 ms; restarting would reset the countdown forever
        self._prep_poll_timer.start() if want else self._prep_poll_timer.stop()

    def _warn(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def _error(self, title: str, exc: Exception) -> None:
        QMessageBox.critical(self, title, str(exc))

    def on_binding_site_changed(self, molecule_id: int) -> None:
        # Active box geometry changed for some receptor; if it's the one in focus, its in-box
        # candidates may differ now → force a reload. Selected residues are receptor-level and stay.
        if int(molecule_id or 0) == int(self._focused_receptor_id or 0) and self._focused_receptor_id:
            self._load_box_residues()

    def _on_receptor_clicked(self, receptor) -> None:
        self._focused_receptor_id = int(getattr(receptor, "id", 0) or 0) or None
        # Marking a receptor shows it in PyMOL together with its active binding-site box.
        self.window().focus_receptor_in_pymol(receptor)
        self._req_preview_timer.start()

    def _on_receptor_selection_changed(self, receptors: list[object]) -> None:
        ids = sorted(
            {
                int(getattr(receptor, "id", 0) or 0)
                for receptor in list(receptors or [])
                if int(getattr(receptor, "id", 0) or 0) > 0
            }
        )
        # Ignore empty updates: scroll/refresh/prepare clear the Qt selection — that's not the
        # user deselecting everything, so keep the last real pick.
        if ids:
            self._selected_receptor_ids = ids[: self._SELECTION_CAP]
        if self._focused_receptor_id is None and self._selected_receptor_ids:
            self._focused_receptor_id = self._selected_receptor_ids[0]
        self._req_preview_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._ready:
            return
        self._sync_ligand_table_filter()
        self._sync_receptor_table_filter()

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self._ready:
            return
        self._release_ligand_table()
        self._release_receptor_table()

    def refresh(self) -> None:
        # GUI-thread only: the embedded tables load via their own (paged) models, and the
        # prep-target lists are cheap. Every counting/requirement query is pushed off-thread
        # by _dispatch_refresh so this never blocks, regardless of molecule-set size.
        self._sync_ligand_table_filter()
        self._sync_receptor_table_filter()
        self._refresh_prep_targets()
        self._refresh_receptor_prep_targets()
        self._dispatch_refresh(force=True)

def register_docking_workspace(window) -> None:
    def _make_docking_studio():
        widget = DockingStudioWidget(runtime=window.runtime, parent=window.central_widget)
        # Reload in-box flexible-residue candidates when the grid box changes (new site,
        # active site switched, center/size saved). grid_dock is None only when headless.
        if getattr(window, "grid_dock", None) is not None:
            window.grid_dock.binding_site_changed.connect(widget.on_binding_site_changed)
        # Prep jobs run in the background: the snapshot says whether one is running (drives the
        # live poll of the Run-step counts) and job_finished gives the final, exact refresh.
        bridge = getattr(window, "monitor_bridge", None)
        if bridge is not None:
            bridge.job_finished.connect(widget.on_job_finished)
            bridge.project_snapshot_updated.connect(widget.on_jobs_snapshot)
        return widget

    window.register_main_view(DOCKING_VIEW_ID, "Docking Studio", _make_docking_studio)


    # Per-engine preparation status: a full table (with its own filters/export), so it lives
    # as a central tab instead of a cramped panel inside the Ligands step. Ligands and
    # receptors share it — the role is a filter, not a second view.
    window.register_main_view(
        PREP_STATUS_VIEW_ID,
        "Prep Status",
        lambda: EngineStatePrepView(runtime=window.runtime, parent=window.central_widget),
    )



__all__ = [
    "DOCKING_VIEW_ID",
    "PREP_STATUS_VIEW_ID",
    "DockingStudioWidget",
    "register_docking_workspace",
]
