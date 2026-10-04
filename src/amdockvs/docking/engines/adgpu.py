"""AutoDock-GPU docking engine: the AutoDock4 force field and genetic search on the GPU.

Reads the same prepared PDBQT as Vina (preparation_engine="ad4"). The receptor reaches it
only as autogrid4 maps, which is where a custom parameter file acts. An atom type the
program does not know (one added by that file) is declared as derived from a known one
(`derived_types`, e.g. "XX=OA"): it takes its receptor terms from its own map and its
internal ligand terms from the base type.

One process saturates the GPU (measured: 1, 2, 4 and 8 concurrent processes all dock the
same ligands per second), so each chunk takes one GPU token and docks its pairs in turn.
ponytail: one adgpu process per pair; --filelist per receptor if start-up ever dominates.
"""
from __future__ import annotations

import json
import os
import subprocess
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from ms_flow.core.executor.tools import READY, Recipe, locate

from amdockvs.core.configuration import app_config
from amdockvs.core.paths import get_default_project_root, tools_home
from amdockvs.docking.engines.autogrid import map_prefix
from amdockvs.docking.engines.vina import count_heavy_atoms
from amdockvs.docking.results.metrics import docking_metrics
from amdockvs.docking.results.rmsd import pose_rmsd_detail
from amdockvs.integrations.envs import tool_spec

ENGINE = "adgpu"
DEFAULT_SPACING = 0.375


def adgpu_recipe(runtime=None) -> Recipe:
    """Where AutoDock-GPU is looked up: $AMDOCK_ADGPU, Settings > Docking > AutoDock-GPU, the
    managed install, then PATH."""
    registered = [os.environ.get("AMDOCK_ADGPU"), app_config(runtime).docking.adgpu.path]
    return tool_spec("adgpu").recipe(
        str(tools_home(runtime)),
        extra_paths=[str(Path(path).expanduser().resolve()) for path in registered if str(path or "").strip()],
    )


def adgpu_command(runtime=None) -> str:
    """The usable executable. Resolved per chunk, in the worker, so it reads the global config
    layer (not a project one). Runs the recipe check in a subprocess: keep it off the GUI thread."""
    state, executable = locate(adgpu_recipe(runtime))
    if state != READY:
        raise RuntimeError(
            "AutoDock-GPU is not installed. Install it from Settings > External tools, or set its "
            "path in Settings > Docking > AutoDock-GPU or $AMDOCK_ADGPU."
        )
    return executable


def _run_adgpu(
    *,
    executable: str,
    maps_prefix: str,
    ligand_path: Path,
    flex_receptor_path: Path | None,
    output_dir: Path,
    name: str,
    nrun: int,
    seed: int,
    derived_types: str,
) -> Path:
    command = [
        executable,
        "--ffile", f"{maps_prefix}.maps.fld",
        "--lfile", str(ligand_path),
        "--resnam", name,
        "--nrun", str(int(nrun)),
        "--xmloutput", "0",
    ]
    if flex_receptor_path is not None:
        command += ["--flexres", str(flex_receptor_path)]
    if int(seed):
        command += ["--seed", str(int(seed))]  # 0 keeps the program's own time-based seed
    if derived_types.strip():
        command += ["--derivtype", derived_types.strip()]
    result = subprocess.run(command, cwd=str(output_dir), capture_output=True, text=True)
    dlg = output_dir / f"{name}.dlg"
    # The exit code is 0 even when the job failed: the text is the only signal.
    if "All jobs ran without errors" not in result.stdout or not dlg.exists():
        output = f"{result.stdout}\n{result.stderr}"
        errors = [line.strip() for line in output.splitlines() if line.strip().startswith("Error")]
        raise RuntimeError(f"AutoDock-GPU failed: {' '.join(errors) or output.strip()[-500:] or 'no output'}")
    return dlg


