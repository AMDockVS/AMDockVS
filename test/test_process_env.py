"""The app's process keeps numpy to one BLAS thread: the computing is done by workers on the
cores the engine hands out, and `import numpy` started an OpenBLAS thread per core that burned
~1 s of CPU in the parent (M7 gate, 2026-11-25)."""
import os
import subprocess
import sys


def _blas_threads_after_import(told):
    env = {key: value for key, value in os.environ.items() if key != "OPENBLAS_NUM_THREADS"}
    if told is not None:
        env["OPENBLAS_NUM_THREADS"] = told
    code = "import amdockvs, os; print(os.environ.get('OPENBLAS_NUM_THREADS'))"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_importing_the_app_keeps_numpy_to_one_blas_thread_unless_told():
    assert _blas_threads_after_import(None) == "1"
    assert _blas_threads_after_import("3") == "3"
