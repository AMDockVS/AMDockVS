"""The Activity table: measured endpoints per ligand, edited in place.

The QSAR side of the catalog — everything a model is trained against enters through here
or through the loaders it calls. Registered by `catalog.domain_views`.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.ui.common.async_query import run_async



class LigandActivityWidget(QWidget):
    """Activity editor: pick ligands, type/normalize values, or bulk-load from CSV. Edits write
    straight through runtime.qsar.set_activity; the same table renders CSV-loaded values."""

    def __init__(self, *, runtime, open_results_view: Callable[[], None] | None = None,
                 show_histogram: Callable[[str, tuple], None] | None = None, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._open_results_view = open_results_view
        self._show_histogram = show_histogram
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to edit ligand activities.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return

        from amdockvs.ui.tools.qsar.activities import TRANSFORM_CHOICES, UNIT_CHOICES

        self.endpoint_combo = QComboBox(self)
        self.endpoint_combo.setEditable(True)
        self.endpoint_combo.setMinimumWidth(140)
        self.endpoint_combo.currentTextChanged.connect(lambda _t: self.refresh_view())
        self.unit_combo = QComboBox(self)
        self.unit_combo.setEditable(True)
        self.unit_combo.addItems(UNIT_CHOICES)
        self.transform_combo = QComboBox(self)
        self.transform_combo.addItems(TRANSFORM_CHOICES)
        self.model_combo = QComboBox(self)
        self.model_combo.setMinimumWidth(160)
        self.model_combo.setToolTip("Show which train/test subset each ligand fell into for this model.")
        self.model_combo.currentIndexChanged.connect(lambda _i: self.refresh_view())
        add_btn = QPushButton("Add ligands…", self)
        add_btn.clicked.connect(self._add_ligands)
        csv_btn = QPushButton("Load from CSV…", self)
        csv_btn.clicked.connect(self._load_csv)
        matrix_btn = QPushButton("Load matrix…", self)
        matrix_btn.setToolTip("Load many endpoints at once from a wide CSV (e.g. Tox21's 12 assays). "
                              "Import the ligands first, then match by name/structure.")
        matrix_btn.clicked.connect(self._load_matrix)
        norm_btn = QPushButton("Normalize to pIC50", self)
        norm_btn.setToolTip("Re-load the current endpoint applying the selected unit + transform.")
        norm_btn.clicked.connect(self._normalize)
        del_btn = QPushButton("Delete selected", self)
        del_btn.clicked.connect(self._delete_selected)
        # Two rows so the toolbar's minimum width is ~half — otherwise a single long row forces a
        # large minimum width on this widget and locks every dock to its right (PyMOL, the chart)
        # at that width. Row 1: action buttons (right). Row 2: endpoint controls (left).
        buttons_row = QHBoxLayout()
        buttons_row.addStretch(1)
        for w in (add_btn, csv_btn, matrix_btn, norm_btn, del_btn):
            buttons_row.addWidget(w)
        outer.addLayout(buttons_row)

        controls_row = QHBoxLayout()
        for w in (QLabel("Endpoint:"), self.endpoint_combo, QLabel("Unit:"), self.unit_combo,
                  QLabel("Transform:"), self.transform_combo, QLabel("Subset for model:"), self.model_combo):
            controls_row.addWidget(w)
        controls_row.addStretch(1)
        outer.addLayout(controls_row)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Ligand id", "Name", "Value", "Unit", "Subset"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_item_changed)
        outer.addWidget(self.table, 1)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.refresh_view()

    def _current_endpoint(self) -> str:
        return self.endpoint_combo.currentText().strip()

    def _current_model_id(self) -> int | None:
        return self.model_combo.currentData() if hasattr(self, "model_combo") else None

    def _fmt_value(self, value) -> str:
        """Categorical endpoints show whole-number class labels; continuous show 4 decimals."""
        if value is None:
            return ""
        if getattr(self, "_current_kind", "continuous") == "categorical":
            return str(int(round(float(value))))
        return f"{float(value):.4f}"

    def _set_row(self, r: int, mid: int, name: str, value, unit: str, subset: str = "") -> None:
        id_item = QTableWidgetItem(str(mid))
        id_item.setFlags(id_item.flags() & ~Qt.ItemIsEditable)
        name_item = QTableWidgetItem(name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
        subset_item = QTableWidgetItem(subset)
        subset_item.setFlags(subset_item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(r, 0, id_item)
        self.table.setItem(r, 1, name_item)
        self.table.setItem(r, 2, QTableWidgetItem(self._fmt_value(value)))
        self.table.setItem(r, 3, QTableWidgetItem(unit or self.unit_combo.currentText().strip()))
        self.table.setItem(r, 4, subset_item)

    def refresh_view(self) -> None:
        if not hasattr(self, "table"):
            return
        from amdockvs.ui.tools.qsar.chart import histogram

        endpoint = self._current_endpoint() or None
        model_id = self._current_model_id()

        def work():
            # All heavy work off the GUI thread: resolve the endpoint (default to the first when
            # none is selected — never fetch every endpoint's rows), fetch its rows, AND bin the
            # histogram here so the GUI thread only renders ~10 bars + the rows.
            kinds = self.runtime.qsar.endpoint_kinds()
            resolved = endpoint or (sorted(kinds)[0] if kinds else None)
            rows = self.runtime.qsar.activity_rows(endpoint=resolved)
            models = self.runtime.qsar.list_models()
            subsets = self.runtime.qsar.model_subsets(model=model_id) if model_id else {}
            bins = histogram([r["value"] for r in rows if r["value"] is not None])
            return kinds, resolved, rows, models, subsets, bins

        run_async(
            work,
            self._fill,
            on_error=lambda exc: self.status.setText(str(exc)),
            busy=self.table,
        )

    def _fill(self, payload) -> None:
        kinds, resolved_endpoint, rows, models, subsets, bins = payload
        endpoints = sorted(kinds)
        self._loading = True
        self.endpoint_combo.blockSignals(True)
        self.endpoint_combo.clear()
        self.endpoint_combo.addItems(endpoints)
        if resolved_endpoint:
            self.endpoint_combo.setCurrentText(resolved_endpoint)
        self.endpoint_combo.blockSignals(False)
        self._current_kind = kinds.get(resolved_endpoint, "continuous")
        current_model = self._current_model_id()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem("(none)", None)
        for m in models:
            self.model_combo.addItem(f"#{m.id} {m.name}", int(m.id))
        idx = self.model_combo.findData(current_model)
        self.model_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_combo.blockSignals(False)
        # blockSignals: every setItem would otherwise fire itemChanged (N*5 emissions) and lag hard.
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self._set_row(r, row["molecule_id"], row["name"], row["value"], row["unit"],
                          subsets.get(row["molecule_id"], ""))
        self.table.setUpdatesEnabled(True)
        self.table.blockSignals(False)
        self._loading = False
        n_test = sum(1 for v in subsets.values() if v == "test")
        suffix = f" — {n_test} test / {len(subsets) - n_test} train" if subsets else ""
        self.status.setText(f"{len(rows)} activity row(s) for endpoint '{self._current_endpoint() or '(all)'}'{suffix}.")
        if self._show_histogram is not None:
            self._show_histogram(self._current_endpoint(), bins)

    def _on_item_changed(self, item) -> None:
        if self._loading or item.column() not in (2, 3):
            return
        endpoint = self._current_endpoint()
        if not endpoint:
            QMessageBox.information(self, "Activities", "Set an endpoint first.")
            return
        try:
            ligand_id = int(self.table.item(item.row(), 0).text())
            value = float(self.table.item(item.row(), 2).text())
        except (TypeError, ValueError):
            return
        unit = (self.table.item(item.row(), 3).text() if self.table.item(item.row(), 3) else "").strip()
        try:
            self.runtime.qsar.set_activity(ligand_id=ligand_id, endpoint=endpoint, value=value, unit=unit)
        except Exception as exc:
            QMessageBox.warning(self, "Activities", str(exc))

    def _add_ligands(self) -> None:
        from amdockvs.ui.tools.qsar.activities import pick_ligands

        chosen = pick_ligands(self, self.runtime)
        if not chosen:
            return
        self._loading = True
        existing = {int(self.table.item(r, 0).text()) for r in range(self.table.rowCount())}
        for mid, name in chosen:
            if mid in existing:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            self._set_row(r, mid, name, None, "")
        self._loading = False
        self.status.setText("Enter a value in the Value column to record each activity.")

    def _load_csv(self) -> None:
        from amdockvs.ui.tools.qsar.activities import load_activities_dialog

        params = load_activities_dialog(self, current_endpoint=self._current_endpoint())
        if not params:
            return
        self.status.setText("Loading activities…")
        run_async(
            lambda: self.runtime.qsar.load_activities(**params),
            self._on_csv_loaded,
            on_error=lambda exc: self.status.setText(f"Load failed: {exc}"),
            busy=self.table,
        )

    def _on_csv_loaded(self, result: dict) -> None:
        self.endpoint_combo.setCurrentText(result["endpoint"])
        self.status.setText(
            f"Loaded {result['loaded']} activities (matched by {result['match_by']}); "
            f"skipped {result['skipped_missing_ligand']} unmatched, {result['skipped_invalid_value']} invalid."
        )
        self.refresh_view()

    def _load_matrix(self) -> None:
        from amdockvs.ui.tools.qsar.activities import map_activity_columns_dialog

        params = map_activity_columns_dialog(self)
        if not params:
            return
        self.status.setText("Loading activity columns…")
        run_async(
            lambda: self.runtime.qsar.load_activity_matrix(**params),
            self._on_matrix_loaded,
            on_error=lambda exc: self.status.setText(f"Load failed: {exc}"),
            busy=self.table,
        )

    def _on_matrix_loaded(self, result: dict) -> None:
        n_cat = sum(1 for k in result["kinds"].values() if k == "categorical")
        self.status.setText(
            f"Loaded {result['loaded']} activities across {len(result['endpoints'])} endpoints "
            f"({n_cat} categorical)."
        )
        if result["endpoints"]:
            self.endpoint_combo.setCurrentText(result["endpoints"][0])
        self.refresh_view()

    def _normalize(self) -> None:
        QMessageBox.information(
            self, "Normalize",
            "Use 'Load from CSV…' with a Transform set to pIC50 to normalize a concentration table. "
            "Manually-typed values are stored as-is in the chosen unit.",
        )

    def _delete_selected(self) -> None:
        endpoint = self._current_endpoint() or None
        ids = sorted({int(self.table.item(idx.row(), 0).text()) for idx in self.table.selectedIndexes()})
        if not ids:
            return
        for ligand_id in ids:
            try:
                self.runtime.qsar.delete_activity(ligand_id=ligand_id, endpoint=endpoint)
            except Exception as exc:
                QMessageBox.warning(self, "Activities", str(exc))
                break
        self.refresh_view()
