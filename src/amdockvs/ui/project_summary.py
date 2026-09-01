"""Summary dialog for the active project — opened from the project chip in the
statusbar. Shows metadata + quick counts; it does not navigate, it only informs
(with an optional button to jump to the project browser)."""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QLabel, QVBoxLayout,
)


def _safe_count(fn: Callable[[], Any]) -> Optional[int]:
    try:
        return int(fn())
    except Exception:
        return None


def _count_box(label: str, value: Optional[int]) -> QFrame:
    box = QFrame()
    box.setObjectName("summaryCountBox")
    box.setStyleSheet(
        "QFrame#summaryCountBox { border:1px solid palette(mid); border-radius:8px; }"
        " QLabel { background:transparent; }"
    )
    lay = QVBoxLayout(box)
    lay.setContentsMargins(10, 8, 10, 8)
    lay.setSpacing(2)
    num = QLabel("—" if value is None else f"{value:,}")
    num.setAlignment(Qt.AlignCenter)
    num.setStyleSheet("font-size:18px; font-weight:600;")
    cap = QLabel(label)
    cap.setAlignment(Qt.AlignCenter)
    cap.setStyleSheet("color:palette(mid); font-size:11px;")
    lay.addWidget(num)
    lay.addWidget(cap)
    return box


def show_project_summary(window) -> None:
    runtime = window.runtime
    if getattr(runtime, "active_context", None) is None:
        return

    ctx = runtime.active_context
    try:
        summary = runtime.get_active_project()
    except Exception:
        summary = None

    name = (getattr(summary, "name", None) or getattr(ctx, "name", None) or "—")
    path = str(getattr(summary, "path", None) or getattr(ctx, "path", "") or "")

    dlg = QDialog(window)
    dlg.setWindowTitle(f"Project — {name}")
    dlg.setMinimumWidth(440)
    root = QVBoxLayout(dlg)

    header = QLabel(f"📁  {name}")
    header.setStyleSheet("font-size:15px; font-weight:600;")
    root.addWidget(header)

    if summary is not None and getattr(summary, "description", ""):
        desc = QLabel(summary.description)
        desc.setWordWrap(True)
        desc.setStyleSheet("color:palette(mid);")
        root.addWidget(desc)

    meta = QFormLayout()
    meta.setLabelAlignment(Qt.AlignRight)
    if path:
        path_label = QLabel(path)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_label.setWordWrap(True)
        meta.addRow("Path:", path_label)
    if summary is not None:
        if getattr(summary, "created_at", None):
            meta.addRow("Created:", QLabel(summary.created_at.strftime("%Y-%m-%d %H:%M")))
        if getattr(summary, "updated_at", None):
            meta.addRow("Updated:", QLabel(summary.updated_at.strftime("%Y-%m-%d %H:%M")))
        if getattr(summary, "tags", None):
            meta.addRow("Tags:", QLabel(", ".join(summary.tags)))
    root.addLayout(meta)

    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    root.addWidget(line)

    mols = runtime.molecules
    counts = [
        ("Molecules", _safe_count(mols.count)),
        ("Ligands", _safe_count(lambda: mols.count(mols.select(role="ligand")))),
        ("Receptors", _safe_count(lambda: mols.count(mols.select(role="receptor")))),
        ("Complexes", _safe_count(runtime.complexes.count)),
    ]
    grid = QGridLayout()
    grid.setSpacing(8)
    for col, (label, value) in enumerate(counts):
        grid.addWidget(_count_box(label, value), 0, col)
    root.addLayout(grid)

    buttons = QDialogButtonBox()
    manage = buttons.addButton("Manage projects…", QDialogButtonBox.ActionRole)
    buttons.addButton(QDialogButtonBox.Close)
    manage.clicked.connect(lambda: (dlg.accept(), window._open_projects_browser()))
    buttons.rejected.connect(dlg.reject)
    root.addWidget(buttons)

    dlg.exec()
