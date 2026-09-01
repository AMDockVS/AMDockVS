"""Redocking launcher — re-dock reference complexes (purpose="redocking") to validate that the
docking protocol reproduces their known poses.

The redocking backend already exists (DockingAPI.redock + redocking_job + the engine's
run_kind="redocking" path); this view is just the thin, off-thread launcher. Rescoring
(score-only) is intentionally not here — it needs an engine path that doesn't exist yet.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.docking.programs import VINA_PROGRAM
from amdockvs.ui.tools.docking.redocking_charts import RedockingChartsPanel
from amdockvs.summaries import DockingHitSummary
from amdockvs.ui.async_query import run_async

REDOCKING_VIEW_ID = "workspace.redocking"


class RedockingWidget(QWidget):
    data_refreshed = Signal(bool)

    def __init__(
        self,
        *,
        runtime,
        load_hit_in_pymol: Callable[[DockingHitSummary, int], None] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.runtime = runtime
        self._load_hit_in_pymol = load_hit_in_pymol
        self._rows: list[DockingHitSummary] = []
        self._data_signature = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 5, 0, 5)

        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to run redocking.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return

        intro = QLabel(
            "Inspect redocking validation results. Execution is launched from Docking Studio "
            "step 4 by selecting Experiment = Redocking.",
            self,
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        self.count_label = QLabel("Checking available complexes…", self)
        self.count_label.setWordWrap(True)
        outer.addWidget(self.count_label)

        box = QGroupBox("Settings", self)
        form = QFormLayout(box)
        self.exhaustiveness = self._spin(1, 512, 8)
        self.num_modes = self._spin(1, 100, 9)
        form.addRow("Exhaustiveness", self.exhaustiveness)
        form.addRow("Poses (num_modes)", self.num_modes)
        outer.addWidget(box)
        box.setVisible(False)

        run_row = QHBoxLayout()
        self.run_button = QPushButton("Run Redocking", self)
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self._run)
        run_row.addWidget(self.run_button)
        self.run_button.setVisible(False)
        self.add_workflow_button = QPushButton("Save to workflow", self)
        self.add_workflow_button.setToolTip("Save this redocking (current settings) as a step in the active workflow — updates the existing redocking step if there is one.")
        self.add_workflow_button.clicked.connect(self._add_to_workflow)
        run_row.addWidget(self.add_workflow_button)
        self.add_workflow_button.setVisible(False)
        outer.addLayout(run_row)

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        results_box = QGroupBox("Redocking Results", self)
        results_layout = QVBoxLayout(results_box)
        result_actions = QHBoxLayout()
        self.refresh_results_button = QPushButton("Refresh results", results_box)
        self.refresh_results_button.clicked.connect(self._refresh_results)
        self.view_selected_button = QPushButton("View selected", results_box)
        self.view_selected_button.clicked.connect(self._view_selected)
        result_actions.addWidget(self.refresh_results_button)
        result_actions.addWidget(self.view_selected_button)
        result_actions.addStretch(1)
        results_layout.addLayout(result_actions)
        self.results_table = QTableWidget(0, 9, results_box)
        self.results_table.setHorizontalHeaderLabels(["Receptor", "Ligand", "P#", "Protocol", "Pose", "RMSD", "Method", "Score", "Complex"])
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.itemSelectionChanged.connect(self._view_selected)
        self.results_tabs = QTabWidget(results_box)
        self.results_tabs.addTab(self.results_table, "Table")
        self.charts_panel = RedockingChartsPanel(results_box)
        self.results_tabs.addTab(self.charts_panel, "Analysis")
        results_layout.addWidget(self.results_tabs, 1)
        self.results_status = QLabel("No redocking results loaded.", results_box)
        self.results_status.setWordWrap(True)
        results_layout.addWidget(self.results_status)
        outer.addWidget(results_box, 1)
        self.refresh()
        self._refresh_results()

    @staticmethod
    def _spin(low: int, high: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        return spin

    def refresh(self) -> None:
        run_async(
            lambda: self.runtime.complexes.count(purpose="redocking,reference"),
            self._apply_count,
            on_error=lambda _exc: self._apply_count(None),
        )

    def _apply_count(self, count: int | None) -> None:
        if count is None:
            self.count_label.setText("Could not count redocking complexes.")
            self.run_button.setEnabled(False)
            return
        self.count_label.setText(f"{count} reference/redocking complex(es) available.")
        self.run_button.setEnabled(count > 0)

    def _run(self) -> None:
        params = {
            "exhaustiveness": int(self.exhaustiveness.value()),
            "num_modes": int(self.num_modes.value()),
            "executor_name": DEFAULT_LOCAL_CPU_EXECUTOR,
        }
        self.run_button.setEnabled(False)
        self.status_label.setText("Submitting redocking job…")
        run_async(lambda: self._submit(params), self._on_submitted, on_error=self._on_error, busy=self)

    def _submit(self, params: dict) -> str:
        # redock() runs its own requirement check (materializes the complex rows) — heavy, hence
        # off-thread. complex_set=None redocks every complex of the given purpose.
        return self.runtime.docking.redock(
            program=VINA_PROGRAM.key,
            complex_set=None,
            purpose="redocking,reference",
            exhaustiveness=params["exhaustiveness"],
            num_modes=params["num_modes"],
            executor_name=params["executor_name"],
        )

    def _add_to_workflow(self) -> None:
        from amdockvs.ui.tools.workflow_panel import save_to_workflow

        params = {
            "exhaustiveness": int(self.exhaustiveness.value()),
            "num_modes": int(self.num_modes.value()),
            "executor_name": DEFAULT_LOCAL_CPU_EXECUTOR,
        }
        save_to_workflow(
            self.window(),
            kind="redocking",
            name=f"Redock (exh={params['exhaustiveness']}, modes={params['num_modes']})",
            category="docking",
            submit=lambda _rt, p=params: self._submit(p),
        )

    def _on_submitted(self, job_id: str) -> None:
        self.run_button.setEnabled(True)
        self.status_label.setText(f"Redocking job submitted: {job_id}")
        self._refresh_results()

    def _on_error(self, exc: Exception) -> None:
        self.run_button.setEnabled(True)
        self.status_label.setText("")
        QMessageBox.warning(self, "Redocking", str(exc))

    @staticmethod
    def _pose_rank(hit: DockingHitSummary) -> int:
        try:
            return int(hit.metadata.get("pose_index", 0)) + 1
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _fmt(value: float | None, decimals: int = 2) -> str:
        return "-" if value is None else f"{float(value):.{decimals}f}"

    def _refresh_results(self) -> None:
        run_async(
            self._fetch_results,
            self._apply_results,
            on_error=lambda exc: self._apply_results({"error": str(exc)}),
        )

    def _fetch_results(self) -> list[DockingHitSummary]:
        # RMSD (and its match method) is computed and persisted by the engine against the
        # frozen reference ligand, so just read it back — no recompute on the GUI thread.
        return self.runtime.docking.filtered_hits(
            limit=5000,
            only_completed=False,
            run_kind="redocking",
        )

    _RMSD_SUCCESS_ANGSTROM = 2.0

    @staticmethod
    def _rmsd_method(hit: DockingHitSummary) -> str:
        return str(hit.metadata.get("rmsd_method") or "") if hit.metadata else ""

    @staticmethod
    def _protocol_key(hit: DockingHitSummary) -> tuple[str, str]:
        return (
            str(hit.protocol_label or hit.engine or "").lower(),
            str(hit.protocol_hash or ""),
        )

    def _assign_protocol_ids(self, rows) -> dict[tuple[str, str], int]:
        # Stable, simple 1..N id per distinct protocol so results can be sorted by protocol and
        # stay in that order across refreshes (ordered by label then hash).
        keys = sorted({self._protocol_key(hit) for hit in rows})
        return {key: index for index, key in enumerate(keys, start=1)}

    def _apply_results(self, rows) -> None:
        if isinstance(rows, dict) and rows.get("error"):
            self._rows = []
            self.results_table.setRowCount(0)
            self.results_status.setText(str(rows["error"]))
            self.data_refreshed.emit(False)
            return
        rows = list(rows or [])
        signature = tuple(sorted(
            (
                int(hit.result_id or 0),
                int(hit.receptor_id),
                int(hit.ligand_id),
                self._pose_rank(hit),
                float(hit.score),
                repr(hit.rmsd_vs_reference),
                str(hit.updated_at or ""),
            )
            for hit in rows
        ))
        changed = signature != self._data_signature
        self._data_signature = signature
        self._protocol_ids = self._assign_protocol_ids(rows)
        self._rows = sorted(
            rows,
            key=lambda hit: (
                self._protocol_ids.get(self._protocol_key(hit), 0),
                str(hit.receptor_name or f"Receptor {hit.receptor_id}").lower(),
                str(hit.ligand_name or f"Ligand {hit.ligand_id}").lower(),
                self._pose_rank(hit),
                int(hit.result_id or 0),
            ),
        )
        self.results_table.setRowCount(len(self._rows))
        for row_index, hit in enumerate(self._rows):
            receptor = QTableWidgetItem(hit.receptor_name or f"Receptor {hit.receptor_id}")
            receptor.setData(Qt.UserRole, row_index)
            ligand = QTableWidgetItem(hit.ligand_name or f"Ligand {hit.ligand_id}")
            ligand.setData(Qt.UserRole, row_index)
            self.results_table.setItem(row_index, 0, receptor)
            self.results_table.setItem(row_index, 1, ligand)
            self.results_table.setItem(row_index, 2, QTableWidgetItem(f"P{self._protocol_ids.get(self._protocol_key(hit), 0)}"))
            self.results_table.setItem(row_index, 3, QTableWidgetItem(hit.protocol_label or hit.engine or "-"))
            self.results_table.setItem(row_index, 4, QTableWidgetItem(str(self._pose_rank(hit))))
            rmsd_item = QTableWidgetItem(self._fmt(hit.rmsd_vs_reference))
            if hit.rmsd_vs_reference is not None:
                good = float(hit.rmsd_vs_reference) <= self._RMSD_SUCCESS_ANGSTROM
                rmsd_item.setBackground(QColor(46, 125, 50) if good else QColor(183, 28, 28))
                rmsd_item.setForeground(QColor("white"))
            self.results_table.setItem(row_index, 5, rmsd_item)
            self.results_table.setItem(row_index, 6, QTableWidgetItem(self._rmsd_method(hit) or "-"))
            self.results_table.setItem(row_index, 7, QTableWidgetItem(self._fmt(hit.score)))
            self.results_table.setItem(row_index, 8, QTableWidgetItem("-" if hit.complex_id is None else str(hit.complex_id)))
        self.results_table.resizeColumnsToContents()
        self.charts_panel.set_records(self._build_records())
        self.results_status.setText(f"{len(self._rows)} redocking pose(s) loaded.")
        self.data_refreshed.emit(changed)

    refresh_results_view = _refresh_results

    def _build_records(self) -> list[dict]:
        # Flatten the loaded hits into the plain records redocking_metrics expects. A "case" is one
        # redocked complex (fall back to receptor+ligand when there's no complex_id); protocol is
        # carried in the case key too so identical complexes under different protocols stay distinct.
        records: list[dict] = []
        for hit in self._rows:
            pid = self._protocol_ids.get(self._protocol_key(hit), 0)
            protocol = f"P{pid} · {hit.protocol_label or hit.engine or '-'}"
            case = hit.complex_id if hit.complex_id is not None else (hit.receptor_id, hit.ligand_id)
            records.append(
                {
                    "protocol": protocol,
                    "case": (protocol, case),
                    "rank": self._pose_rank(hit),
                    "rmsd": None if hit.rmsd_vs_reference is None else float(hit.rmsd_vs_reference),
                    "score": None if hit.score is None else float(hit.score),
                }
            )
        return records

    def _view_selected(self) -> None:
        if self._load_hit_in_pymol is None:
            return
        indexes = self.results_table.selectionModel().selectedRows()
        if not indexes:
            return
        item = self.results_table.item(indexes[0].row(), 0)
        row_index = int(item.data(Qt.UserRole) or 0) if item is not None else indexes[0].row()
        if row_index < 0 or row_index >= len(self._rows):
            return
        hit = self._rows[row_index]
        self._load_hit_in_pymol(hit, self._pose_rank(hit))


def register_redocking_workspace(window) -> None:
    window.register_main_view(
        REDOCKING_VIEW_ID,
        "Redocking Results",
        lambda: RedockingWidget(
            runtime=window.runtime,
            load_hit_in_pymol=getattr(window, "load_hit_in_pymol", None),
            parent=window.central_widget,
        ),
    )


__all__ = ["REDOCKING_VIEW_ID", "RedockingWidget", "register_redocking_workspace"]