def _cluster_leads(dlg_path: Path, sdf_path: Path, num_modes: int) -> list[dict]:
    """Write the best pose of each cluster (lowest energy first) as SDF and return their
    energies, in the same order."""
    from meeko import PDBQTMolecule, RDKitMolCreate

    # skip_typing: Meeko's type table has no entry for types added by a parameter file; the
    # chemistry comes from the SMILES remark, not from the AutoDock types.
    molecule = PDBQTMolecule(
        dlg_path.read_text(encoding="utf-8", errors="replace"), name=dlg_path.stem, is_dlg=True, skip_typing=True
    )
    sd_string, _failures = RDKitMolCreate.write_sd_string(molecule, only_cluster_leads=True, keep_flexres=False)
    blocks = [block for block in str(sd_string or "").split("$$$$\n") if block.strip()][: max(1, int(num_modes))]
    if not blocks:
        raise RuntimeError(f"Meeko could not read poses from {dlg_path.name}")
    sdf_path.write_text("".join(f"{block}$$$$\n" for block in blocks), encoding="utf-8")
    poses: list[dict] = []
    for block in blocks:
        lines = block.splitlines()
        tag = next(index for index, line in enumerate(lines) if line.startswith(">  <meeko>"))
        poses.append(json.loads(lines[tag + 1]))
    return poses


