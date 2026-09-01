from __future__ import annotations

from amdockvs.ui.monitor.monitor import MonitorPage


MONITOR_JOBS_VIEW_ID = "monitor.jobs"


def register_monitor_views(window) -> None:
    window.register_main_view(
        MONITOR_JOBS_VIEW_ID,
        "Jobs",
        lambda: MonitorPage(
            bridge=window.monitor_bridge,
            parent=window.central_widget,
        ),
        on_close=window.restore_monitor_dock,
    )
