"""The Activity table: measured endpoints per ligand, edited in place.

The QSAR side of the catalog — everything a model is trained against enters through here
or through the loaders it calls. Registered by `catalog.domain_views`.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from amdockvs.ui.common.async_query import run_async

_HEADERS = ("Ligand id", "Name", "Value", "Unit", "Subset")
_VALUE, _UNIT = 2, 3  # the editable columns


class _ActivityModel(QAbstractTableModel):
    """The activity rows as a model over a plain list: Qt asks only for the visible cells, so
    39k rows load in a blink (a QTableWidget built ~200k items on the GUI thread, ~2.4 s).
    Edits to Value/Unit go through ``write(row, value_text, unit_text) -> bool``."""

    def __init__(self, write, parent=None):
        super().__init__(parent)
        self._rows: list[list] = []  # [molecule_id, name, value | None, unit, subset]
        self._kind = "continuous"
        self._write = write

    def set_rows(self, rows: list[list], kind: str) -> None:
        self.beginResetModel()
        self._rows, self._kind = rows, kind
        self.endResetModel()

    def append(self, molecule_id: int, name: str, unit: str) -> None:
        at = len(self._rows)
        self.beginInsertRows(QModelIndex(), at, at)
        self._rows.append([int(molecule_id), name, None, unit, ""])
        self.endInsertRows()

    def molecule_id(self, row: int) -> int:
        return int(self._rows[row][0])

    def molecule_ids(self) -> set[int]:
        return {int(r[0]) for r in self._rows}

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt override
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt override
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802 - Qt override
        if role == Qt.DisplayRole:
            return _HEADERS[section] if orientation == Qt.Horizontal else str(section + 1)
        return None

    def _text(self, row: list, column: int) -> str:
        value = row[column]
        if column == _VALUE:
            if value is None:
                return ""
            # categorical endpoints show whole-number class labels; continuous show 4 decimals
            return str(int(round(float(value)))) if self._kind == "categorical" else f"{float(value):.4f}"
        return str(value if value is not None else "")

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if role in (Qt.DisplayRole, Qt.EditRole):
            return self._text(self._rows[index.row()], index.column())
        return None

    def flags(self, index):
        base = super().flags(index)
        return base | Qt.ItemIsEditable if index.column() in (_VALUE, _UNIT) else base

    def setData(self, index, text, role=Qt.EditRole) -> bool:  # noqa: N802 - Qt override
        if role != Qt.EditRole or index.column() not in (_VALUE, _UNIT):
            return False
        row = self._rows[index.row()]
        value_text = str(text) if index.column() == _VALUE else self._text(row, _VALUE)
        unit_text = str(text).strip() if index.column() == _UNIT else str(row[_UNIT] or "")
        if index.column() == _UNIT and row[_VALUE] is None:
            # Nothing to record yet, but the unit is the user's answer to "in what?": keep it on
            # the row, so it stays on screen and the value typed next is stored in it. Rejecting
            # the edit instead would revert the cell and silently record the combo's unit.
            row[_UNIT] = unit_text
            self.dataChanged.emit(index, index)
            return True
        if not self._write(index.row(), value_text, unit_text):
            return False
        row[_VALUE], row[_UNIT] = float(value_text), unit_text
        self.dataChanged.emit(self.index(index.row(), _VALUE), self.index(index.row(), _UNIT))
        return True



class LigandActivityWidget(QWidget):
    """Activity editor: pick ligands, type/normalize values, or bulk-load from CSV. Edits write
    straight through runtime.qsar.set_activity; the same table renders CSV-loaded values."""

    def __init__(self, *, runtime, open_results_view: Callable[[], None] | None = None,
                 show_histogram: Callable[[str, tuple], None] | None = None, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._open_results_view = open_results_view
        self._show_histogram = show_histogram
        self._in_flight = None  # (endpoint, model) of the refresh running now: a repeat is dropped

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

        self.model = _ActivityModel(self._write_activity, self)
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        outer.addWidget(self.table, 1)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.refresh_view()

    def _current_endpoint(self) -> str:
        return self.endpoint_combo.currentText().strip()

    def _current_model_id(self) -> int | None:
        return self.model_combo.currentData() if hasattr(self, "model_combo") else None

    def refresh_view(self) -> None:
        if not hasattr(self, "table"):
            return
        from amdockvs.ui.tools.qsar.chart import class_counts, histogram

        endpoint = self._current_endpoint() or None
        model_id = self._current_model_id()
        # Opening the view asks twice (the widget on creation, then the window as it shows the
        # chart): the second answer would be the first one again, so one fetch serves both.
        if self._in_flight == (endpoint, model_id):
            return
        self._in_flight = (endpoint, model_id)

        def work():
            # All heavy work off the GUI thread: resolve the endpoint (default to the first when
            # none is selected — never fetch every endpoint's rows), fetch its rows, AND bin the
            # histogram here so the GUI thread only renders ~10 bars + the rows.
            kinds = self.runtime.qsar.endpoint_kinds()
            resolved = endpoint or (sorted(kinds)[0] if kinds else None)
            rows = self.runtime.qsar.activity_rows(endpoint=resolved)
            models = self.runtime.qsar.list_models()
            subsets = self.runtime.qsar.model_subsets(model=model_id) if model_id else {}
            values = [r["value"] for r in rows if r["value"] is not None]
            # classes are counted per label, not binned along a continuous axis
            bins = class_counts(values) if kinds.get(resolved) == "categorical" else histogram(values)
            return kinds, resolved, rows, models, subsets, bins

        def done(payload) -> None:
            self._in_flight = None
            self._fill(payload)

        def failed(exc) -> None:
            self._in_flight = None
            self.status.setText(str(exc))

        run_async(work, done, on_error=failed, busy=self.table, busy_text="Loading activities…")

    def _fill(self, payload) -> None:
        kinds, resolved_endpoint, rows, models, subsets, bins = payload
        endpoints = sorted(kinds)
        self.endpoint_combo.blockSignals(True)
        self.endpoint_combo.clear()
        self.endpoint_combo.addItems(endpoints)
        if resolved_endpoint:
            self.endpoint_combo.setCurrentText(resolved_endpoint)
        self.endpoint_combo.blockSignals(False)
        kind = kinds.get(resolved_endpoint, "continuous")
        current_model = self._current_model_id()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem("(none)", None)
        for m in models:
            self.model_combo.addItem(f"#{m.id} {m.name}", int(m.id))
        idx = self.model_combo.findData(current_model)
        self.model_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_combo.blockSignals(False)
        unit = self.unit_combo.currentText().strip()
        self.model.set_rows([[row["molecule_id"], row["name"], row["value"], row["unit"] or unit,
                              subsets.get(row["molecule_id"], "")] for row in rows], kind)
        n_test = sum(1 for v in subsets.values() if v == "test")
        suffix = f" - {n_test} test / {len(subsets) - n_test} train" if subsets else ""
        self.status.setText(f"{len(rows)} activity row(s) for endpoint '{self._current_endpoint() or '(all)'}'{suffix}.")
        if self._show_histogram is not None:
            self._show_histogram(self._current_endpoint(), bins)

    def _write_activity(self, row: int, value_text: str, unit: str) -> bool:
        """An edited Value/Unit cell, written through; False keeps the cell as it was."""
        endpoint = self._current_endpoint()
        if not endpoint:
            QMessageBox.information(self, "Activities", "Set an endpoint first.")
            return False
        try:
            value = float(value_text)
        except (TypeError, ValueError):
            return False  # no value yet (a unit typed on a new row): nothing to record
        try:
            self.runtime.qsar.set_activity(ligand_id=self.model.molecule_id(row), endpoint=endpoint,
                                           value=value, unit=unit)
        except Exception as exc:
            QMessageBox.warning(self, "Activities", str(exc))
            return False
        return True

    def _add_ligands(self) -> None:
        from amdockvs.ui.tools.qsar.activities import pick_ligands

        chosen = pick_ligands(self, self.runtime)
        if not chosen:
            return
        existing = self.model.molecule_ids()
        unit = self.unit_combo.currentText().strip()
        for mid, name in chosen:
            if mid not in existing:
                self.model.append(mid, name, unit)
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
        ids = sorted({self.model.molecule_id(idx.row()) for idx in self.table.selectionModel().selectedRows()})
        if not ids:
            return
        for ligand_id in ids:
            try:
                self.runtime.qsar.delete_activity(ligand_id=ligand_id, endpoint=endpoint)
            except Exception as exc:
                QMessageBox.warning(self, "Activities", str(exc))
                break
        self.refresh_view()
