"""Shared helpers of the M0 baseline scripts. Import this module FIRST: it points HOME at a
throwaway folder before amdockvs / ms_flow are imported (projects register under ~/.molsuite)."""
import atexit
import json
import os
import random
import resource
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# a spawned worker re-imports this module: it must see the SAME home, not make another one
_OWN = "BENCH_HOME" not in os.environ
_HOME = os.environ.get("BENCH_HOME") or tempfile.mkdtemp(prefix="bench_home_")
os.environ["BENCH_HOME"] = _HOME
if _OWN:  # the process that made it removes it; projects of 1M records are big
    atexit.register(shutil.rmtree, _HOME, True)
os.environ["HOME"] = _HOME
os.environ["MOLSUITE_HOME"] = str(Path(_HOME) / ".molsuite")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TOKENS = ["C", "CC", "N", "O", "S", "C(=O)", "C(F)", "C(Cl)", "C(=O)N", "c1ccccc1", "c1ccncc1",
          "C1CC1", "C1CCNCC1", "c1ccsc1", "C#N", "CO", "CN"]


def make_smiles(path: Path, records: int, seed: int) -> Path:
    """`records` distinct-by-name, RDKit-valid molecules: 4-9 tokens of TOKENS joined in a
    chain, drawn from random.Random(seed). Deterministic for (records, seed)."""
    from rdkit import Chem, RDLogger

    RDLogger.DisableLog("rdApp.*")
    rng = random.Random(seed)
    with open(path, "w") as handle:
        n = 0
        while n < records:
            smi = "".join(rng.choice(TOKENS) for _ in range(rng.randint(4, 9)))
            if Chem.MolFromSmiles(smi) is None:
                continue
            handle.write(f"{smi} M{n:08d}\n")
            n += 1
    return path


def peak_rss_mb() -> float:
    """VmHWM of this process, MiB (falls back to ru_maxrss)."""
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def cpu_s() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


class ChildSampler(threading.Thread):
    """Polls the descendants of this process every `interval` s (psutil, RSS). Records the peak
    RSS of any one descendant and the peak of the sum over all descendants alive together."""

    def __init__(self, interval: float = 0.1):
        super().__init__(daemon=True)
        import psutil

        self._psutil, self._me, self._stop_event = psutil, psutil.Process(), threading.Event()
        self.interval = interval
        self.peak_one = 0.0
        self.peak_sum = 0.0
        self.peak_count = 0

    def run(self):
        while not self._stop_event.is_set():
            total, count = 0, 0
            for child in self._me.children(recursive=True):
                try:
                    rss = child.memory_info().rss
                except self._psutil.Error:
                    continue
                total += rss
                count += 1
                self.peak_one = max(self.peak_one, rss / 2**20)
            self.peak_sum = max(self.peak_sum, total / 2**20)
            self.peak_count = max(self.peak_count, count)
            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()
        self.join()


def open_project(name: str):
    from amdockvs import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name=name, folder=Path(_HOME) / name)
    return runtime


def run_import(runtime, smi: Path, executor: str = "compute"):
    """The app's own `htpvs` library import (`runtime.loader.shard_ligands`), waited for."""
    job_ids = runtime.loader.shard_ligands([smi], executor_name=executor)
    final = runtime.wait_for_jobs(job_ids, timeout_s=6 * 3600, poll_s=0.1)
    bad = {j: r.status for j, r in final.items() if r.status != "completed"}
    if bad:
        raise RuntimeError(f"import did not complete: {bad}")


def emit(result: dict, out: str | None):
    line = json.dumps(result)
    print(line)
    if out:
        Path(out).write_text(line + "\n")