def _resolve_optional(value: object, project_root: Path | None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    return path.resolve()


def adgpu_dock_runner(payload: dict) -> list[dict]:
    pairs = list(payload.get("pairs") or [])
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    default_center = [float(v) for v in (payload.get("box_center") or [])]
    default_size = [float(v) for v in (payload.get("box_size") or [])]
    nrun = int(payload.get("nrun") or 20)
    num_modes = int(payload.get("num_modes") or 9)
    seed = int(payload.get("seed") or 0)
    derived_types = str(payload.get("derived_types") or "")
    run_id = str(payload.get("run_id") or "")
    protocol_payload = dict(payload.get("protocol_metadata") or {})
    project_root = get_default_project_root()
    map_cache: dict[tuple, str] = {}

    rows: list[dict] = []
    for pair in pairs:
        complex_id = int(pair.get("complex_id") or 0) or None
        run_kind = str(pair.get("run_kind") or "screening")
        ligand_id = int(pair.get("ligand_id") or 0)
        receptor_id = int(pair.get("receptor_id") or 0)
        ligand_path = Path(str(pair.get("ligand_path") or "")).expanduser().resolve()
        receptor_path = Path(str(pair.get("receptor_path") or "")).expanduser().resolve()
        center = [float(v) for v in (pair.get("box_center") or default_center)]
        size = [float(v) for v in (pair.get("box_size") or default_size)]
        spacing = float(pair.get("spacing") or DEFAULT_SPACING)
        base = {
            "engine": ENGINE,
            "run_kind": run_kind,
            "complex_id": complex_id,
            "ligand_id": ligand_id,
            "receptor_id": receptor_id,
            "ligand_path": str(pair.get("ligand_path_logical") or ligand_path),
            "receptor_path": str(pair.get("receptor_path_logical") or receptor_path),
            "reference_ligand_path": str(
                pair.get("reference_ligand_path_logical") or pair.get("reference_ligand_path") or ""
            ),
            "protocol": protocol_payload,
        }
        output_stem = f"{run_kind}_{complex_id}" if complex_id is not None else f"{ligand_id}__{receptor_id}"
        output_sdf = output_dir / f"{output_stem}.adgpu.sdf"
        try:
            invalid_reason = str(pair.get("invalid_reason") or "").strip()
            if invalid_reason:
                raise ValueError(invalid_reason)
            for label, path in (("ligand", ligand_path), ("receptor", receptor_path)):
                if not path.exists():
                    raise FileNotFoundError(f"{label} file missing: {path}")
            if len(center) != 3 or len(size) != 3:
                raise ValueError("AutoDock-GPU requires a box (center_xyz + size_xyz).")
            parameter_file = _resolve_optional(payload.get("parameter_file"), project_root)
            if parameter_file is not None and not parameter_file.is_file():
                raise FileNotFoundError(f"parameter file missing: {parameter_file}")
            # Same convention as the Vina runner: flexible-residue prep leaves "<receptor>__flex.pdbqt".
            explicit_flex = str(pair.get("flex_receptor_path") or "").strip()
            flex_path = Path(explicit_flex) if explicit_flex else receptor_path.with_name(f"{receptor_path.stem}__flex.pdbqt")
            flex_path = flex_path if flex_path.exists() else None
            prefix = map_prefix(
                cache=map_cache,
                maps_dir=output_dir / "_ad4_maps",
                receptor_path=receptor_path,
                ligand_path=ligand_path,
                box_center=center,
                box_size=size,
                spacing=spacing,
                flex_receptor_path=flex_path,
                parameter_file=parameter_file,
            )
            dlg = _run_adgpu(
                executable=adgpu_command(),
                maps_prefix=prefix,
                ligand_path=ligand_path,
                flex_receptor_path=flex_path,
                output_dir=output_dir,
                name=f"{output_stem}.adgpu",
                nrun=nrun,
                seed=seed,
                derived_types=derived_types,
            )
            poses = _cluster_leads(dlg, output_sdf, num_modes)
            dlg.unlink()  # every pose kept is in the SDF; the DLG repeats all runs
        except Exception as exc:
            rows.append(
                {
                    "receptor_molecule_id": receptor_id,
                    "ligand_molecule_id": ligand_id,
                    "engine": ENGINE,
                    "pose_rank": 1,
                    "score": None,
                    "score_type": "ad4_score",
                    "pose_path": "",
                    "rmsd_vs_reference": None,
                    "metrics": {**base, "status": "failed", "error": str(exc)},
                    "created_at": datetime.now(),
                }
            )
            continue

        pose_text = str(output_sdf)
        if project_root is not None:
            with suppress(Exception):
                pose_text = str(output_sdf.relative_to(project_root))
        ligand_source_path = _resolve_optional(pair.get("ligand_source_path"), project_root)
        heavy_atoms = count_heavy_atoms(ligand_path)
        for rank, pose in enumerate(poses, start=1):
            score = float(pose["free_energy"])
            rmsd_detail = (
                pose_rmsd_detail(
                    reference_ligand_path=pair.get("reference_ligand_path"),
                    pose_path=output_sdf,
                    pose_rank=rank,
                )
                if run_kind == "redocking"
                else None
            )
            rows.append(
                {
                    "receptor_molecule_id": receptor_id,
                    "ligand_molecule_id": ligand_id,
                    "engine": ENGINE,
                    "pose_rank": rank,
                    "score": score,
                    "score_type": "ad4_score",
                    "pose_path": pose_text,
                    "rmsd_vs_reference": None if rmsd_detail is None else rmsd_detail[0],
                    "metrics": {
                        **base,
                        "ligand_source_path": str(
                            pair.get("ligand_source_path_logical") or pair.get("ligand_source_path") or ""
                        ),
                        "reference_receptor_path": str(
                            pair.get("reference_receptor_path_logical") or pair.get("reference_receptor_path") or ""
                        ),
                        "selected_pose_path": pose_text,
                        "grid": {"box_center": center, "box_size": size, "spacing": spacing},
                        "energy_kcal_mol": score,
                        "intermolecular_energy": pose.get("intermolecular_energy"),
                        "internal_energy": pose.get("internal_energy"),
                        "cluster_size": pose.get("cluster_size"),
                        "rmsd_method": "" if rmsd_detail is None else rmsd_detail[1],
                        **docking_metrics(
                            score=score,
                            ligand_source_path=ligand_source_path,
                            heavy_atoms_fallback=heavy_atoms,
                            descriptors=pair.get("ligand_descriptors"),
                        ),
                        "is_selected": rank == 1,
                        "run_id": run_id,
                        "generated_at": datetime.now().isoformat(),
                    },
                    "created_at": datetime.now(),
                }
            )
    return rows


__all__ = ["adgpu_command", "adgpu_dock_runner", "adgpu_recipe"]
