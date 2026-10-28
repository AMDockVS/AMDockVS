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


__all__ = ["land_on_view", "nudge_monitor", "submit_import"]


def submit_import(dialog, title: str, runtime, files, on_submitted) -> None:
    """The duplicate check, the reimport question if any, then `dialog.workflow_submit(force=)`'s
    submit, with `on_submitted(job_ids)` back on the GUI thread. The check and the submit hash and
    count every file — minutes for a proteome — so both run off the GUI thread."""
    from PySide6.QtWidgets import QMessageBox

    from amdockvs.ui.common.async_query import run_async

    def failed(exc) -> None:
        QMessageBox.critical(dialog, title, f"Could not submit the import job:\n{exc}")

    def submit(force: bool) -> None:
        payload = dialog.workflow_submit(force=force)
        if payload is not None:
            run_async(lambda: payload[0](runtime), on_submitted, failed, busy=dialog)

    def checked(done: list[str]) -> None:
        if done:
            names = "\n".join(Path(f).name for f in done[:10]) + ("\n…" if len(done) > 10 else "")
            answer = QMessageBox.question(
                dialog, title,
                f"{len(done)} file(s) already imported into this project:\n\n{names}\n\n"
                "Import again? The molecules will be duplicated.",
                QMessageBox.Yes | QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        submit(bool(done))

    if not files:
        submit(False)  # workflow_submit says what is missing
        return
    run_async(lambda: runtime.loader.duplicate_imports(files), checked, failed, busy=dialog)
