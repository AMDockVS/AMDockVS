"""measure.py <label> <script>: wall, CPU, peak PSS/RSS of the whole tree, disk written, files left."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

label, script = sys.argv[1], sys.argv[2]
out = Path(f"/tmp/bench_vs/out_{label}")
subprocess.run(["rm", "-rf", str(out)])
out.mkdir()
inner = (f"python {script} {out}; rc=$?; cg=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup); "
         f"cat $cg/cpu.stat $cg/memory.peak > {out}/cgroup.txt; exit $rc")
def sectors_written():
    for line in open("/proc/diskstats"):
        f = line.split()
        if f[2] == "nvme0n1p3":
            return int(f[9])


subprocess.run(["sync"])
w0 = sectors_written()
start = time.monotonic()
proc = subprocess.Popen(["systemd-run", "--user", "--scope", "-q", "-p", "IOAccounting=yes",
                         "taskset", "-c", "0-13", "bash", "-c", inner],
                        stdout=open(out / "stdout.log", "w"), stderr=subprocess.STDOUT)
peak = {"pss": 0, "rss": 0, "procs": 0}
timeline = []


def sample():
    root = psutil.Process(proc.pid)
    while proc.poll() is None:
        pss = rss = 0
        try:
            tree = [root, *root.children(recursive=True)]
        except psutil.Error:
            break
        for p in tree:
            try:
                info = p.memory_full_info()
                pss, rss = pss + info.pss, rss + info.rss
            except psutil.Error:
                pass
        peak["pss"], peak["rss"], peak["procs"] = max(peak["pss"], pss), max(peak["rss"], rss), max(peak["procs"], len(tree))
        timeline.append((round(time.monotonic() - start, 1), round(pss / 2**20), len(tree)))
        time.sleep(0.5)


thread = threading.Thread(target=sample, daemon=True)
thread.start()
rc = proc.wait()
wall = time.monotonic() - start
thread.join()
subprocess.run(["sync"])
disk_mb = (sectors_written() - w0) * 512 / 2**20  # ponytail: device-wide, idle machine assumed
cg = (out / "cgroup.txt").read_text().split()
stat = dict(zip(cg[0:12:2], map(int, cg[1:12:2])))
files = [p for p in out.rglob("*") if p.is_file() and "home" not in p.relative_to(out).parts[:1]]
result = {
    "label": label, "rc": rc, "wall_s": round(wall, 1),
    "cpu_user_s": round(stat["user_usec"] / 1e6, 1), "cpu_sys_s": round(stat["system_usec"] / 1e6, 1),
    "cpu_util_of_14": round(stat["usage_usec"] / 1e6 / wall / 14, 3),
    "peak_pss_mb": round(peak["pss"] / 2**20), "peak_rss_sum_mb": round(peak["rss"] / 2**20),
    "cgroup_memory_peak_mb": round(int(cg[-1]) / 2**20), "max_procs": peak["procs"],
    "disk_written_mb": round(disk_mb, 1),
    "files_left": len(files), "bytes_left_mb": round(sum(p.stat().st_size for p in files) / 2**20, 1),
}
(out / "timeline.json").write_text(json.dumps(timeline))
(out / "result.json").write_text(json.dumps(result, indent=1))
print(json.dumps(result, indent=1))
