"""Python's cyclic garbage collection, on the GUI thread only.

Python collects on whichever thread happens to allocate when a generation fills up. When that is
a worker (a `run_async` read, an engine thread) and the garbage holds Qt widgets, they are
destroyed off the GUI thread: ~QObject takes Qt's connection lock and then waits for the GIL,
while the GUI thread holds the GIL and waits for that lock inside a connect(). Both stop for good
(seen in the tutorial tests: a closed window's QMenu destroyed by a pooled read thread).

So automatic collection is off and the GUI thread collects on a timer, honouring the same
thresholds the automatic collector would have used. Reference counting still frees everything
that is not in a cycle at once, on whatever thread drops it.
"""
from __future__ import annotations

import gc

from PySide6.QtCore import QObject, QTimer

INTERVAL_MS = 1000


def collect_due() -> None:
    """Collect the oldest generation whose threshold is reached, as gc's own trigger would."""
    counts, thresholds = gc.get_count(), gc.get_threshold()
    for generation in (2, 1, 0):
        if counts[generation] > thresholds[generation]:
            gc.collect(generation)
            return


class _Collector(QObject):
    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(INTERVAL_MS)
        self._timer.timeout.connect(collect_due)
        self._timer.start()


def install(app: QObject) -> None:
    """Turn automatic collection off and collect from ``app``'s (GUI) thread. Once per app."""
    if getattr(app, "_gui_thread_gc", None) is None:
        gc.disable()
        app._gui_thread_gc = _Collector(app)
