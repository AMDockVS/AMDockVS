"""What both importers do once their jobs are submitted."""

from __future__ import annotations

from pathlib import Path

from amdockvs.ui.catalog.common import BoundTableWidget


def land_on_view(window, view_id: str, job_ids=(), *, noun: str = "Import") -> None:
    """Open the table the import fills and put the jobs' progress under it, where the eye is."""
    if window is None or not hasattr(window, "open_or_focus_view"):
        return
    view = window.open_or_focus_view(view_id)
    table = view if isinstance(view, BoundTableWidget) else view.findChild(BoundTableWidget)
    if table is not None:
        table.follow_jobs(job_ids, noun=noun, stage="Importing")


def nudge_monitor(window) -> None:
    # The monitor backs off to a ~3s idle poll, so a freshly submitted job (and its progress
    # bar) can take that long to appear. Poke it to poll now so the bar shows immediately.
    bridge = getattr(window, "monitor_bridge", None)
    if bridge is not None and hasattr(bridge, "request_refresh"):
        bridge.request_refresh()


__all__ = ["land_on_view", "nudge_monitor"]


def confirm_reimport(parent, title: str, runtime, files) -> bool | None:
    """None: cancel. True: some files were imported before and the user wants them again (the
    import goes with `force=True`). False: nothing imported before."""
    from PySide6.QtWidgets import QMessageBox

    done = runtime.loader.duplicate_imports(files)
    if not done:
        return False
    names = "\n".join(Path(f).name for f in done[:10]) + ("\n…" if len(done) > 10 else "")
    answer = QMessageBox.question(
        parent, title,
        f"Already imported into this project:\n\n{names}\n\nImport again? The molecules will be duplicated.",
        QMessageBox.Yes | QMessageBox.Cancel,
    )
    return True if answer == QMessageBox.Yes else None
