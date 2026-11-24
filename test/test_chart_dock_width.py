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


class _Central:
    """The centre, with the slack a growing claim is allowed to take from it."""

    def __init__(self, width, minimum):
        self._w, self._min = width, minimum

    def width(self):
        return self._w

    def minimumSizeHint(self):  # noqa: N802 - Qt name
        from PySide6.QtCore import QSize

        return QSize(self._min, 0)


class _Window:
    """Enough QMainWindow for the controller: a dock whose width the layout may refuse to grant.

    ``grant`` caps what resizeDocks hands out, standing in for a centre widget at its minimum.
    A resize event only follows when the width actually changed, as Qt does it.
    """

    def __init__(self, width=1400, grant=None, central_slack=10_000):
        self.qsar_chart_dock = _Dock()
        self._w, self._grant = width, grant
        self._central = _Central(central_slack, 0)
        self.controller = None

    def width(self):
        return self._w

    def centralWidget(self):  # noqa: N802 - Qt name
        return self._central

    def resizeDocks(self, docks, sizes, orientation):  # noqa: N802 - Qt name
        want = sizes[0] if self._grant is None else min(sizes[0], self._grant)
        if want != docks[0]._w:
            self._central._w -= want - docks[0]._w  # the centre gives up what the dock takes
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
    opened = int(900 * _CHART_WIDTH_SHARE)
    assert window.qsar_chart_dock.width() == opened

    # the same width again: a height-only resize decides nothing
    window.resizeDocks([window.qsar_chart_dock], [opened], Qt.Horizontal)
    window._w = 1920
    assert not controller._chosen

    controller.size_chart_to_third()
    _settle(controller)
    assert window.qsar_chart_dock.width() == int(1920 * _CHART_WIDTH_SHARE)


def test_a_chart_wider_than_its_share_hands_the_width_back():
    # Taking the viewer's slot can leave the chart wider than its share. Shrinking to the share
    # gives width back to the centre, which is what lets the centre meet the minimum of the tab
    # that just opened; refusing to shrink left the tool panel short by the difference.
    window = _Window(width=1920)
    controller = _controller(window)
    window.qsar_chart_dock._w = 692  # the 3D viewer's width, wider than a third
    controller._queue_widen(force=True)
    _settle(controller)
    assert window.qsar_chart_dock.width() == int(1920 * _CHART_WIDTH_SHARE)


def test_a_claim_grows_only_by_what_the_centre_can_spare():
    # The chart sits on the far side of the centre from the tool panel, so width the centre
    # cannot cover is taken off that panel and never handed back: opening a chart on a full
    # window cost Docking Studio 53 px. The chart gets the centre's slack and no more.
    window = _Window(width=1920, central_slack=40)
    controller = _controller(window)
    window.qsar_chart_dock._w = 500  # what the 3D viewer left behind
    controller._queue_widen(force=True)
    _settle(controller)
    assert controller._chart_width == int(1920 * _CHART_WIDTH_SHARE)  # it still wants its share
    assert window.qsar_chart_dock.width() == 540  # ...but takes only the centre's 40


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
