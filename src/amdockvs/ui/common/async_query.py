"""Run a blocking callable (typically a DB read) off the GUI thread.

The project DB opens a fresh SQLite session per call with check_same_thread=False,
so read queries are safe from a worker thread. Use this to keep heavy reads
(scope counts, requirement checks over large molecule sets) from freezing the UI.

    run_async(lambda: runtime.molecules.count(scope), on_result=self._apply)

`on_result` / `on_error` run on the GUI thread (queued signals). Capture any Qt
widget state on the calling thread BEFORE handing work to `fn` — never touch
widgets inside `fn`.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QRunnable, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QVBoxLayout, QWidget


class BusyOverlay(QWidget):
    """A translucent 'Loading…' layer over a host widget — the reusable busy indicator.

    Qt has no built-in "this widget is waiting on a thread" affordance, so this is the
    least-hellish substitute: one overlay per host, auto-sized to it, shown while a
    `run_async(..., busy=host)` call is in flight and hidden when it lands. It also eats
    clicks so the user can't act on stale content mid-load.
    """

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        self._label = QLabel("Loading…", self)
        self._label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._label)
        # Indeterminate (range 0,0) bar: there is no incremental signal from the blocking call, so
        # this animates to show the UI is alive rather than faking a percentage.
        self._bar = QProgressBar(self)
        self._bar.setRange(0, 0)
        self._bar.setTextVisible(False)
        self._bar.setFixedWidth(160)
        layout.addWidget(self._bar, alignment=Qt.AlignCenter)
        # Text follows the palette; the scrim is painted (QSS can't alpha a palette color).
        self._label.setStyleSheet("background: transparent; font-weight: 600;")
        host.installEventFilter(self)
        self.hide()

    def paintEvent(self, _event) -> None:
        # Translucent scrim tinted to the current theme's window color (dim in dark, light themes alike).
        scrim = self.palette().color(QPalette.ColorRole.Window)
        scrim.setAlpha(150)
        QPainter(self).fillRect(self.rect(), scrim)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.parent() and event.type() in (QEvent.Resize, QEvent.Show):
            self.setGeometry(self.parent().rect())
        return False

    def start(self) -> None:
        self.setGeometry(self.parent().rect())
        self.raise_()
        self.show()

    def stop(self) -> None:
        self.hide()


class BusySpinner(QWidget):
    """A small inline spinner for compact components — a single value, a short label —
    where the full BusyOverlay would be overkill. Painted with QPainter (no gif asset),
    driven by a QTimer, parked at the host's right edge so it doesn't hide the content.
    """

    def __init__(self, host: QWidget, *, size: int = 16) -> None:
        super().__init__(host)
        self._angle = 0
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._tick)
        host.installEventFilter(self)
        self.hide()

    def _tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.parent() and event.type() in (QEvent.Resize, QEvent.Show, QEvent.Move):
            self._reposition()
        return False

    def _reposition(self) -> None:
        r = self.parent().rect()
        self.move(r.right() - self.width() - 1, r.top() + max(0, (r.height() - self.height()) // 2))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self.palette().color(QPalette.ColorRole.Highlight))
        pen.setWidth(2)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        # A 300° arc rotated each tick reads as a spinning ring.
        painter.drawArc(self.rect().adjusted(2, 2, -2, -2), -self._angle * 16, 300 * 16)

    def start(self) -> None:
        self._reposition()
        self.raise_()
        self.show()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()


def _ensure_overlay(widget: QWidget) -> BusyOverlay:
    overlay = getattr(widget, "_busy_overlay", None)
    if overlay is None:
        overlay = BusyOverlay(widget)
        widget._busy_overlay = overlay
    return overlay


def _ensure_spinner(widget: QWidget) -> BusySpinner:
    spinner = getattr(widget, "_busy_spinner", None)
    if spinner is None:
        spinner = BusySpinner(widget)
        widget._busy_spinner = spinner
    return spinner


class _Signals(QObject):
    done = Signal(object)
    error = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # surfaced to on_error on the GUI thread
            self._signals.error.emit(exc)
            return
        self._signals.done.emit(result)


_ACTIVE_TASKS: dict[_Task, _Signals] = {}


def run_async(
    fn: Callable[[], Any],
    on_result: Callable[[Any], None],
    on_error: Callable[[Exception], None] | None = None,
    *,
    pool: QThreadPool | None = None,
    busy: QWidget | None = None,
    compact: bool = False,
) -> None:
    signals = _Signals(QApplication.instance())
    # busy region gets either a full overlay (big areas: tables, previews) or a small inline
    # spinner (compact=True, for a single value/label where the overlay would be overkill).
    overlay = None
    if busy is not None:
        overlay = _ensure_spinner(busy) if compact else _ensure_overlay(busy)
        overlay.start()

    def _done(result: Any) -> None:
        _ACTIVE_TASKS.pop(task, None)
        if overlay is not None:
            overlay.stop()
        signals.deleteLater()
        on_result(result)

    def _err(exc: Any) -> None:
        _ACTIVE_TASKS.pop(task, None)
        if overlay is not None:
            overlay.stop()
        signals.deleteLater()
        if on_error is not None:
            on_error(exc)

    signals.done.connect(_done)
    signals.error.connect(_err)
    task = _Task(fn, signals)
    task.setAutoDelete(False)
    # PySide does not guarantee keeping the Python wrappers of the QRunnable or of its signal
    # QObject alive while the work crosses threads; both references live until done/error.
    _ACTIVE_TASKS[task] = signals
    (pool or QThreadPool.globalInstance()).start(task)
