"""Summary dialog for the active project, opened from Project in the top toolbar.
Shows metadata + quick counts; it does not navigate, it only informs
(with an optional button to jump to the project browser)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QPushButton, QVBoxLayout,
)

from amdockvs.core.payloads import payload_store
from amdockvs.project.storage import compact_storage, storage_stats
from amdockvs.ui.common.async_query import run_async


def _safe_count(fn: Callable[[], Any]) -> Optional[int]:
    try:
        return int(fn())
    except Exception:
        return None


def _muted_label(text: str = "") -> QLabel:
    """Secondary text, unwrapped: a wrapped label's height is not re-asked when its text grows,
    which is what cut the last line off. Lines are broken by hand instead."""
    label = QLabel(text)
    label.setStyleSheet("color:palette(placeholder-text);")
    return label


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
    num.setObjectName("summaryCountValue")
    num.setAlignment(Qt.AlignCenter)
    num.setStyleSheet("font-size:18px; font-weight:600;")
    cap = QLabel(label)
    cap.setAlignment(Qt.AlignCenter)
    cap.setStyleSheet("color:palette(placeholder-text); font-size:11px;")
    lay.addWidget(num)
    lay.addWidget(cap)
    return box


def _storage_section(dlg: QDialog, runtime) -> QVBoxLayout:
    """Size and dead bytes of the structure volumes, with Check (reads every structure), Repair
    (clears the leftovers a check finds), Compact (reclaims the dead bytes) and Clear queue files
    (what campaigns of rows packed for a cluster's queue). All of it runs off
    the GUI thread."""
    store = payload_store(runtime)
    layout = QVBoxLayout()
    layout.addWidget(QLabel("Structure storage"))
    if _safe_count(runtime.has_shards):
        layout.addWidget(_muted_label("Structures of the rows. Shards are files of their own, counted above."))
    text = _muted_label("Reading…")
    layout.addWidget(text)
    row = QHBoxLayout()
    check, repair, compact = QPushButton("Check"), QPushButton("Repair"), QPushButton("Compact")
    repair.setToolTip("Delete structures no row points to any more, empty volumes and orphan files. Not while jobs run.")
    transport = QPushButton("Clear queue files")
    transport.setToolTip("Delete the ligands packed for a cluster's queue. A canceled or failed docking there can "
                         "no longer be resumed; dock it again and what is docked is skipped. Not while jobs run.")
    buttons = (check, repair, compact, transport)
    for button in buttons:  # equal shares of the full width: both edges line up with the rows above
        row.addWidget(button, 1)
    layout.addLayout(row)
    result = _muted_label()  # what the last button did: the summary above alone often reads the same
    layout.addWidget(result)
    result.hide()
    open_ = [True]  # a result that lands after the dialog closed has nowhere to go
    dlg.finished.connect(lambda _code: open_.__setitem__(0, False))

    def run(work, note: str) -> None:
        text.setText(note)
        for button in buttons:
            button.setEnabled(False)
        run_async(work, show, on_error=fail)

    def done(said: str) -> None:
        if said:
            result.setText(f"{datetime.now():%H:%M:%S}  {said}")
            result.show()
        for button in buttons:
            button.setEnabled(True)

    def show(answer) -> None:
        if open_[0]:
            stats, said = answer if isinstance(answer, tuple) else (answer, "")
            text.setText(stats.summary())
            done(said)

    def fail(exc) -> None:
        if open_[0]:
            text.setText("Storage could not be read.")
            done(f"Failed: {exc}")

    def leftovers(stats) -> int:
        report = stats.report
        return len(report.unreferenced) + len(report.empty) + len(report.orphans)

    def checked() -> tuple:
        stats = storage_stats(store, full=True)
        if not stats.healthy:
            return stats, "Check: problems found, see above."
        found = leftovers(stats)
        return stats, (f"Check: every structure read; {found:,} leftover item(s), Repair clears them."
                       if found else "Check: every structure read, nothing to repair.")

    def repaired() -> tuple:
        before = leftovers(storage_stats(store))
        runtime.repair_storage()
        stats = storage_stats(store)
        cleared = before - leftovers(stats)
        return stats, f"Repair: cleared {cleared:,} leftover item(s)." if cleared else "Repair: nothing to clear."

    def compacted() -> tuple:
        before = storage_stats(store)
        report = compact_storage(store) or {}
        stats = storage_stats(store)
        rewritten = int(report.get("volumes") or 0)
        freed = max(0, before.size - stats.size)
        return stats, (f"Compact: rewrote {rewritten:,} volume(s), freed {freed / 1e6:,.1f} MB."
                       if rewritten else "Compact: nothing to reclaim.")

    def cleared() -> tuple:
        freed = runtime.clear_transport()
        size = f"{freed / 1e6:,.1f} MB" if freed >= 100_000 else f"{freed / 1e3:,.0f} KB"
        return storage_stats(store), (f"Clear queue files: freed {size}." if freed
                                      else "Clear queue files: there were none.")

    check.clicked.connect(lambda: run(checked, "Checking every structure…"))
    repair.clicked.connect(lambda: run(repaired, "Repairing…"))
    compact.clicked.connect(lambda: run(compacted, "Compacting…"))
    transport.clicked.connect(lambda: run(cleared, "Clearing queue files…"))
    run(lambda: storage_stats(store), "Reading…")
    return layout


def _library_section(dlg: QDialog, runtime) -> QVBoxLayout:
    """A sharded project's library: the row counts above read zero for it, since its ligands
    live in shard files. Read off the shard inventory, off the GUI thread."""
    from amdockvs.molecules.library_stats import format_bytes

    layout = QVBoxLayout()
    layout.addWidget(QLabel("Screening library (shards)"))
    grid = QGridLayout()
    grid.setSpacing(8)
    boxes = {}
    for col, label in enumerate(("Ligands", "Shards", "Docked", "Hits")):
        box = _count_box(label, None)
        boxes[label] = box.findChild(QLabel, "summaryCountValue")
        boxes[label].setText("…")
        grid.addWidget(box, 0, col)
    layout.addLayout(grid)
    text = _muted_label("Reading…")
    layout.addWidget(text)
    open_ = [True]
    dlg.finished.connect(lambda _code: open_.__setitem__(0, False))

    def show(stats) -> None:
        if not open_[0]:
            return
        for label, value in (("Ligands", stats.ligands), ("Shards", stats.shards),
                             ("Docked", stats.ligands_docked), ("Hits", stats.hits)):
            boxes[label].setText(f"{value:,}")
        lines = []
        if stats.last_step:
            lines.append(f"Last step: {stats.last_step}")
        if stats.previous_step:
            lines.append(f"Kept for a redo: {stats.previous_step}")
        for family, (n_shards, n_ligands, n_failed) in sorted(stats.prepared.items()):
            failed = f", {n_failed:,} failed" if n_failed else ""
            lines.append(f"Prepared ({family}): {n_shards:,}/{stats.shards:,} shards · {n_ligands:,} ligands{failed}")
        if stats.campaigns:
            lines.append(f"Docked: {stats.campaigns:,} campaign(s) · {stats.receptors_docked:,} receptor(s)"
                         " · counts are ligand-receptor pairs")
        tail = [f"{format_bytes(stats.disk_bytes)} on disk"] if stats.disk_bytes else []
        if stats.failed_shards:
            tail.append(f"{stats.failed_shards:,} failed shard(s): their ligands are missing")
        if tail:
            lines.append(" · ".join(tail))
        text.setText("\n".join(lines) or "No library yet.")

    def fail(exc) -> None:
        if open_[0]:
            text.setText(f"Library error: {exc}")

    run_async(runtime.molecules.library_stats, show, on_error=fail)
    return layout


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
    dlg.setWindowTitle(f"Project - {name}")
    dlg.setMinimumWidth(440)
    root = QVBoxLayout(dlg)
    # Grows with what arrives later and cannot be dragged smaller than its content.
    root.setSizeConstraint(QLayout.SetMinimumSize)

    header = QLabel(f"📁  {name}")
    header.setStyleSheet("font-size:15px; font-weight:600;")
    root.addWidget(header)

    if summary is not None and getattr(summary, "description", ""):
        desc = QLabel(summary.description)
        desc.setWordWrap(True)
        desc.setStyleSheet("color:palette(placeholder-text);")
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
    sharded = bool(_safe_count(runtime.has_shards))
    counts = [
        ("Molecules", _safe_count(mols.count)),
        ("Receptors", _safe_count(lambda: mols.count(mols.select(role="receptor")))),
        # In a sharded project these are the curated ones (cocrystals, activity sets), not the library.
        ("Ligand rows" if sharded else "Ligands", _safe_count(lambda: mols.count(mols.select(role="ligand")))),
        ("Complexes", _safe_count(runtime.complexes.count)),
    ]
    grid = QGridLayout()
    grid.setSpacing(8)
    for col, (label, value) in enumerate(counts):
        grid.addWidget(_count_box(label, value), 0, col)
    root.addLayout(grid)
    if sharded:
        root.addSpacing(6)
        root.addLayout(_library_section(dlg, runtime))
    root.addSpacing(6)
    root.addLayout(_storage_section(dlg, runtime))

    buttons = QDialogButtonBox()
    manage = buttons.addButton("Manage projects…", QDialogButtonBox.ActionRole)
    buttons.addButton(QDialogButtonBox.Close)
    manage.clicked.connect(lambda: (dlg.accept(), window._open_projects_browser()))
    buttons.rejected.connect(dlg.reject)
    root.addWidget(buttons)

    dlg.exec()
