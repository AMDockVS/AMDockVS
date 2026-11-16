"""Uni-Dock engine: the Vina search and scoring run for many ligands at once on an NVIDIA GPU.

Reads the same prepared PDBQT as Vina (preparation_engine="ad4") and writes the same PDBQT
poses (REMARK VINA RESULT), so the Vina parsing and SDF conversion are reused. The point of
Uni-Dock is the batch: one process docks every ligand of a site in parallel on the card, so
a task docks all its pairs that share a receptor and box in one `--ligand_index` run.

Uni-Dock sorts a run's ligands by size into groups and docks each group in GPU batches as
large as the card's memory allows; it writes a batch's poses before it says "Batch k running
time". Each pair is reported as its batch ends, so rows, hits and progress move per GPU batch.

Uni-Dock aborts the whole run on a ligand it cannot read; what a failed run did not dock is
retried one ligand at a time, so a bad ligand fails only its own pair.
ponytail: scoring is vina/vinardo; ad4 needs autogrid4 maps passed with --maps, add it the
way adgpu.py does if asked.
"""
from __future__ import annotations

import os
import re
import shutil
from contextlib import suppress
from pathlib import Path
from typing import IO, Callable

from ms_env import READY, Recipe, locate

from amdockvs.core.configuration import app_config
from amdockvs.core.paths import get_default_project_root, tools_home
from amdockvs.docking.engines.vina import (
    _convert_vina_pdbqt_to_sdf,
    _parse_vina_pose_energies,
    _require_pdbqt,
    _run_vina,
    count_heavy_atoms,
)
from amdockvs.docking.methods import check_program, check_receptor
from amdockvs.docking.preparation.adtools import dockable_ligand
from amdockvs.docking.results.metrics import docking_row, pose_file_stem
from amdockvs.docking.results.rmsd import pose_rmsd_detail
from amdockvs.integrations.envs import tool_spec

ENGINE = "unidock"
_BATCH_DONE = re.compile(r"Batch \d+ running time")  # the batch's poses are written by then


def unidock_recipe(runtime=None) -> Recipe:
    """Where Uni-Dock is looked up: $AMDOCK_UNIDOCK, Settings > Docking > Uni-Dock, the managed
    install, then PATH."""
    registered = [os.environ.get("AMDOCK_UNIDOCK"), app_config(runtime).docking.unidock.path]
    return tool_spec("unidock").recipe(
        str(tools_home(runtime)),
        extra_paths=[str(Path(path).expanduser().resolve()) for path in registered if str(path or "").strip()],
    )


def unidock_command(runtime=None) -> str:
    """The usable executable. Resolved per chunk, in the worker, so it reads the global config
    layer (not a project one). Runs the recipe check in a subprocess: keep it off the GUI thread."""
    state, executable = locate(unidock_recipe(runtime))
    if state != READY:
        raise RuntimeError(
            "Uni-Dock is not installed. Install it from Settings > External tools, or set its "
            "path in Settings > Docking > Uni-Dock or $AMDOCK_UNIDOCK."
        )
    return executable


def _run_unidock(
    *,
    executable: str,
    site: tuple,
    ligands: dict[str, Path],
    work: Path,
    options: list[str],
    batch_done: Callable[[], None],
) -> dict[str, Path | str]:
    """Dock `ligands` (output name → PDBQT) over one site in one process, calling `batch_done`
    as each GPU batch has written its poses. Each name maps to its pose PDBQT, or to why it failed."""
    receptor_path, center, size, flex_path = site
    work.mkdir(parents=True, exist_ok=True)
    for name, path in ligands.items():  # Uni-Dock names each output after its input file
        link = work / f"{name}.pdbqt"
        link.unlink(missing_ok=True)
        link.symlink_to(path)
    index = work / "ligands.txt"
    index.write_text("".join(f"{work / name}.pdbqt\n" for name in ligands))
    command = [executable, "--receptor", str(receptor_path), "--ligand_index", str(index), "--dir", str(work)]
    for axis, c, s in zip("xyz", center, size):
        command += [f"--center_{axis}", str(c), f"--size_{axis}", str(s)]
    if flex_path is not None:
        command += ["--flex", str(flex_path)]

    def follow(stream: IO[str]) -> str:
        lines = []
        for line in stream:
            lines.append(line)
            if _BATCH_DONE.match(line):
                batch_done()
        return "".join(lines)

    why = "Uni-Dock wrote no poses"
    try:
        _run_vina([*command, *options], follow=follow, program="Uni-Dock")
    except RuntimeError as exc:
        why = str(exc)[-500:]  # the reason Uni-Dock gives is at the end
    outputs = {name: work / f"{name}_out.pdbqt" for name in ligands}
    return {name: path if path.exists() else why for name, path in outputs.items()}


