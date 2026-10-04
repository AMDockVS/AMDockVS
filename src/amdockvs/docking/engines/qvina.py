"""QuickVina 2 docking engine: Vina's scoring function with a faster local search.

QuickVina reads the SAME prepared PDBQT as Vina (preparation_engine="ad4") and writes the
same PDBQT poses, so the whole Vina runner is reused; only the binary and the options it
accepts differ (vina.py drops the ones QuickVina 2.1 does not know). It is the variant for
a known binding site, which is what every AMDock run has: a box per receptor. QuickVina-W
(blind docking over a whole protein) is a different binary and is not wired.
"""
from __future__ import annotations

import os
from pathlib import Path

from ms_flow.core.executor.tools import READY, Recipe, locate

from amdockvs.core.configuration import app_config
from amdockvs.core.paths import tools_home
from amdockvs.docking.engines.vina import run_vina_docking_rows
from amdockvs.integrations.envs import tool_spec


def qvina_recipe(runtime=None) -> Recipe:
    """Where QuickVina is looked up: $AMDOCK_QVINA, Settings > Docking > QuickVina, the managed
    install, then PATH."""
    registered = [os.environ.get("AMDOCK_QVINA"), app_config(runtime).docking.qvina.path]
    return tool_spec("qvina").recipe(
        str(tools_home(runtime)),
        extra_paths=[str(Path(path).expanduser().resolve()) for path in registered if str(path or "").strip()],
    )


def qvina_command(runtime=None) -> str:
    """The usable executable. Resolved per chunk, in the worker, so it reads the global config
    layer (not a project one). Runs the recipe check in a subprocess: keep it off the GUI thread."""
    state, executable = locate(qvina_recipe(runtime))
    if state != READY:
        raise RuntimeError(
            "QuickVina 2 is not installed. Install it from Settings > External tools, or set its "
            "path in Settings > Docking > QuickVina or $AMDOCK_QVINA."
        )
    return executable


def qvina_dock_runner(payload: dict) -> list[dict]:
    return run_vina_docking_rows(
        pairs=list(payload.get("pairs") or []),
        output_dir=str(payload.get("output_dir") or ""),
        box_center=[float(value) for value in (payload.get("box_center") or [])],
        box_size=[float(value) for value in (payload.get("box_size") or [])],
        scoring_function="vina",  # the only one QuickVina has
        vina_backend="binary",
        vina_command=qvina_command(),
        vina_cpu=int(payload.get("vina_cpu") or 1),
        seed=int(payload.get("seed") or 0),
        energy_range=float(payload.get("energy_range") or 3.0),
        run_id=str(payload.get("run_id") or ""),
        protocol_metadata=dict(payload.get("protocol_metadata") or {}),
        report_name=str(payload.get("report_name") or "").strip() or None,
        pair_callback=payload.get("_pair_callback"),
        collect_rows=bool(payload.get("_collect_rows", True)),
        engine="qvina",
    )


__all__ = ["qvina_command", "qvina_dock_runner", "qvina_recipe"]
