"""Workflow view — a visual editor + monitor with a library of predefined workflows.

Left: a library of predefined workflows (double-click to open one). Right: the editor/monitor —
a top→down DAG canvas. A workflow opens read-only (monitor); pressing Edit unlocks it (the runner
already holds an editable copy, so a preset template is never mutated). In edit mode every node
grows a "＋" to add a child step connected to it (branching), and "＋ Add root step" adds an
independent branch.

Two run modes:
  * Automatic (unattended): hand the whole DAG to MolSuite via materialize() — job steps run in
    parallel/dependency order and survive the app closing. Interactive steps (docking) are
    pre-configured from their own panel's "Save to workflow".
  * Guided (step by step): walk a linear route one step at a time. Job steps (chemistry, prepare)
    submit and auto-advance on completion; manual steps (import, docking) open their real panel with
    data present, and you press "Mark done ▸" to advance. This is the frequent AMDock-v1-style case.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QCursor, QFont, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from amdockvs.orchestrator import STEP_NEEDS_CONFIG, STEP_RUNNING, WorkflowStep
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.workflow_steps import (
    DOCKING_VIEW_ID,
    PRESET_WORKFLOWS,
    STEP_SPECS,
    build_preset,
    build_route,
    make_step,
)

WORKFLOW_VIEW_ID = "workspace.workflow"

_IMPORT_KINDS = ("import_ligands", "import_receptors")

_STATUS_COLORS = {
    "needs_config": "#c98a2b",
    "pending": "#6b7280",
    "running": "#2f7bd6",
    "completed": "#3fbf73",
    "failed": "#d9776c",
    "skipped": "#9ca3af",
}


class WorkflowGraphView(QWidget):
    """Paints the workflow to fill the viewport. A LINEAR chain (a guided route) is drawn as a
    boustrophedon "snake": rows fill left→right, then right→left, wrapping to use the full width with
    big nodes and orthogonal connectors. A BRANCHED DAG (auto mode) falls back to a top→down layered
    layout. Clicking a node selects it; in edit mode a branched DAG shows a "＋" to add a child."""

    node_clicked = Signal(str)
    node_double_clicked = Signal(str)
    add_child_requested = Signal(str)  # step_id the new child should depend on

    _NODE_W = 168
    _NODE_H = 46
    _H_GAP = 26  # between siblings on a row
    _V_GAP = 44  # between layers (leaves room for the ＋ badge)
    _MARGIN = 16
    _PLUS_R = 10
    _MIN_SCALE = 0.5  # scale down to fit; below this we stop and let it scroll
    # serpentine: aim for wide-ish cells, cap node size so a short route doesn't get absurdly huge
    _SNAKE_MIN_CELL_W = 200
    _SNAKE_MAX_W = 240
    _SNAKE_MAX_H = 96
    _SNAKE_H_GAP = 20
    _SNAKE_V_GAP = 34

    def __init__(self, runner, parent=None):
        super().__init__(parent)
        self.runner = runner
        self.editable = False
        self._rects: dict[str, QRectF] = {}       # content coords (pre-scale)
        self._plus_rects: dict[str, QRectF] = {}
        self._snake_order: list[str] | None = None  # set when the graph is a linear chain
        self._selected: str | None = None
        self._scale = 1.0
        self._offset = (0.0, 0.0)
        self.setMinimumHeight(200)

    def set_selected(self, step_id: str | None) -> None:
        self._selected = step_id
        self.update()

    def _linear_order(self) -> list[str] | None:
        """The steps in chain order if the explicit-dependency graph is a single linear chain
        (each node ≤1 predecessor and ≤1 successor, one head, all connected), else None."""
        steps = self.runner.steps
        if len(steps) < 2:
            return None
        ids = {s.step_id for s in steps}
        succ: dict[str, list[str]] = {}
        pred: dict[str, list[str]] = {}
        for src, dst in self.runner.explicit_edges():
            succ.setdefault(src, []).append(dst)
            pred.setdefault(dst, []).append(src)
        if any(len(succ.get(sid, [])) > 1 or len(pred.get(sid, [])) > 1 for sid in ids):
            return None
        heads = [s.step_id for s in steps if not pred.get(s.step_id)]
        if len(heads) != 1:
            return None
        order: list[str] = []
        cur, seen = heads[0], set()
        while cur is not None and cur not in seen:
            order.append(cur)
            seen.add(cur)
            nxt = succ.get(cur, [])
            cur = nxt[0] if nxt else None
        return order if len(order) == len(ids) else None

    def _layout(self) -> dict[str, tuple[int, float]]:
        """step_id -> (level, x). Level runs DOWN the canvas; x is a GLOBAL column coordinate a
        child inherits from its parent (barycenter for joins), so a branch stays under its parent —
        a lone child doesn't collapse to column 0. Parentless nodes take the next free column, and
        same-level overlaps are pushed apart while preserving order (keeps edges from crossing)."""
        levels = self.runner.explicit_levels()
        parents: dict[str, list[str]] = {}
        for src, dst in self.runner.explicit_edges():
            parents.setdefault(dst, []).append(src)
        by_level: dict[int, list[str]] = {}
        for step in self.runner.steps:  # insertion order = stable tiebreak
            by_level.setdefault(levels.get(step.step_id, 0), []).append(step.step_id)
        x_of: dict[str, float] = {}
        for level in sorted(by_level):
            ids = by_level[level]
            order = {sid: i for i, sid in enumerate(ids)}
            raw: dict[str, float | None] = {}
            for sid in ids:
                pcols = [x_of[p] for p in parents.get(sid, []) if p in x_of]
                raw[sid] = sum(pcols) / len(pcols) if pcols else None
            # parented first (by inherited x), then parentless; pack left→right with min spacing 1
            ordered = sorted(ids, key=lambda sid: (raw[sid] is None, raw[sid] if raw[sid] is not None else order[sid], order[sid]))
            prev: float | None = None
            for sid in ordered:
                x = raw[sid] if raw[sid] is not None else (prev + 1 if prev is not None else 0.0)
                if prev is not None and x < prev + 1:
                    x = prev + 1
                x_of[sid] = x
                prev = x
        return {sid: (levels.get(sid, 0), x_of[sid]) for sid in x_of}

    def _recompute(self) -> None:
        self._snake_order = self._linear_order()
        if self._snake_order is not None:
            self._recompute_serpentine(self._snake_order)
        else:
            self._recompute_layered()

    def _avail(self) -> tuple[int, int]:
        size = self.parentWidget().size() if self.parentWidget() is not None else self.size()
        return max(1, size.width()), max(1, size.height())

    def _recompute_serpentine(self, order: list[str]) -> None:
        """Wrap the linear chain into a snake that fills the viewport: pick columns from the width,
        rows from the count, size big nodes to the resulting cells (capped), place row 0 left→right,
        row 1 right→left, etc. Fills the area exactly, so no scaling/scroll."""
        avail_w, avail_h = self._avail()
        m = self._MARGIN
        n = len(order)
        usable_w = max(1, avail_w - 2 * m)
        cols = max(1, min(n, int(usable_w // self._SNAKE_MIN_CELL_W)))
        rows = max(1, math.ceil(n / cols))
        cell_w = (avail_w - 2 * m) / cols
        cell_h = (avail_h - 2 * m) / rows
        node_w = max(80.0, min(self._SNAKE_MAX_W, cell_w - self._SNAKE_H_GAP))
        node_h = max(40.0, min(self._SNAKE_MAX_H, cell_h - self._SNAKE_V_GAP))
        self._rects = {}
        for i, sid in enumerate(order):
            row = i // cols
            in_row = i % cols
            col = in_row if row % 2 == 0 else (cols - 1 - in_row)  # boustrophedon
            x = m + col * cell_w + (cell_w - node_w) / 2
            y = m + row * cell_h + (cell_h - node_h) / 2
            self._rects[sid] = QRectF(x, y, node_w, node_h)
        self._plus_rects = {}  # a route grows via "Add root step" (chains); no per-node branching
        self._scale = 1.0
        self._offset = (0.0, 0.0)
        self.setMinimumSize(0, 0)

    def _recompute_layered(self) -> None:
        pos = self._layout()
        max_level = max((lvl for lvl, _ in pos.values()), default=0)
        max_x = max((x for _, x in pos.values()), default=0.0)
        content_w = self._MARGIN * 2 + max_x * (self._NODE_W + self._H_GAP) + self._NODE_W
        content_h = self._MARGIN * 2 + (max_level + 1) * self._NODE_H + max_level * self._V_GAP
        self._rects = {
            step_id: QRectF(
                self._MARGIN + x * (self._NODE_W + self._H_GAP),
                self._MARGIN + level * (self._NODE_H + self._V_GAP),
                self._NODE_W,
                self._NODE_H,
            )
            for step_id, (level, x) in pos.items()
        }
        self._plus_rects = {}
        if self.editable:
            for step_id, rect in self._rects.items():
                cx, cy = rect.center().x(), rect.bottom() + self._V_GAP / 2
                self._plus_rects[step_id] = QRectF(
                    cx - self._PLUS_R, cy - self._PLUS_R, self._PLUS_R * 2, self._PLUS_R * 2
                )

        # fit-to-viewport: scale the whole DAG down to the available area (never up); below the
        # floor, stop scaling and let the scroll area take over.
        avail = self.parentWidget().size() if self.parentWidget() is not None else self.size()
        avail_w, avail_h = max(1, avail.width()), max(1, avail.height())
        fit = min(1.0, avail_w / content_w, avail_h / content_h)
        self._scale = max(self._MIN_SCALE, fit)
        scaled_w, scaled_h = content_w * self._scale, content_h * self._scale
        if fit >= self._MIN_SCALE:
            self.setMinimumSize(0, 0)  # fill viewport, paint scaled to fit (no scroll)
        else:
            self.setMinimumSize(int(scaled_w), int(scaled_h))  # floored -> scroll
        # centre the content when there's slack
        self._offset = (max(0.0, (self.width() - scaled_w) / 2), max(0.0, (self.height() - scaled_h) / 2))

    def refresh(self) -> None:
        self._recompute()
        self.update()

    def _to_content(self, pos):
        ox, oy = self._offset
        s = self._scale or 1.0
        return (pos.x() - ox) / s, (pos.y() - oy) / s

    def paintEvent(self, _event) -> None:
        self._recompute()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if not self.runner.steps:
            painter.setPen(QPen(QColor("#9ca3af")))
            hint = "Press Edit and add a step, or open a workflow from the library"
            painter.drawText(self.rect(), Qt.AlignCenter, hint)
            return
        painter.translate(*self._offset)
        painter.scale(self._scale, self._scale)
        steps_by_id = {s.step_id: s for s in self.runner.steps}

        # edges first (under the nodes)
        painter.setPen(QPen(QColor("#7c8aa0"), 1.8))
        if self._snake_order is not None:
            self._paint_snake_edges(painter, self._snake_order)
        else:
            for src_id, dst_id in self.runner.explicit_edges():  # parent bottom -> child top
                src, dst = self._rects.get(src_id), self._rects.get(dst_id)
                if src is None or dst is None:
                    continue
                x1, y1 = src.center().x(), src.bottom()
                x2, y2 = dst.center().x(), dst.top()
                path = QPainterPath()
                path.moveTo(x1, y1)
                mid = (y1 + y2) / 2
                path.cubicTo(x1, mid, x2, mid, x2, y2)
                painter.drawPath(path)

        # nodes
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        for step_id, rect in self._rects.items():
            step = steps_by_id.get(step_id)
            if step is None:
                continue
            color = QColor(_STATUS_COLORS.get(step.status, "#6b7280"))
            painter.setBrush(QBrush(color))
            pen = QPen(QColor("#f5f7fa") if step_id == self._selected else color.darker(140))
            pen.setWidth(3 if step_id == self._selected else 1)
            painter.setPen(pen)
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QPen(QColor("#0f1115") if step.status in ("completed", "running") else QColor("#f5f7fa")))
            name = step.name if len(step.name) <= 26 else step.name[:25] + "…"
            half = rect.height() / 2
            painter.drawText(rect.adjusted(10, 0, -10, int(-half)), Qt.AlignLeft | Qt.AlignBottom, name)
            painter.drawText(rect.adjusted(10, int(half), -10, 0), Qt.AlignLeft | Qt.AlignTop, step.status)

        # "＋" add-child badges (edit mode only)
        for prect in self._plus_rects.values():
            painter.setBrush(QBrush(QColor("#2f7bd6")))
            painter.setPen(QPen(QColor("#2f7bd6").darker(140), 1))
            painter.drawEllipse(prect)
            painter.setPen(QPen(QColor("#f5f7fa"), 2))
            c = prect.center()
            painter.drawLine(c.x() - 4, c.y(), c.x() + 4, c.y())
            painter.drawLine(c.x(), c.y() - 4, c.x(), c.y() + 4)

    def _paint_snake_edges(self, painter, order: list[str]) -> None:
        """Orthogonal connectors following the snake: horizontal between neighbours in a row, a short
        vertical U-turn between rows. A small arrowhead at the far end shows the flow direction."""
        for a, b in zip(order, order[1:]):
            ra, rb = self._rects.get(a), self._rects.get(b)
            if ra is None or rb is None:
                continue
            if abs(ra.center().y() - rb.center().y()) < 1.0:  # same row -> horizontal
                cy = ra.center().y()
                if rb.center().x() > ra.center().x():
                    x1, x2 = ra.right(), rb.left()
                else:
                    x1, x2 = ra.left(), rb.right()
                self._arrow(painter, x1, cy, x2, cy)
            else:  # U-turn to the next row (same column) -> vertical
                cx = ra.center().x()
                self._arrow(painter, cx, ra.bottom(), rb.center().x(), rb.top())

    @staticmethod
    def _arrow(painter, x1, y1, x2, y2) -> None:
        painter.drawLine(x1, y1, x2, y2)
        ang = math.atan2(y2 - y1, x2 - x1)
        size = 7.0
        for da in (math.radians(150), math.radians(-150)):
            painter.drawLine(x2, y2, x2 + size * math.cos(ang + da), y2 + size * math.sin(ang + da))

    def _node_at(self, pos) -> str | None:
        cx, cy = self._to_content(pos)
        for step_id, rect in self._rects.items():
            if rect.contains(cx, cy):
                return step_id
        return None

    def _plus_at(self, pos) -> str | None:
        cx, cy = self._to_content(pos)
        for step_id, prect in self._plus_rects.items():
            if prect.contains(cx, cy):
                return step_id
        return None

    def mousePressEvent(self, event) -> None:
        plus_id = self._plus_at(event.position())
        if plus_id is not None:
            self.add_child_requested.emit(plus_id)
            return
        step_id = self._node_at(event.position())
        if step_id is not None:
            self._selected = step_id
            self.update()
            self.node_clicked.emit(step_id)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        step_id = self._node_at(event.position())
        if step_id is not None:
            self._selected = step_id
            self.update()
            self.node_double_clicked.emit(step_id)
            return
        super().mouseDoubleClickEvent(event)


class WorkflowPanel(QWidget):
    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self.runner = runtime.workflow if getattr(runtime, "active_context", None) is not None else None
        outer = QVBoxLayout(self)

        if self.runner is None:
            label = QLabel("Open or create a project to build a workflow.", self)
            label.setAlignment(Qt.AlignCenter)
            outer.addWidget(label)
            return

        self._selected_step_id: str | None = None
        self._edit_mode = False
        self._workflow_name = "Untitled workflow"
        self._mode = "guided"  # "guided" (linear route, walk step by step) | "auto" (deferred DAG)
        self._opened_step_id: str | None = None  # guided: which manual step's panel we've opened

        panels = QSplitter(Qt.Horizontal, self)

        # --- library (left) ------------------------------------------------------
        lib_box = QGroupBox("Predefined workflows", panels)
        lib_layout = QVBoxLayout(lib_box)
        self.library = QListWidget(lib_box)
        self.library.setWordWrap(True)
        for name, preset in PRESET_WORKFLOWS.items():
            item = QListWidgetItem(f"{name}\n{len(preset.steps)} steps")
            item.setData(Qt.UserRole, name)
            chain = " → ".join(STEP_SPECS[k].label if k in STEP_SPECS else "Docking" for k in preset.steps)
            item.setToolTip(f"{preset.description}\n\n{chain}")
            self.library.addItem(item)
        self.library.itemDoubleClicked.connect(self._open_from_library)
        lib_layout.addWidget(QLabel("Double-click to open in the editor.", lib_box))
        lib_layout.addWidget(self.library, 1)
        open_btn = QPushButton("Open in editor", lib_box)
        open_btn.clicked.connect(lambda: self._open_from_library(self.library.currentItem()))
        lib_layout.addWidget(open_btn)
        panels.addWidget(lib_box)

        # --- editor / monitor (right) -------------------------------------------
        editor_box = QGroupBox("Editor / Monitor", panels)
        editor_layout = QVBoxLayout(editor_box)

        toolbar = QHBoxLayout()
        self.name_label = QLabel(self._workflow_name, editor_box)
        f = self.name_label.font()
        f.setBold(True)
        self.name_label.setFont(f)
        toolbar.addWidget(self.name_label, 1)
        self.new_btn = QPushButton("New", editor_box)
        self.new_btn.setToolTip("Start a new empty workflow in edit mode.")
        self.new_btn.clicked.connect(self._new_workflow)
        self.edit_btn = QPushButton("Edit", editor_box)
        self.edit_btn.setCheckable(True)
        self.edit_btn.setToolTip("Unlock this workflow to add, connect or remove steps.")
        self.edit_btn.toggled.connect(self._toggle_edit)
        self.save_as_btn = QPushButton("Save as new…", editor_box)
        self.save_as_btn.setEnabled(False)  # Fase 3: serialize the working copy to a file
        self.save_as_btn.setToolTip("Save this edited workflow as a new one — coming with persistence.")
        self.mode_combo = QComboBox(editor_box)
        self.mode_combo.addItem("Guided (step by step)", "guided")
        self.mode_combo.addItem("Automatic (unattended)", "auto")
        self.mode_combo.setToolTip(
            "Guided: walk the steps one at a time, running interactive ones in their real panel.\n"
            "Automatic: hand the whole DAG to MolSuite and let job steps run unattended.")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.run_btn = QPushButton("Run", editor_box)
        self.run_btn.clicked.connect(self._run)
        self.next_btn = QPushButton("Mark done ▸", editor_box)  # guided: finish the current manual step
        self.next_btn.setToolTip("The current interactive step is done — advance the route.")
        self.next_btn.clicked.connect(self._mark_done)
        self.open_step_btn = QPushButton("Open step", editor_box)  # guided: (re)open current step's panel
        self.open_step_btn.setToolTip("Open the panel for the current step.")
        self.open_step_btn.clicked.connect(lambda: self._open_step_panel(self.runner.current_step()))
        self.abort_btn = QPushButton("Abort", editor_box)
        self.abort_btn.clicked.connect(lambda: (self.runner.abort(cancel_running=True), self._refresh_all()))
        for b in (self.new_btn, self.edit_btn, self.save_as_btn, self.mode_combo,
                  self.run_btn, self.open_step_btn, self.next_btn, self.abort_btn):
            toolbar.addWidget(b)
        editor_layout.addLayout(toolbar)

        self.graph = WorkflowGraphView(self.runner, editor_box)
        self.graph.node_clicked.connect(self._select_node)
        self.graph.node_double_clicked.connect(self._configure_step)
        self.graph.add_child_requested.connect(self._add_child)
        scroll = QScrollArea(editor_box)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.graph)
        editor_layout.addWidget(scroll, 1)

        edit_row = QHBoxLayout()
        self.add_root_btn = QPushButton("＋ Add root step", editor_box)
        self.add_root_btn.setToolTip("Add an independent step (a new parallel branch, e.g. import receptors).")
        self.add_root_btn.clicked.connect(self._add_root)
        self.remove_btn = QPushButton("Remove selected", editor_box)
        self.remove_btn.clicked.connect(self._remove_selected)
        edit_row.addWidget(self.add_root_btn)
        edit_row.addWidget(self.remove_btn)
        edit_row.addStretch(1)
        self.status_label = QLabel("", editor_box)
        edit_row.addWidget(self.status_label)
        editor_layout.addLayout(edit_row)

        panels.addWidget(editor_box)
        panels.setSizes([280, 760])
        outer.addWidget(panels, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_poll)
        self._timer.start()
        self._refresh_all()

    # --- library / lifecycle -------------------------------------------------
    def _open_from_library(self, item) -> None:
        if item is None:
            return
        name = item.data(Qt.UserRole)
        if not self._reset_runner():  # refuses if something is running
            return
        mode = PRESET_WORKFLOWS[name].mode
        builder = build_route if mode == "guided" else build_preset
        for step in builder(name):
            self.runner.add_step(step)
        self._workflow_name = name
        self._set_mode(mode)
        self._set_edit_mode(False)  # opens read-only; press Edit to modify
        self._refresh_all()

    def _new_workflow(self) -> None:
        if not self._reset_runner():
            return
        self._workflow_name = "Untitled workflow"
        self._set_edit_mode(True)  # New goes straight to edit mode (less click friction)
        self._refresh_all()

    def _reset_runner(self) -> bool:
        try:
            self.runner.clear()
        except ValueError as exc:
            QMessageBox.information(self, "Workflow", str(exc))
            return False
        self._selected_step_id = None
        self._opened_step_id = None
        return True

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        idx = self.mode_combo.findData(mode)
        if idx >= 0 and self.mode_combo.currentIndex() != idx:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(idx)
            self.mode_combo.blockSignals(False)

    def _on_mode_changed(self, _index: int) -> None:
        self._mode = self.mode_combo.currentData()
        self._refresh_all()

    def _toggle_edit(self, checked: bool) -> None:
        if checked and any(s.status == "running" for s in self.runner.steps):
            QMessageBox.information(self, "Workflow", "Can't edit a running workflow — abort it first.")
            self.edit_btn.setChecked(False)
            return
        self._set_edit_mode(checked)
        self._refresh_all()

    def _set_edit_mode(self, on: bool) -> None:
        self._edit_mode = on
        if self.edit_btn.isChecked() != on:
            self.edit_btn.blockSignals(True)
            self.edit_btn.setChecked(on)
            self.edit_btn.blockSignals(False)

    # --- step adding (edit mode) ---------------------------------------------
    def _step_menu(self, depends_on: str | None) -> QMenu:
        menu = QMenu(self)
        for kind, spec in STEP_SPECS.items():
            act = QAction(spec.label + ("…" if kind in _IMPORT_KINDS else ""), menu)
            act.triggered.connect(lambda _checked=False, k=kind, d=depends_on: self._add_step(k, d))
            menu.addAction(act)
        menu.addSeparator()
        dock_act = QAction("Docking", menu)  # runs in the Docking panel (guided) or filled from it (auto)
        dock_act.triggered.connect(lambda _checked=False, d=depends_on: self._add_step("docking", d))
        menu.addAction(dock_act)
        return menu

    def _add_root(self) -> None:
        if not self._edit_mode:
            return
        # A guided route is linear: a new step chains onto the last one instead of starting a branch.
        parent = self.runner.steps[-1].step_id if (self._mode == "guided" and self.runner.steps) else None
        self._step_menu(parent).exec(QCursor.pos())

    def _add_child(self, parent_step_id: str) -> None:
        if not self._edit_mode:
            return
        self._step_menu(parent_step_id).exec(QCursor.pos())

    def _add_step(self, kind: str, depends_on: str | None) -> None:
        if kind == "docking":
            # Docking carries scope/box config only its panel builds. Guided: a manual step the user
            # runs in the Docking panel. Auto: a needs_config node filled from the panel's "Save to
            # workflow". Either way: no whole-panel-in-a-dialog config here.
            manual = self._mode == "guided"
            step = WorkflowStep(name="Docking", kind="docking", category="docking",
                                manual=manual, view_id=DOCKING_VIEW_ID if manual else "",
                                status=STEP_NEEDS_CONFIG)
        else:
            step = make_step(kind)  # imports start needs_config; no-arg steps are ready
        if depends_on:
            step.depends_on = [depends_on]
        self.runner.add_step(step)
        self._refresh_all()
        # In an AUTO DAG an import must capture its deferred payload now (no data at run time). In a
        # GUIDED route the import runs interactively when the route reaches it, so don't ask yet.
        if kind in _IMPORT_KINDS and self._mode == "auto":
            self._configure_import(step)

    def _remove_selected(self) -> None:
        step = self._step_by_id(self._selected_step_id or "")
        if step is None:
            QMessageBox.information(self, "Workflow", "Click a node in the graph to select a step first.")
            return
        try:
            self.runner.remove_step(step.step_id)
        except ValueError as exc:
            QMessageBox.information(self, "Workflow", str(exc))
            return
        self._selected_step_id = None
        self._refresh_all()

    # --- selection / config panel --------------------------------------------
    def _step_by_id(self, step_id: str):
        return next((s for s in self.runner.steps if s.step_id == step_id), None)

    def _select_node(self, step_id: str) -> None:
        self._selected_step_id = step_id
        self.graph.set_selected(step_id)
        self._refresh_status_text()

    def _configure_step(self, step_id: str) -> None:
        """Double-click a node → configure it in an ephemeral modal dialog that hosts a fresh
        instance of the relevant config widget. Modal, so the step can't be run mid-configuration.
        Only in edit mode (selecting steps is an edit-mode action)."""
        step = self._step_by_id(step_id)
        if step is None:
            return
        if not step.editable:
            QMessageBox.information(self, "Workflow", "This step is already running or finished — can't reconfigure it.")
            return
        if not self._edit_mode:
            QMessageBox.information(self, "Workflow", "Enable Edit to configure steps.")
            return
        if step.kind in _IMPORT_KINDS and self._mode == "auto":
            self._configure_import(step)
        elif step.category == "docking":
            QMessageBox.information(
                self, "Workflow",
                "Docking is configured in the Docking Studio: open it, set the box/engine, and use "
                "'Save to workflow' (Automatic) or just run it when the guided route reaches this step.")
        else:
            QMessageBox.information(self, "Workflow", f"'{step.name}' has no configuration.")

    def _configure_import(self, step) -> None:
        # Reuse the real import dialogs (drag-drop table, per-type grouping, streaming prefilter,
        # receptor binding-site extraction). defer=True captures the whole import as a deferred
        # submit that runs when the workflow runs — nothing is imported at config time.
        from amdockvs.ui.tools.import_workspace import LigandImportDialog, ReceptorImportDialog

        dialog_cls = LigandImportDialog if step.kind == "import_ligands" else ReceptorImportDialog
        dlg = dialog_cls(runtime=self.runtime, parent=self.window(), defer=True)
        if dlg.exec() == QDialog.Accepted and dlg.deferred_submit is not None:
            self.runner.configure_step(
                step.step_id, submit=dlg.deferred_submit, name=dlg.deferred_name, category="import"
            )
            self._refresh_all()

    # --- running -------------------------------------------------------------
    def _run(self) -> None:
        unconfigured = self.runner.unconfigured_steps()
        if unconfigured:
            names = ", ".join(s.name for s in unconfigured)
            QMessageBox.information(
                self, "Workflow",
                f"Configure these steps first (double-click a node to open its panel): {names}.",
            )
            return
        if not any(s.status == "pending" for s in self.runner.steps):
            QMessageBox.information(self, "Workflow", "Nothing to run — add some steps first.")
            return
        self._set_edit_mode(False)  # lock while running
        if self._mode == "guided":
            self.runner.start()  # step-by-step: the poll timer drives tick() and opens each panel
            self._refresh_all()
            return
        self.run_btn.setEnabled(False)
        from amdockvs.ui.async_query import run_async

        run_async(self.runner.materialize, self._on_launched, on_error=self._on_launched, busy=self)

    def _on_launched(self, _result) -> None:
        self.run_btn.setEnabled(True)
        self._refresh_all()

    def _on_poll(self) -> None:
        if self._mode == "guided":
            self.runner.tick()  # advances job steps; sets manual steps RUNNING to await the user
            self._maybe_open_current()
        else:
            self.runner.refresh_status()
        self._refresh_all()

    # --- guided route --------------------------------------------------------
    def _maybe_open_current(self) -> None:
        """When the route reaches a manual step, open its panel once (the user runs it there, then
        presses 'Mark done'). Guarded so we don't reopen a dialog the user just closed."""
        step = self.runner.current_step()
        if step is None or not step.manual or step.status != STEP_RUNNING:
            return
        if step.step_id == self._opened_step_id:
            return
        self._opened_step_id = step.step_id
        self._open_step_panel(step)

    def _open_step_panel(self, step) -> None:
        if step is None:
            return
        if step.kind in _IMPORT_KINDS:
            # Guided import: the real dialog with data going in for real (not deferred). Accepting it
            # launches the import and advances the route; the job then shows in the monitor.
            from amdockvs.ui.tools.import_workspace import LigandImportDialog, ReceptorImportDialog

            dialog_cls = LigandImportDialog if step.kind == "import_ligands" else ReceptorImportDialog
            dlg = dialog_cls(runtime=self.runtime, parent=self.window())
            if dlg.exec() == QDialog.Accepted and step.status == STEP_RUNNING and step.manual:
                self.runner.mark_step_done(step.step_id)
                self._refresh_all()
        elif step.view_id:
            self.window().open_or_focus_view(step.view_id)  # e.g. Docking Studio; user runs it, then Mark done

    def _mark_done(self) -> None:
        step = self.runner.current_step()
        if step is None or not step.manual or step.status != STEP_RUNNING:
            return
        self.runner.mark_step_done(step.step_id)
        self._opened_step_id = None
        self._refresh_all()

    # --- refresh -------------------------------------------------------------
    def _refresh_all(self) -> None:
        if self._selected_step_id is not None and self._step_by_id(self._selected_step_id) is None:
            self._selected_step_id = None
        self.graph.editable = self._edit_mode
        self.graph.set_selected(self._selected_step_id)
        self.graph.refresh()
        self.name_label.setText(self._workflow_name + ("  ·  editing" if self._edit_mode else ""))
        running = any(s.status == "running" for s in self.runner.steps)
        guided = self._mode == "guided"
        current = self.runner.current_step()
        manual_now = current is not None and current.manual and current.status == STEP_RUNNING
        self.add_root_btn.setEnabled(self._edit_mode)
        self.remove_btn.setEnabled(self._edit_mode)
        self.edit_btn.setEnabled(not running)
        self.mode_combo.setEnabled(not running)  # can't switch execution style mid-run
        self.run_btn.setEnabled(not running and bool(self.runner.steps))
        self.run_btn.setText("Start route" if guided else "Run")
        self.open_step_btn.setVisible(guided)
        self.open_step_btn.setEnabled(guided and running and current is not None)
        self.next_btn.setVisible(guided)
        self.next_btn.setEnabled(manual_now)
        self.abort_btn.setEnabled(running)
        self._refresh_status_text()

    def _refresh_status_text(self) -> None:
        selected = self._step_by_id(self._selected_step_id or "")
        if selected is not None and selected.error:
            self.status_label.setText(f"{selected.name}: {selected.error}")
        else:
            self.status_label.setText(self._status_text())

    def _status_text(self) -> str:
        steps = self.runner.steps
        if not steps:
            return "empty"
        running = [s.name for s in steps if s.status == "running"]
        needs = sum(1 for s in steps if s.status == "needs_config")
        if running:
            done = sum(1 for s in steps if s.status in ("completed", "skipped"))
            return f"running: {', '.join(running)}  ({done}/{len(steps)} done)"
        if needs:
            return f"{self.runner.status} · {needs} step(s) need configuration"
        return self.runner.status


