"""AutoDock-GPU docking engine: the AutoDock4 force field and genetic search on the GPU.

Reads the same prepared PDBQT as Vina (preparation_engine="ad4"). The receptor reaches it
only as autogrid4 maps, which is where a custom parameter file acts. An atom type the
program does not know (one added by that file) is declared as derived from a known one
(`derived_types`, e.g. "XX=OA"): it takes its receptor terms from its own map and its
internal ligand terms from the base type.

One process saturates the GPU (measured: 1, 2, 4 and 8 concurrent processes all dock the
same ligands per second), so each chunk takes one GPU token and docks its pairs in turn —
all the pairs that share maps in one `--filelist` process: start-up and map loading are ~0.6 s
of a ~1.8 s dock (1iep, nrun 20, GTX 1660), so 8 ligands take 10 s instead of 14.6 s.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from pathlib import Path

from ms_env import READY, Recipe, locate

from amdockvs.core.configuration import app_config
from amdockvs.core.paths import get_default_project_root, tools_home
from amdockvs.core.worker_io import run_program
from amdockvs.docking.engines.autogrid import map_prefix
from amdockvs.docking.engines.vina import count_heavy_atoms
from amdockvs.docking.methods import check_program, check_receptor
from amdockvs.docking.preparation.meeko import dockable_ligand
from amdockvs.docking.results.metrics import docking_row, pose_file_stem
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
    ligands: dict[str, Path],
    flex_receptor_path: Path | None,
    output_dir: Path,
    nrun: int,
    seed: int,
    derived_types: str,
) -> dict[str, Path | str]:
    """Dock `ligands` (output name → PDBQT) over one set of maps in one process. Each name maps
    to its DLG, or to why it failed: a bad ligand fails its own job, the others still run."""
    batch = output_dir / f"{next(iter(ligands))}.lst"
    batch.write_text(f"{maps_prefix}.maps.fld\n" + "".join(f"{path}\n{name}\n" for name, path in ligands.items()))
    command = [executable, "--filelist", str(batch), "--nrun", str(int(nrun)), "--xmloutput", "0"]
    if flex_receptor_path is not None:
        command += ["--flexres", str(flex_receptor_path)]
    if int(seed):
        command += ["--seed", str(int(seed))]  # 0 keeps the program's own time-based seed
    if derived_types.strip():
        command += ["--derivtype", derived_types.strip()]
    try:
        result = run_program(command, cwd=str(output_dir))
    finally:
        batch.unlink(missing_ok=True)
    # The exit code is 0 even when jobs failed: the text is the only signal. A failed job
    # prints its "Error: ..." lines, then "Error in setup of Job #<1-based position>".
    output = f"{result.stdout}\n{result.stderr}"
    reasons: dict[int, str] = {}
    pending: list[str] = []
    for line in (line.strip() for line in output.splitlines()):
        job = re.match(r"Error in setup of Job #(\d+)", line)
        if job:
            reasons[int(job.group(1))] = " ".join(pending)
            pending = []
        elif line.startswith("Error") and not line.startswith("Error in "):
            pending.append(line)
    fallback = " ".join(pending) or output.strip()[-500:] or "no output"
    outcome: dict[str, Path | str] = {}
    for position, name in enumerate(ligands, start=1):
        dlg = output_dir / f"{name}.dlg"
        outcome[name] = dlg if dlg.exists() and position not in reasons else (
            f"AutoDock-GPU failed: {reasons.get(position) or fallback}")
    return outcome


def _cluster_leads(dlg_path: Path, sdf_path: Path, num_modes: int) -> list[dict]:
    """Write the best pose of each cluster (lowest energy first) as SDF and return their
    energies, in the same order."""
    from meeko import PDBQTMolecule, RDKitMolCreate

    # skip_typing: Meeko's type table has no entry for types added by a parameter file; the
    # chemistry comes from the SMILES remark, not from the AutoDock types.
    molecule = PDBQTMolecule(
        dlg_path.read_text(encoding="utf-8", errors="replace"), name=dlg_path.stem, is_dlg=True, skip_typing=True
    )
    from amdockvs.docking.preparation.meeko import boron_in_sd

    sd_string, _failures = RDKitMolCreate.write_sd_string(molecule, only_cluster_leads=True, keep_flexres=False)
    sd_string = boron_in_sd(str(sd_string or ""))
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
    protocol_payload = dict(payload.get("protocol_metadata") or {})
    project_root = get_default_project_root()
    map_cache: dict[tuple, str] = {}
    method = check_program(payload.get("method"), "adgpu")

    parameter_file = _resolve_optional(payload.get("parameter_file"), project_root)

    rows: list[dict] = []
    sites: dict[tuple, list[dict]] = {}  # (receptor, box, spacing, flexres) → its prepared pairs
    for pair in pairs:
        ligand_path = Path(str(pair.get("ligand_path") or "")).expanduser().resolve()
        receptor_path = Path(str(pair.get("receptor_path") or "")).expanduser().resolve()
        center = [float(v) for v in (pair.get("box_center") or default_center)]
        size = [float(v) for v in (pair.get("box_size") or default_size)]
        spacing = float(pair.get("spacing") or DEFAULT_SPACING)
        try:
            invalid_reason = str(pair.get("invalid_reason") or "").strip()
            if invalid_reason:
                raise ValueError(invalid_reason)
            for label, path in (("ligand", ligand_path), ("receptor", receptor_path)):
                if not path.exists():
                    raise FileNotFoundError(f"{label} file missing: {path}")
            if len(center) != 3 or len(size) != 3:
                raise ValueError("AutoDock-GPU requires a box (center_xyz + size_xyz).")
            check_receptor(method, receptor_path, center, size)
            ligand_path = dockable_ligand(ligand_path, program="adgpu", scoring_function=None,
                                          output_dir=output_dir)
            if parameter_file is not None and not parameter_file.is_file():
                raise FileNotFoundError(f"parameter file missing: {parameter_file}")
            # Same convention as the Vina runner: flexible-residue prep leaves "<receptor>__flex.pdbqt".
            explicit_flex = str(pair.get("flex_receptor_path") or "").strip()
            flex_path = Path(explicit_flex) if explicit_flex else receptor_path.with_name(f"{receptor_path.stem}__flex.pdbqt")
            flex_path = flex_path if flex_path.exists() else None
        except Exception as exc:
            rows.append(docking_row(pair, score_type="ad4_score", error=str(exc)))
            continue
        site = (receptor_path, tuple(center), tuple(size), spacing, flex_path)
        sites.setdefault(site, []).append(
            {"pair": pair, "ligand_path": ligand_path, "name": f"{pose_file_stem(pair, protocol_payload)}.adgpu"})

    def maps(site, ligands) -> str:
        receptor_path, center, size, spacing, flex_path = site
        return map_prefix(cache=map_cache, maps_dir=output_dir / "_ad4_maps", receptor_path=receptor_path,
                          ligand_path=ligands, box_center=list(center), box_size=list(size), spacing=spacing,
                          flex_receptor_path=flex_path, parameter_file=parameter_file, zinc=method.zinc_site)

    # One map set per site covering every ligand's types, so the whole site is one process.
    batches: dict[tuple, list[dict]] = {}  # (maps prefix, flexres) → the pairs one process docks
    for site, group in sites.items():
        try:
            batches[(maps(site, [item["ligand_path"] for item in group]), site[-1])] = group
            continue
        except Exception:
            pass  # a type autogrid cannot map fails the shared maps: each ligand gets its own instead
        for item in group:
            try:
                batches.setdefault((maps(site, item["ligand_path"]), site[-1]), []).append(item)
            except Exception as exc:
                rows.append(docking_row(item["pair"], score_type="ad4_score", error=str(exc)))

    executable = ""
    for (prefix, flex_path), group in batches.items():
        try:
            executable = executable or adgpu_command()
            outcome = _run_adgpu(
                executable=executable,
                maps_prefix=prefix,
                ligands={item["name"]: item["ligand_path"] for item in group},
                flex_receptor_path=flex_path,
                output_dir=output_dir,
                nrun=nrun,
                seed=seed,
                derived_types=derived_types,
            )
        except Exception as exc:  # no program, or it did not run: every pair of the batch fails
            outcome = {item["name"]: str(exc) for item in group}
        for item in group:
            rows.extend(_pair_rows(item, outcome[item["name"]], output_dir, num_modes, project_root))
    return rows


def _pair_rows(item: dict, dlg: Path | str, output_dir: Path, num_modes: int, project_root: Path | None) -> list[dict]:
    pair = item["pair"]
    output_sdf = output_dir / f"{item['name']}.sdf"
    try:
        if not isinstance(dlg, Path):
            raise RuntimeError(dlg)
        poses = _cluster_leads(dlg, output_sdf, num_modes)
        dlg.unlink()  # every pose kept is in the SDF; the DLG repeats all runs
    except Exception as exc:
        return [docking_row(pair, score_type="ad4_score", error=str(exc))]

    pose_text = str(output_sdf)
    if project_root is not None:
        with suppress(Exception):
            pose_text = str(output_sdf.relative_to(project_root))
    heavy_atoms = count_heavy_atoms(item["ligand_path"])
    redocking = str(pair.get("run_kind") or "screening") == "redocking"
    return [
        docking_row(
            pair,
            score_type="ad4_score",
            pose_rank=rank,
            score=float(pose["free_energy"]),
            pose_path=pose_text,
            rmsd=pose_rmsd_detail(
                reference_ligand_path=pair.get("reference_ligand_path"),
                pose_path=output_sdf,
                pose_rank=rank,
            ) if redocking else None,
            extra={key: pose.get(key) for key in ("intermolecular_energy", "internal_energy", "cluster_size")},
            heavy_atoms=heavy_atoms,
        )
        for rank, pose in enumerate(poses, start=1)
    ]


__all__ = ["adgpu_command", "adgpu_dock_runner", "adgpu_recipe"]
