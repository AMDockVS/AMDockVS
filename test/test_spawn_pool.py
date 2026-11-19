"""A pool the GUI spawns never outlives it, however it ends (`amdockvs.core.pool`)."""
import os
import subprocess
import sys
import time

import pytest

PARENT = """
import os, sys
from amdockvs.core.pool import spawn_pool
if __name__ == "__main__":
    pool = spawn_pool(1)
    print(pool.submit(os.getpid).result(), flush=True)
    os._exit(0)  # no shutdown: a worker without the guard stays forever
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat") as stat:  # a zombie is gone, only not reaped yet
        return stat.read().split(")")[-1].split()[0] != "Z"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_the_worker_exits_when_the_app_dies_without_shutting_it_down(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT)
    pid_file = tmp_path / "worker.pid"
    with pid_file.open("w") as out:  # a file, not a pipe: a surviving worker would hold a pipe open
        subprocess.run([sys.executable, str(script)], stdout=out, timeout=60, check=True)
    worker = int(pid_file.read_text().strip())
    deadline = time.time() + 10
    while _alive(worker) and time.time() < deadline:
        time.sleep(0.1)
    if _alive(worker):
        os.kill(worker, 9)
        pytest.fail("the pool worker outlived the process that spawned it")