def save_to_workflow(window, *, kind: str, name: str, category: str | None, submit) -> bool:
    """Reusable 'Save to workflow' for any op panel: upsert a configured step into the active
    workflow (update the pending step of this kind if present, else add). Returns True on success.
    Each panel builds `submit` from its current widgets and passes a stable `kind`."""
    runtime = getattr(window, "runtime", None)
    runner = runtime.workflow if runtime is not None and getattr(runtime, "active_context", None) is not None else None
    if runner is None:
        QMessageBox.information(window, "Workflow", "Open or create a project first.")
        return False
    _step, created = runner.upsert_step(WorkflowStep(name=name, kind=kind, category=category, submit=submit))
    QMessageBox.information(
        window, "Workflow",
        f"{'Added to' if created else 'Updated in'} the active workflow: {name}.",
    )
    return True


def register_workflow_panel(window) -> None:
    # Workflow is reached from the left-sidebar action (see _wire_workflow_quick_access).
    window.register_main_view(
        WORKFLOW_VIEW_ID, "Workflow",
        lambda: WorkflowPanel(runtime=window.runtime, parent=window.central_widget),
    )


__all__ = [
    "WORKFLOW_VIEW_ID",
    "WorkflowPanel",
    "WorkflowGraphView",
    "save_to_workflow",
    "register_workflow_panel",
]
