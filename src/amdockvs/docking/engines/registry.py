"""Docking runner registry and Molsuite chunk dispatcher."""
from __future__ import annotations

from typing import Any, Callable

from amdockvs.core.worker_io import project_root_from_output_dir
from amdockvs.core.paths import set_default_project_root

DockRunner = Callable[[dict[str, Any]], list[dict[str, Any]]]

DOCK_RUNNERS: dict[str, DockRunner] = {}
_BUILTINS_LOADED = False


def register_dock_runner(engine: str, runner: DockRunner, *, replace: bool = False) -> None:
    key = str(engine or "").strip().lower()
    if not key:
        raise ValueError("A docking runner requires a non-empty engine key.")
    if not callable(runner):
        raise TypeError("A docking runner must be callable.")
    if key in DOCK_RUNNERS and not replace:
        raise ValueError(f"Docking runner '{key}' is already registered.")
    DOCK_RUNNERS[key] = runner


def register_docking_engine(program, runner: DockRunner, *, replace: bool = False) -> None:
    """Register one complete extension: program metadata plus its chunk runner."""
    from amdockvs.docking.engines.programs import list_docking_programs, register_docking_program

    program_key = str(program.key or "").strip().lower()
    runner_key = str(program.docking_engine or "").strip().lower()
    if not program_key or not runner_key:
        raise ValueError("A docking extension requires non-empty program and engine keys.")
    if not callable(runner):
        raise TypeError("A docking runner must be callable.")
    if not replace:
        if any(str(item.key).strip().lower() == program_key for item in list_docking_programs()):
            raise ValueError(f"Docking program '{program_key}' is already registered.")
        if runner_key in DOCK_RUNNERS:
            raise ValueError(f"Docking runner '{runner_key}' is already registered.")

    register_docking_program(program, replace=replace)
    register_dock_runner(runner_key, runner, replace=replace)


def load_builtin_docking_engines() -> None:
    """Load built-ins explicitly; importing an engine module has no side effects."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    from amdockvs.docking.engines.autodock4 import autodock4_dock_runner
    from amdockvs.docking.engines.vina import _vina_dock_runner
    from amdockvs.docking.engines.gnina import gnina_dock_runner

    register_dock_runner("vina", _vina_dock_runner, replace=True)
    register_dock_runner("autodock4", autodock4_dock_runner, replace=True)
    register_dock_runner("gnina", gnina_dock_runner, replace=True)
    _BUILTINS_LOADED = True


def run_docking_chunk(payload: dict) -> list[dict]:
    load_builtin_docking_engines()
    payload = dict(payload)
    reserved = {
        "engine",
        "engine_config",
        "output_dir",
        "pairs",
        "protocol_metadata",
        "run_id",
        "_collect_rows",
        "_pair_callback",
    }
    for key, value in dict(payload.get("engine_config") or {}).items():
        if str(key) not in reserved:
            payload[str(key)] = value
    output_dir = str(payload.get("output_dir") or "").strip()
    if output_dir:
        set_default_project_root(project_root_from_output_dir(output_dir))
    engine = str(payload.get("engine") or "vina").strip().lower()
    runner = DOCK_RUNNERS.get(engine)
    if runner is None:
        raise ValueError(
            f"No docking runner registered for engine '{engine}'. "
            f"Registered engines: {sorted(DOCK_RUNNERS)}."
        )
    rows = runner(payload)
    if payload.get("compute_diagram") and rows:
        from amdockvs.docking.results.diagram import render_diagrams_for_result_rows

        render_diagrams_for_result_rows(rows, fmt=str(payload.get("diagram_format") or "png"))
    return rows


__all__ = [
    "DOCK_RUNNERS",
    "DockRunner",
    "load_builtin_docking_engines",
    "register_dock_runner",
    "register_docking_engine",
    "run_docking_chunk",
]