def unidock_dock_runner(payload: dict) -> list[dict]:
    pairs = list(payload.get("pairs") or [])
    pair_callback = payload.get("_pair_callback")
    collect_rows = bool(payload.get("_collect_rows", True))
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    default_center = [float(v) for v in (payload.get("box_center") or [])]
    default_size = [float(v) for v in (payload.get("box_size") or [])]
    scoring_function = str(payload.get("scoring_function") or "vina")
    num_modes = int(payload.get("num_modes") or 9)
    seed = int(payload.get("seed") or 0)
    options = [
        "--scoring", scoring_function,
        "--search_mode", str(payload.get("search_mode") or "fast"),
        "--num_modes", str(num_modes),
        "--energy_range", str(float(payload.get("energy_range") or 3.0)),
        "--cpu", "1",  # the task holds one CPU token; the search runs on the GPU
        *(["--seed", str(seed)] if seed else []),  # 0 keeps the program's own random seed
    ]
    protocol_payload = dict(payload.get("protocol_metadata") or {})
    project_root = get_default_project_root()
    method = check_program(payload.get("method"), ENGINE, scoring_function)

    rows: list[dict] = []

    def report(pair_rows: list[dict]) -> None:
        if collect_rows:
            rows.extend(pair_rows)
        if pair_callback is not None:
            pair_callback(pair_rows)

    sites: dict[tuple, list[dict]] = {}  # (receptor, box, flexres) → its prepared pairs
    for pair in pairs:
        ligand_path = Path(str(pair.get("ligand_path") or "")).expanduser().resolve()
        receptor_path = Path(str(pair.get("receptor_path") or "")).expanduser().resolve()
        center = [float(v) for v in (pair.get("box_center") or default_center)]
        size = [float(v) for v in (pair.get("box_size") or default_size)]
        try:
            invalid_reason = str(pair.get("invalid_reason") or "").strip()
            if invalid_reason:
                raise ValueError(invalid_reason)
            for label, path in (("ligand", ligand_path), ("receptor", receptor_path)):
                if not path.exists():
                    raise FileNotFoundError(f"{label} file missing: {path}")
                _require_pdbqt(path, kind=label)
            if len(center) != 3 or len(size) != 3:
                raise ValueError("Uni-Dock requires a box (center_xyz + size_xyz).")
            check_receptor(method, receptor_path, center, size)
            ligand_path = dockable_ligand(ligand_path, program=ENGINE, scoring_function=scoring_function,
                                          output_dir=output_dir)
            # Same convention as the Vina runner: flexible-residue prep leaves "<receptor>__flex.pdbqt".
            explicit_flex = str(pair.get("flex_receptor_path") or "").strip()
            flex_path = Path(explicit_flex) if explicit_flex else receptor_path.with_name(f"{receptor_path.stem}__flex.pdbqt")
            flex_path = flex_path if flex_path.exists() else None
        except Exception as exc:
            report([docking_row(pair, score_type="vina_score", error=str(exc))])
            continue
        site = (receptor_path, tuple(center), tuple(size), flex_path)
        sites.setdefault(site, []).append(
            {"pair": pair, "ligand_path": ligand_path, "name": f"{pose_file_stem(pair, protocol_payload)}.unidock"})

    executable = ""
    for site, group in sites.items():
        work = output_dir / f"_unidock_{group[0]['name']}"
        shutil.rmtree(work, ignore_errors=True)  # poses left by an interrupted run are not this run's
        reported: set[str] = set()

        def collect(group=group, work=work, reported=reported) -> None:
            """Report the pairs whose poses are written."""
            for item in group:
                poses = work / f"{item['name']}_out.pdbqt"
                if item["name"] not in reported and poses.exists():
                    reported.add(item["name"])
                    report(_pair_rows(item, poses, output_dir, project_root))

        try:
            executable = executable or unidock_command()
            batch = {item["name"]: item["ligand_path"] for item in group}
            outcome = _run_unidock(executable=executable, site=site, ligands=batch, work=work, options=options,
                                   batch_done=collect)
            retry = [name for name, value in outcome.items() if not isinstance(value, Path)]
            if len(batch) > 1:  # one unreadable ligand stops the run: the rest go one by one
                for name in retry:
                    outcome.update(_run_unidock(executable=executable, site=site, ligands={name: batch[name]},
                                                work=work, options=options, batch_done=collect))
        except Exception as exc:  # no program, or it did not run: every pair not yet reported fails
            outcome = {item["name"]: str(exc) for item in group}
        for item in group:
            if item["name"] not in reported:
                report(_pair_rows(item, outcome[item["name"]], output_dir, project_root))
        shutil.rmtree(work, ignore_errors=True)  # the poses are in the SDFs now
    return rows


def _pair_rows(item: dict, poses: Path | str, output_dir: Path, project_root: Path | None) -> list[dict]:
    pair = item["pair"]
    output_sdf = output_dir / f"{item['name']}.sdf"
    try:
        if not isinstance(poses, Path):
            raise RuntimeError(poses)
        energies = _parse_vina_pose_energies(output_path=poses)
        if not energies:
            raise RuntimeError(f"Uni-Dock wrote no poses for {item['ligand_path'].name}")
        _convert_vina_pdbqt_to_sdf(pdbqt_path=poses, sdf_path=output_sdf)
    except Exception as exc:
        return [docking_row(pair, score_type="vina_score", error=str(exc))]

    pose_text = str(output_sdf)
    if project_root is not None:
        with suppress(Exception):
            pose_text = str(output_sdf.relative_to(project_root))
    heavy_atoms = count_heavy_atoms(item["ligand_path"])
    redocking = str(pair.get("run_kind") or "screening") == "redocking"
    return [
        docking_row(
            pair,
            score_type="vina_score",
            pose_rank=rank,
            score=float(energy[0]),
            pose_path=pose_text,
            rmsd=pose_rmsd_detail(
                reference_ligand_path=pair.get("reference_ligand_path"),
                pose_path=output_sdf,
                pose_rank=rank,
            ) if redocking else None,
            heavy_atoms=heavy_atoms,
        )
        for rank, energy in enumerate(energies, start=1)
    ]


__all__ = ["unidock_command", "unidock_dock_runner", "unidock_recipe"]
