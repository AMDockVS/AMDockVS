"""gnina docking engine: CNN-scored docking on the DOCK_RUNNERS seam.

gnina reads the SAME prepared PDBQT as Vina (preparation_engine="ad4") and writes
poses straight to SDF (no meeko conversion needed). It runs the Vina Monte-Carlo
search on CPU and scores/optimises with a CNN on the GPU; how much GPU it uses is
set by --cnn_scoring:
    none        pure smina, no GPU        (gpu_required=0, add --no_gpu)
    rescore     CNN rescores final poses  (gpu_required=0; GPU used in short bursts)
    refinement  CNN refines poses         (GPU-bound)
    all         CNN on every MC step       (GPU-bound; saturates the card)
The gnina "cnn mode" rides on the chunk's `scoring_function` field (gnina is its own
engine, so that slot is free) — no new param threading. The GPU token cost per mode
is declared at submit time in docking/api.py.

score = minimizedAffinity (kcal/mol, same sign/units as Vina) so existing
sorting/stats/LE keep working; the CNN values live in `extra`.
ponytail: per-pair gnina calls (reloads receptor+CNN each pair). Batch same-receptor
ligands via a multi-model SDF input if the reload time ever dominates.
"""
from __future__ import annotations

import glob
import os
from contextlib import suppress
from pathlib import Path

from ms_env import READY, Recipe, locate

from amdockvs.docking.results.metrics import docking_row, pose_file_stem
from amdockvs.docking.results.rmsd import pose_rmsd_detail
from amdockvs.core.paths import get_default_project_root, tools_home
from amdockvs.core.worker_io import run_program
from amdockvs.integrations.envs import tool_spec

CNN_MODES = ("rescore", "none", "refinement", "all")
GPU_BOUND_CNN_MODES = frozenset({"refinement", "all"})


def normalize_cnn_mode(value: object) -> str:
    """gnina's cnn mode rides on `scoring_function`, whose default is "vina" (a Vina scoring
    name that means nothing to gnina) — fall back to rescore for anything unrecognised."""
    mode = str(value or "").strip().lower()
    return mode if mode in CNN_MODES else "rescore"


def chunk_gpu_tokens(engine: str, scoring_function: str) -> dict:
    """Per-chunk resource keys for the feed. gnina refinement/all are GPU-bound → declare a
    GPU token so MF's two-axis scheduler admits at most `total_gpu` at once (feeding.py pops
    `_gpu_required`). CPU-bound engines/modes add nothing (job-level cpu_required covers CPU)."""
    if str(engine).strip().lower() == "gnina" and normalize_cnn_mode(scoring_function) in GPU_BOUND_CNN_MODES:
        return {"_gpu_required": 1}
    return {}


def gnina_recipe(runtime=None) -> Recipe:
    """Where gnina is looked up: $AMDOCK_GNINA, Settings > Docking > gnina, the managed install,
    then PATH."""
    from amdockvs.core.configuration import app_config

    registered = [os.environ.get("AMDOCK_GNINA"), app_config(runtime).docking.gnina.path]
    return tool_spec("gnina").recipe(
        str(tools_home(runtime)),
        extra_paths=[str(Path(path).expanduser().resolve()) for path in registered if str(path or "").strip()],
    )


def gnina_binary(runtime=None) -> str:
    """The usable executable. Resolved per run, in the worker, so it reads the global config
    layer (not a project one). Runs the recipe check in a subprocess: keep it off the GUI thread."""
    state, executable = locate(gnina_recipe(runtime))
    if state != READY:
        raise RuntimeError(
            "gnina is not installed. Install it from Settings > External tools, or set its path "
            "in Settings > Docking > gnina or $AMDOCK_GNINA."
        )
    return executable


def _gnina_env(binary: str) -> dict:
    """The prebuilt gnina binary is not truly static: it needs libcudnn.so.9 + the CUDA-12
    runtime libs shipped as nvidia-*-cu12 pip wheels. Prepend those wheel lib dirs (found
    next to the binary's env) to LD_LIBRARY_PATH so the child process can load them."""
    env = dict(os.environ)
    binary = Path(binary)
    # <env>/bin/gnina -> <env>; nvidia wheels live under <env>/lib/python*/site-packages/nvidia/*/lib
    env_root = binary.resolve().parent.parent if binary.exists() else None
    lib_dirs: list[str] = []
    if env_root is not None:
        lib_dirs.extend(sorted(glob.glob(str(env_root / "lib" / "python*" / "site-packages" / "nvidia" / "*" / "lib"))))
        lib_dirs.append(str(env_root / "lib"))
    if lib_dirs:
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(lib_dirs + ([existing] if existing else []))
    return env


def _parse_gnina_sdf(sdf_path: Path) -> list[dict]:
    """One dict per pose with the gnina score tags. RDKit reads the multi-model SDF; sanitize
    is off because poses come from PDBQT typing that RDKit may reject."""
    from rdkit import Chem

    poses: list[dict] = []
    supplier = Chem.SDMolSupplier(str(sdf_path), sanitize=False, removeHs=False)
    for mol in supplier:
        if mol is None:
            continue
        props = mol.GetPropsAsDict()

        def _f(key: str) -> float | None:
            with suppress(Exception):
                return float(props[key])
            return None

        poses.append(
            {
                "minimizedAffinity": _f("minimizedAffinity"),
                "CNNscore": _f("CNNscore"),
                "CNNaffinity": _f("CNNaffinity"),
                "CNN_VS": _f("CNN_VS"),
            }
        )
    return poses


