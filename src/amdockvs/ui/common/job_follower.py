"""Drives a tool's ActionBar from the monitor bridge for the jobs one Run submitted."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from ms_components.tool_panel import ActionBar, format_count


class JobFollower(QObject):
    """One Run = ``[(stage, job_id), ...]`` in execution order (a chain waits on ``depends_on``).

    The bar shows the running job, else the first unfinished one; it finishes once every job
    is terminal. MolSuite cancels a failed job's dependents, so a chain always terminates.
    """

    finished = Signal(str)  # overall outcome: completed / failed / canceled

    def __init__(self, host: QWidget, bar: ActionBar, *, noun: str, unit: str = "batches") -> None:
        """``host`` owns the follower and provides ``runtime`` (cancel) and the window (bridge)."""
        super().__init__(host)
        self._host = host
        self.bar = bar
        self.noun = noun
        self.unit = unit
        self._stages: list[tuple[str, str]] = []
        self._states: dict[str, object] = {}
        self._outcomes: dict[str, str] = {}
        self._connected = False
        bar.cancel_requested.connect(self.cancel)
        bar.details_requested.connect(self._open_jobs_monitor)

    @property
    def job_ids(self) -> list[str]:
        return [job_id for _stage, job_id in self._stages]

    def follow(self, stages: list[tuple[str, str]], *, unit: str | None = None) -> None:
        self._connect()
        self._stages = [(str(stage), str(job_id)) for stage, job_id in stages]
        self._states, self._outcomes = {}, {}
        self.unit = unit or self.unit
        self.bar.start("Queued", self._step_text(0))

    def on_upserted(self, job_id: str, state) -> None:
        job_id = str(job_id)
        if job_id not in self.job_ids or job_id in self._outcomes or state.is_terminal:
            return  # the terminal state arrives through on_finished
        self._states[job_id] = state
        self._render()

    def on_finished(self, job_id: str, status: str) -> None:
        job_id = str(job_id)
        if job_id not in self.job_ids or job_id in self._outcomes:
            return
        self._outcomes[job_id] = str(status or "").strip().lower()
        if len(self._outcomes) < len(self._stages):
            self._render()
            return
        outcomes = set(self._outcomes.values())
        status = next((s for s in ("failed", "canceled") if s in outcomes), "completed")
        headline = {"completed": f"{self.noun} completed", "canceled": f"{self.noun} cancelled"}.get(
            status, f"{self.noun} {status}"
        )
        self.bar.finish(headline, "Open Jobs for the log" if status == "failed" else "", failures=self._failures())
        self._stages = []
        self.finished.emit(status)

    def cancel(self) -> None:
        pending = [job_id for job_id in self.job_ids if job_id not in self._outcomes]
        try:
            for job_id in pending:
                self._host.runtime.cancel_job(job_id)
        except Exception as exc:  # noqa: BLE001 - the jobs keep running; let the user retry
            self.bar.cancel_button.setEnabled(True)
            QMessageBox.warning(self._host, f"Cancel {self.noun}", str(exc))

    def _render(self) -> None:
        pending = [(i, stage, job_id) for i, (stage, job_id) in enumerate(self._stages) if job_id not in self._outcomes]
        if not pending:
            return
        running = [item for item in pending if getattr(self._states.get(item[2]), "status", "") == "running"]
        index, stage, job_id = (running or pending)[0]
        state = self._states.get(job_id)
        if state is None or state.status != "running":
            self.bar.start("Queued", self._step_text(index) or "Waiting for earlier jobs or a free worker")
            return
        # A streamed feed only knows a floor for the total until it is exhausted.
        total = int(state.chunks_total) if state.feed_exhausted and state.chunks_total else None
        busy = int(state.chunks_running)
        detail = " · ".join(filter(None, (self._step_text(index), f"{format_count(busy)} running" if busy else "")))
        self.bar.set_progress(
            int(state.chunks_done), total, stage=stage, unit=self.unit, failures=self._failures(), detail=detail
        )

    def _step_text(self, index: int) -> str:
        return f"step {index + 1} of {len(self._stages)}" if len(self._stages) > 1 else ""

    def _failures(self) -> int:
        return sum(int(getattr(state, "chunks_failed", 0)) for state in self._states.values())

    def _connect(self) -> None:
        bridge = getattr(self._host.window(), "monitor_bridge", None)
        if self._connected or bridge is None:
            return
        bridge.job_upserted.connect(self.on_upserted)
        bridge.job_finished.connect(self.on_finished)
        self._connected = True

    def _open_jobs_monitor(self) -> None:
        opener = getattr(self._host.window(), "open_jobs_monitor", None)
        if callable(opener):
            opener()
