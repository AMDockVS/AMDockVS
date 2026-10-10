"""The one per-user folder of AMDockVS: its settings, the tools it installs, its caches.

What the ``ms_*`` packages write (project registry, executors, logs) lives in the
MolSuite home, not here.  Kept free of imports so any module, a worker included, can ask.
"""
from __future__ import annotations

import os
from pathlib import Path


def amdock_home() -> Path:
    """``$AMDOCK_HOME`` when set (a cluster whose home is small, a test); else ``~/.amdockvs``."""
    configured = str(os.environ.get("AMDOCK_HOME") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".amdockvs"