def _run_gnina(
    *,
    receptor_path: Path,
    ligand_path: Path,
    output_path: Path,
    box_center: list[float],
    box_size: list[float],
    cnn_scoring: str,
    exhaustiveness: int,
    num_modes: int,
    cpu: int,
    seed: int,
    device: int = 0,
) -> None:
    binary = gnina_binary()
    mode = str(cnn_scoring or "rescore").strip().lower() or "rescore"
    command = [
        binary,
        "-r", str(receptor_path),
        "-l", str(ligand_path),
        "--center_x", str(float(box_center[0])),
        "--center_y", str(float(box_center[1])),
        "--center_z", str(float(box_center[2])),
        "--size_x", str(float(box_size[0])),
        "--size_y", str(float(box_size[1])),
        "--size_z", str(float(box_size[2])),
        "--exhaustiveness", str(int(exhaustiveness)),
        "--num_modes", str(int(num_modes)),
        "--cpu", str(max(1, int(cpu))),
        "--seed", str(int(seed)),
        "-o", str(output_path),
    ]
    if mode == "none":
        command += ["--cnn_scoring", "none", "--no_gpu"]
    else:
        command += ["--cnn_scoring", mode, "--device", str(int(device))]
    result = run_program(command, env=_gnina_env(binary))
    if result.returncode != 0:
        raise RuntimeError(
            f"gnina failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
        )


def run_gnina_docking_rows(payload: dict) -> list[dict]:
    from amdockvs.docking.methods import check_program, check_receptor
    from amdockvs.docking.preparation.meeko import dockable_ligand

    method = check_program(payload.get("method"), "gnina")
    pairs = list(payload.get("pairs") or [])
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    default_center = [float(v) for v in (payload.get("box_center") or [])]
    default_size = [float(v) for v in (payload.get("box_size") or [])]
    cnn_scoring = normalize_cnn_mode(payload.get("scoring_function"))
    exhaustiveness = int(payload.get("exhaustiveness") or 8)
    cpu = int(payload.get("vina_cpu") or 1)
    seed = int(payload.get("seed") or 0)
    protocol_payload = dict(payload.get("protocol_metadata") or {})
    project_root = get_default_project_root()

    rows: list[dict] = []
    for pair in pairs:
        run_kind = str(pair.get("run_kind") or "screening")
        ligand_path = Path(str(pair.get("ligand_path") or "")).expanduser().resolve()
        receptor_path = Path(str(pair.get("receptor_path") or "")).expanduser().resolve()
        num_modes = int(pair.get("num_modes") or 9)
        center = [float(v) for v in (pair.get("box_center") or default_center)]
        size = [float(v) for v in (pair.get("box_size") or default_size)]
        invalid_reason = str(pair.get("invalid_reason") or "").strip()
        output_stem = pose_file_stem(pair, protocol_payload)
        output_sdf = output_dir / f"{output_stem}.gnina.sdf"
        try:
            if invalid_reason:
                raise ValueError(invalid_reason)
            if not ligand_path.exists():
                raise FileNotFoundError(f"ligand file missing: {ligand_path}")
            if not receptor_path.exists():
                raise FileNotFoundError(f"receptor file missing: {receptor_path}")
            if len(center) != 3 or len(size) != 3:
                raise ValueError("gnina requires a box (center_xyz + size_xyz).")
            check_receptor(method, receptor_path, center, size)
            _run_gnina(
                receptor_path=receptor_path,
                ligand_path=dockable_ligand(ligand_path, program="gnina", scoring_function=None,
                                            output_dir=output_dir),
                output_path=output_sdf,
                box_center=center,
                box_size=size,
                cnn_scoring=cnn_scoring,
                exhaustiveness=exhaustiveness,
                num_modes=num_modes,
                cpu=cpu,
                seed=seed,
            )
            poses = _parse_gnina_sdf(output_sdf)
            if not poses:
                raise RuntimeError(f"gnina produced no parseable poses in {output_sdf}")
        except Exception as exc:
            rows.append(docking_row(pair, score_type="gnina_score", error=str(exc)))
            continue

        pose_text = str(output_sdf)
        if project_root is not None:
            with suppress(Exception):
                pose_text = str(output_sdf.relative_to(project_root))
        heavy_atoms = _heavy_atoms(ligand_path)
        for rank, pose in enumerate(poses, start=1):
            score = pose.get("minimizedAffinity")
            if score is None:
                continue
            rows.append(docking_row(
                pair,
                score_type="gnina_score",
                pose_rank=rank,
                score=float(score),
                pose_path=pose_text,
                rmsd=pose_rmsd_detail(
                    reference_ligand_path=pair.get("reference_ligand_path"),
                    pose_path=output_sdf,
                    pose_rank=rank,
                ) if run_kind == "redocking" else None,
                extra={key: pose.get(key) for key in ("CNNscore", "CNNaffinity", "CNN_VS")},
                heavy_atoms=heavy_atoms,
            ))
    return rows


def _resolve_optional(value: object, project_root: Path | None):
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    with suppress(Exception):
        return path.resolve()
    return path


def _heavy_atoms(path: Path) -> int:
    from amdockvs.docking.engines.vina import count_heavy_atoms

    with suppress(Exception):
        return count_heavy_atoms(path)
    return 0


def gnina_dock_runner(payload: dict) -> list[dict]:
    return run_gnina_docking_rows(payload)
