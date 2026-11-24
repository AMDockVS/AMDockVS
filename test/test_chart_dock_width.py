"""The chart dock's width bookkeeping: it shares a slot with the 3D viewer, so every swap back
hands it whatever width the viewer left. ChartController claims its own width back — without
taking away one the user dragged, and without fighting a layout that will not grant it."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from amdockvs.ui.visualization.chart_controller import (  # noqa: E402
    _CHART_WIDTH_SHARE,
    _MAX_WIDTH_CLAIMS,
    ChartController,
)

app = QApplication.instance() or QApplication([])


class _Dock:
    def __init__(self, width=100):
        self._w, self._visible, self._floating = width, True, False

    def width(self):
        return self._w

    def isVisible(self):  # noqa: N802 - Qt name
        return self._visible

    def isFloating(self):  # noqa: N802 - Qt name
        return self._floating


class _Window:
    """Enough QMainWindow for the controller: a dock whose width the layout may refuse to grant.

    ``grant`` caps what resizeDocks hands out, standing in for a centre widget at its minimum.
    A resize event only follows when the width actually changed, as Qt does it.
    """

    def __init__(self, width=1400, grant=None):
        self.qsar_chart_dock = _Dock()
        self._w, self._grant = width, grant
        self.controller = None

    def width(self):
        return self._w

    def resizeDocks(self, docks, sizes, orientation):  # noqa: N802 - Qt name
        want = sizes[0] if self._grant is None else min(sizes[0], self._grant)
        if want != docks[0]._w:
            docks[0]._w = want
            self.controller._note_chart_width(want)


def _controller(window):
    c = ChartController.__new__(ChartController)
    c.w = window
    window.controller = c
    c._chart_width = c._claims = 0
    c._last_claim = -1
    c._chosen = c._settling = c._widen_queued = False
    c._width_watcher = object()  # watch_chart_width() needs a real QObject window; neutralise it
    return c


def _settle(controller, passes=_MAX_WIDTH_CLAIMS + 4):
    """Run the claim loop out: it re-queues itself until granted, refused, or out of tries."""
    for _ in range(passes):
        app.processEvents()
    assert not controller._settling, "the claim never finished settling"


def _drag(controller, width):
    """A divider drag: the layout resizes the dock with a mouse button held down."""
    with mock.patch.object(QApplication, "mouseButtons", staticmethod(lambda: Qt.LeftButton)):
        controller.w.resizeDocks([controller.w.qsar_chart_dock], [width], Qt.Horizontal)


def test_the_chart_opens_at_its_share_of_the_window():
    window = _Window(width=1400)
    controller = _controller(window)
    controller._queue_widen(force=True)
    _settle(controller)
    assert window.qsar_chart_dock.width() == int(1400 * _CHART_WIDTH_SHARE)
    assert not controller._chosen  # our own measure, not anyone's choice


def test_a_width_the_user_dragged_survives_the_startup_measure():
    # size_chart_to_third runs from resizeEvent, which fires on every resize of a window that
    # never maximizes. It must not take the divider back each time the window edge is touched.
    window = _Window(width=1400)
    controller = _controller(window)
    controller._queue_widen(force=True)
    _settle(controller)
    _drag(controller, 300)
    assert controller._chosen and controller._chart_width == 300

    window._w = 1600
    controller.size_chart_to_third()
    app.processEvents()
    assert window.qsar_chart_dock.width() == 300


def test_the_startup_measure_still_corrects_a_width_nobody_chose():
    # A project that opens on QSAR Analysis is laid out against the window before it reaches its
    # real size, so the share it got is of the wrong total. A resize nobody asked for - a taller
    # window, a tab switch squeezing the dock - must not pass for a choice and block the retry.
    window = _Window(width=900)
    controller = _controller(window)
    controller._queue_widen(force=True)
    _settle(controller)
    assert window.qsar_chart_dock.width() == 360  # 40% of 900

    window.resizeDocks([window.qsar_chart_dock], [360], Qt.Horizontal)  # height-only: same width
    window._w = 1920
    assert not controller._chosen

    controller.size_chart_to_third()
    _settle(controller)
    assert window.qsar_chart_dock.width() == int(1920 * _CHART_WIDTH_SHARE)


def test_the_claim_gives_up_when_the_layout_will_not_grant_it():
    # The centre is at its minimum, so the dock can never reach its share. The claim has to stop:
    # while it is still settling every width is read as the layout's and taken straight back, and
    # the user could not resize the dock again for the rest of the session.
    window = _Window(width=1000, grant=294)
    controller = _controller(window)
    controller._queue_widen(force=True)
    _settle(controller)  # asserts it stopped settling: otherwise the dock is locked for good
    assert window.qsar_chart_dock.width() == 294  # capped, never the 400 it asked for

    _drag(controller, 254)
    assert window.qsar_chart_dock.width() == 254
