"""Every runtime a test makes is shut down after it.

Closing the project leaves the engine running, and with it the pool of workers its first process
job started (one per CPU, each gone only after 60 s idle): a suite piles them up by the dozen.
"""

import weakref

import pytest

from amdockvs.runtime import AMDockVSRuntime

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
