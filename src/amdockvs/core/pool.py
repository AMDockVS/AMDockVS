"""Process pools the GUI spawns for itself, which never outlive it.

A `ProcessPoolExecutor` worker only exits when its pool shuts down. When the app crashes, is
killed or leaves through `os._exit`, nobody shuts it down: the worker and its
`resource_tracker` are adopted by init and stay forever (106 of them, 4.2 GB, had piled up by
2026-11-19). Each worker here watches its parent and exits when it goes.
"""
from __future__ import annotations

import multiprocessing
import multiprocessing.connection
import os
import threading
from concurrent.futures import ProcessPoolExecutor


def _exit_with_parent() -> None:
    """Pool initializer: exit as soon as the process that started this one is gone.

    Waits on the parent's sentinel (a pipe on POSIX, the process handle on Windows), so it
    costs nothing while the parent lives and works on every platform."""
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch() -> None:
        multiprocessing.connection.wait([parent.sentinel])
        os._exit(1)

    threading.Thread(target=watch, name="amdockvs-parent-watch", daemon=True).start()


def spawn_pool(workers: int = 1) -> ProcessPoolExecutor:
    """A pool of `workers` spawned processes that exit with the app."""
    return ProcessPoolExecutor(
        workers, mp_context=multiprocessing.get_context("spawn"), initializer=_exit_with_parent
    )


__all__ = ["spawn_pool"]
