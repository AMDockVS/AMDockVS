"""Every runtime a test makes is shut down after it.

Closing the project leaves the engine running, and with it the pool of workers its first process
job started (one per CPU, each gone only after 60 s idle): a suite piles them up by the dozen.

Each pytest process (each xdist worker too) gets its own empty HOME, so no test ever touches the
real user folders and parallel workers never share one. Tests that start a local Ray cluster run
one after another on a single worker (`--dist loadgroup`).

`AMDOCK_TEST_MEM=<file>` appends, per test, the peak and the growth of this process plus all its
children (engine child, pools, Ray), to find the test that makes the memory climb.
"""

import atexit
import os
import shutil
import tempfile
import threading
import time
import weakref

_HOME = tempfile.mkdtemp(prefix="amdock-test-home-")
os.environ["HOME"] = _HOME
os.environ.pop("MOLSUITE_HOME", None)
for _name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
    os.environ.pop(_name, None)
atexit.register(shutil.rmtree, _HOME, True)

import pytest  # noqa: E402

from amdockvs.runtime import AMDockVSRuntime  # noqa: E402

_SERIAL = {"test_docking_poses.py", "test_run_destinations.py"}  # they start a local Ray cluster


_QT = ("PySide6", "QApplication", "amdockvs.ui", "ms_components.ms_monitor", "ms_components.labels")
_uses_qt: dict = {}


def _qt_module(path) -> bool:
    if path not in _uses_qt:
        text = path.read_text(errors="replace")
        _uses_qt[path] = any(name in text for name in _QT)
    return _uses_qt[path]


@pytest.hookimpl(tryfirst=True)  # before xdist reads the group
def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name in _SERIAL:
            item.add_marker(pytest.mark.xdist_group("serial"))
        if "e2e" in item.path.parts:
            item.add_marker(pytest.mark.e2e)
        if _qt_module(item.path):
            item.add_marker(pytest.mark.ui)



_MEM = os.environ.get("AMDOCK_TEST_MEM")
_peak = [0]


def _tree_rss():
    import psutil

    me = psutil.Process()
    total = 0
    for process in [me, *me.children(recursive=True)]:
        try:
            total += process.memory_info().rss
        except psutil.Error:
            pass
    return total


def _sample():
    while True:
        _peak[0] = max(_peak[0], _tree_rss())
        time.sleep(0.25)


if _MEM:
    threading.Thread(target=_sample, daemon=True, name="test-mem").start()

    @pytest.fixture(autouse=True)
    def _memory(request):
        start = _peak[0] = _tree_rss()
        yield
        peak, mb = max(_peak[0], _tree_rss()), 1 << 20
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        with open(_MEM, "a") as out:
            out.write(f"{peak // mb}\t{(peak - start) // mb}\t{worker}\t{request.node.nodeid}\n")

_made = weakref.WeakSet()
_init = AMDockVSRuntime.__init__


def _tracked_init(self, *args, **kwargs):
    _init(self, *args, **kwargs)
    _made.add(self)


AMDockVSRuntime.__init__ = _tracked_init


@pytest.fixture(autouse=True)
def _shut_down_runtimes():
    yield
    for runtime in list(_made):
        runtime.shutdown()
    _made.clear()


@pytest.fixture
def inline_engine(monkeypatch):
    """The engine in this process, not in ms_flow's child: for a test that patches or records
    here what its jobs do."""
    monkeypatch.setenv("MSFLOW_ENGINE_HOST", "inline")
