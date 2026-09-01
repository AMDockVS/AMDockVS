from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from amdockvs.constants import DEFAULT_VINA_BACKEND, DEFAULT_VINA_COMMAND
from amdockvs.api_common import project_root_from_output_dir
from amdockvs.docking.metrics import docking_metrics
from amdockvs.docking.rmsd import pose_rmsd_detail
from amdockvs.molecule_paths import get_default_project_root, set_default_project_root

def _require_pdbqt(path: Path, *, kind: str) -> None:
    if path.suffix.lower() != ".pdbqt":
        raise ValueError(
            f"AutoDock Vina requires {kind} in PDBQT format, got: {path.name}. "
            "Prepare inputs first, e.g. with Meeko."
        )


def _ad4_map_prefix(
    *,
    cache: dict[tuple, str],
    maps_dir: Path,
    receptor_path: Path,
    ligand_path: Path,
    box_center: list[float],
    box_size: list[float],
    spacing: float,
    flex_receptor_path: Path | None = None,
) -> str:
    """Autogrid4 maps for ad4 scoring, returning the load_maps() prefix. Reuses autodock4._autogrid;
    cached per (receptor, box, ligand atom types) so identical-typed ligands share maps.
    ponytail: autogrid runs once per distinct ligand-type set; union-per-receptor if it's too slow.
    """
    import hashlib

    from amdockvs.docking import autodock4

    if flex_receptor_path is not None:
        raise RuntimeError(
            "ad4 scoring with flexible residues is not supported — use a rigid receptor, "
            "or pick vina/vinardo."
        )
    ligand_types = tuple(sorted(set(autodock4._atom_types(ligand_path))))
    key = (
        str(receptor_path),
        tuple(round(float(v), 3) for v in box_center),
        tuple(round(float(v), 3) for v in box_size),
        round(float(spacing), 4),
        ligand_types,
    )
    cached = cache.get(key)
    if cached is not None:
        return cached
    binary = autodock4.AUTOGRID4
    if not (Path(binary).exists() or shutil.which(str(binary))):
        raise RuntimeError(
            f"ad4 scoring needs autogrid4 but it was not found ({binary!r}). "
            "Set $AMDOCK_AUTOGRID4 or install AutoDock4."
        )
    work = Path(maps_dir) / f"{receptor_path.stem}_{hashlib.md5(repr(key).encode()).hexdigest()[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    autodock4._autogrid(receptor_path, list(ligand_types), list(box_center), list(box_size), float(spacing), work)
    prefix = str(work / receptor_path.stem)
    cache[key] = prefix
    return prefix


def _prepared_vina(
    *,
    cache: dict[tuple, object],
    receptor_path: Path,
    box_center: list[float],
    box_size: list[float],
    spacing: float,
    scoring_function: str,
    cpu: int,
    seed: int,
    flex_receptor_path: Path | None = None,
    ad4_map_prefix: str | None = None,
):
    cache_key = (
        str(receptor_path),
        str(flex_receptor_path or ""),
        tuple(float(value) for value in box_center),
        tuple(float(value) for value in box_size),
        float(spacing),
        str(scoring_function),
        str(ad4_map_prefix or ""),
        int(cpu),
        int(seed),
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        from vina import Vina
    except ImportError as exc:
        raise RuntimeError(
            "Python package 'vina' is not available in the current environment."
        ) from exc

    vina_obj = Vina(sf_name=str(scoring_function), cpu=int(cpu), seed=int(seed), verbosity=0)
    if ad4_map_prefix:
        # ad4: the rigid receptor is baked into the autogrid maps -> load them instead of
        # set_receptor + compute_vina_maps (which can't produce ad4 maps).
        vina_obj.load_maps(str(ad4_map_prefix))
    else:
        if flex_receptor_path is not None:
            vina_obj.set_receptor(str(receptor_path), str(flex_receptor_path))
        else:
            vina_obj.set_receptor(str(receptor_path))
        vina_obj.compute_vina_maps(
            center=[float(value) for value in box_center],
            box_size=[float(value) for value in box_size],
            spacing=float(spacing),
        )
    cache[cache_key] = vina_obj
    return vina_obj


def _parse_vina_pose_energies(*, output_path: Path, stdout_text: str = "") -> list[list[float]]:
    def _collect(raw_text: str) -> list[list[float]]:
        rows: list[list[float]] = []
        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            if not line.startswith("REMARK VINA RESULT:"):
                continue
            parts = line.split(":", 1)[-1].split()
            numeric: list[float] = []
            for token in parts[:3]:
                try:
                    numeric.append(float(token))
                except Exception:
                    break
            if numeric:
                rows.append(numeric)
        return rows

    if output_path.exists():
        parsed = _collect(output_path.read_text(encoding="utf-8", errors="replace"))
        if parsed:
            return parsed
    if stdout_text:
        parsed = _collect(stdout_text)
        if parsed:
            return parsed
    return []


def _count_heavy_atoms_pdbqt(path: Path) -> int:
    # Heavy-atom count from the prepared ligand PDBQT: count ATOM/HETATM records whose
    # AutoDock atom type (last token) is not hydrogen. Cheap, no RDKit parse needed.
    # ponytail: AD types for H are H/HD/HS — all start with "H"; no heavy type does.
    count = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not (raw.startswith("ATOM") or raw.startswith("HETATM")):
            continue
        ad_type = raw.split()[-1] if raw.split() else ""
        if ad_type and not ad_type.upper().startswith("H"):
            count += 1
    return count


def _ligand_efficiency(score: float, heavy_atoms: int) -> float | None:
    return round(float(score) / heavy_atoms, 4) if heavy_atoms > 0 else None


def _resolve_optional_path(value: object, project_root: Path | None = None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    return path.resolve()


def count_heavy_atoms(path: Path) -> int:
    """Heavy-atom count from any ligand file. PDBQT via AD types (no RDKit needed),
    everything else (SDF/MOL/PDB) via RDKit. 0 if unreadable. Used to backfill LE for
    results docked before LE was stored in metrics."""
    suffix = path.suffix.lower()
    if suffix == ".pdbqt":
        return _count_heavy_atoms_pdbqt(path)
    try:
        from rdkit import Chem
    except ImportError:
        return 0
    if suffix in {".sdf", ".mol"}:
        mol = next(iter(Chem.SDMolSupplier(str(path), sanitize=False, removeHs=True)), None)
    elif suffix in {".pdb", ".ent"}:
        mol = Chem.MolFromPDBFile(str(path), sanitize=False, removeHs=True)
    elif suffix == ".mol2":
        mol = Chem.MolFromMol2File(str(path), sanitize=False, removeHs=True)
    else:
        return 0
    return mol.GetNumHeavyAtoms() if mol is not None else 0


def _convert_vina_pdbqt_to_sdf(
    *,
    pdbqt_path: Path,
    sdf_path: Path,
    only_cluster_leads: bool = False,
) -> Path:
    try:
        from meeko import PDBQTMolecule, RDKitMolCreate
    except ImportError as exc:
        raise RuntimeError("Python package 'meeko' is not available in the current environment.") from exc

    pdbqt_molecule = PDBQTMolecule(
        pdbqt_path.read_text(encoding="utf-8", errors="replace"),
        name=pdbqt_path.stem,
        poses_to_read=None,
        energy_range=None,
        is_dlg=False,
        skip_typing=False,
    )
    # write_sd_string returns (sd_string, failures); writing the tuple repr (str(...)) is what
    # corrupted the SDF ("('...\\n...', [])") and made PyMOL fail with "bad atom count".
    sd_string, _failures = RDKitMolCreate.write_sd_string(
        pdbqt_molecule,
        only_cluster_leads=bool(only_cluster_leads),
        keep_flexres=False,
    )
    if not str(sd_string or "").strip():
        raise RuntimeError(f"Meeko could not convert docked PDBQT to SDF: {pdbqt_path.name}")
    sdf_path.parent.mkdir(parents=True, exist_ok=True)
    sdf_path.write_text(str(sd_string), encoding="utf-8")
    return sdf_path


def _run_vina_binary(
    *,
    vina_command: str,
    receptor_path: Path,
    ligand_path: Path,
    output_path: Path,
    flex_receptor_path: Path | None = None,
    box_center: list[float],
    box_size: list[float],
    spacing: float,
    scoring_function: str,
    cpu: int,
    seed: int,
    exhaustiveness: int,
    num_modes: int,
    min_rmsd: float,
    energy_range: float,
    ad4_map_prefix: str | None = None,
) -> tuple[list[list[float]], str]:
    resolved_command = shutil.which(str(vina_command)) if not Path(str(vina_command)).expanduser().exists() else str(
        Path(str(vina_command)).expanduser().resolve()
    )
    if not resolved_command:
        raise RuntimeError(
            f"vina binary is not available. Command not found: {vina_command!r}. "
            "Install AutoDock Vina or pass vina_command explicitly."
        )
    if ad4_map_prefix:
        # ad4: the autogrid maps carry the rigid receptor and the box, so --maps replaces
        # --receptor, the box (--center_*/--size_*) and --spacing (docking_basic.html, ad4 section).
        grid_args = ["--maps", str(ad4_map_prefix)]
    else:
        grid_args = [
            "--receptor", str(receptor_path),
            *(["--flex", str(flex_receptor_path)] if flex_receptor_path is not None else []),
            "--center_x", str(float(box_center[0])),
            "--center_y", str(float(box_center[1])),
            "--center_z", str(float(box_center[2])),
            "--size_x", str(float(box_size[0])),
            "--size_y", str(float(box_size[1])),
            "--size_z", str(float(box_size[2])),
            "--spacing", str(float(spacing)),
        ]
    command = [
        str(resolved_command),
        "--ligand", str(ligand_path),
        "--scoring", str(scoring_function),
        *grid_args,
        "--cpu", str(int(cpu)),
        "--seed", str(int(seed)),
        "--exhaustiveness", str(int(exhaustiveness)),
        "--num_modes", str(int(num_modes)),
        "--min_rmsd", str(float(min_rmsd)),
        "--energy_range", str(float(energy_range)),
        "--out", str(output_path),
        "--verbosity", "0",
    ]
    process: subprocess.Popen[str] | None = None
    previous_handlers: dict[int, object] = {}

    def _kill_child_process() -> None:
        nonlocal process
        if process is None or process.poll() is not None:
            return
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        else:
            with suppress(Exception):
                process.terminate()
        with suppress(Exception):
            process.wait(timeout=2.0)
        if process.poll() is None:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:
                with suppress(Exception):
                    process.kill()
            with suppress(Exception):
                process.wait(timeout=1.0)

    def _forward_shutdown(signum, _frame) -> None:
        _kill_child_process()
        raise SystemExit(128 + int(signum))

    # signal.signal() only works in the main thread; under the `thread` executor run_chunk
    # runs in a worker thread, so guard on that (otherwise every dock raises "signal only
    # works in main thread"). start_new_session already isolates the child for cleanup.
    install_signal_handlers = os.name == "posix" and threading.current_thread() is threading.main_thread()
    previous_handlers: dict = {}
    if install_signal_handlers:
        previous_handlers = {
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
            signal.SIGINT: signal.getsignal(signal.SIGINT),
        }
        signal.signal(signal.SIGTERM, _forward_shutdown)
        signal.signal(signal.SIGINT, _forward_shutdown)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(os.name == "posix"),
        )
        stdout_text, stderr_text = process.communicate()
    finally:
        if install_signal_handlers:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
    stdout_text = str(stdout_text or "")
    stderr_text = str(stderr_text or "")
    if process.returncode != 0:
        raise RuntimeError(
            f"vina binary failed with exit code {process.returncode}: "
            f"{stderr_text.strip() or stdout_text.strip() or 'unknown error'}"
        )
    energies = _parse_vina_pose_energies(output_path=output_path, stdout_text=stdout_text)
    if not energies:
        raise RuntimeError(
            "vina binary did not produce parseable pose energies. "
            f"output_file={output_path} stdout={stdout_text.strip()!r} stderr={stderr_text.strip()!r}"
        )
    return energies, stdout_text


def run_vina_docking_rows(
    *,
    pairs: list[dict],
    output_dir: str | Path,
    box_center: list[float],
    box_size: list[float],
    scoring_function: str = "vina",
    vina_cpu: int = 1,
    seed: int = 0,
    spacing: float = 0.375,
    energy_range: float = 3.0,
    min_rmsd: float = 1.0,
    vina_backend: str = DEFAULT_VINA_BACKEND,
    vina_command: str = DEFAULT_VINA_COMMAND,
    run_id: str = "",
    protocol_metadata: dict | None = None,
    report_name: str | None = None,
) -> list[dict]:
    resolved_output_dir = Path(output_dir).expanduser().resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    normalized_backend = str(vina_backend or "python").strip().lower() or "python"
    if normalized_backend not in {"python", "binary"}:
        raise ValueError(f"Unsupported vina backend: {vina_backend}")

    rows: list[dict] = []
    failures: list[dict] = []
    project_root = get_default_project_root()
    vina_cache: dict[tuple, object] = {}
    ad4_map_cache: dict[tuple, str] = {}
    ad4_maps_dir = resolved_output_dir / "_ad4_maps"
    protocol_payload = dict(protocol_metadata or {})
    protocol_hash = str(protocol_payload.get("hash") or "").strip()
    protocol_suffix = f"__proto_{protocol_hash[:12]}" if protocol_hash else ""
    for pair in pairs:
        complex_id = int(pair.get("complex_id") or 0) or None
        run_kind = str(pair.get("run_kind") or "screening")
        ligand_id = int(pair.get("ligand_id") or 0)
        receptor_id = int(pair.get("receptor_id") or 0)
        invalid_reason = str(pair.get("invalid_reason") or "").strip()
        ligand_path = Path(str(pair.get("ligand_path") or "")).expanduser().resolve()
        receptor_path = Path(str(pair.get("receptor_path") or "")).expanduser().resolve()
        ligand_path_logical = str(pair.get("ligand_path_logical") or ligand_path)
        receptor_path_logical = str(pair.get("receptor_path_logical") or receptor_path)
        reference_ligand_logical = str(
            pair.get("reference_ligand_path_logical") or pair.get("reference_ligand_path") or ""
        )
        reference_receptor_logical = str(
            pair.get("reference_receptor_path_logical") or pair.get("reference_receptor_path") or ""
        )
        pair_box_center = [float(value) for value in (pair.get("box_center") or box_center or [])]
        pair_box_size = [float(value) for value in (pair.get("box_size") or box_size or [])]
        pair_spacing = float(pair.get("spacing") or spacing)
        output_stem = f"{run_kind}_{int(complex_id)}" if complex_id is not None else f"{ligand_id}__{receptor_id}"
        output_stem = f"{output_stem}{protocol_suffix}"
        output_pdbqt_path = resolved_output_dir / f"{output_stem}.dock.pdbqt"
        output_sdf_path = resolved_output_dir / f"{output_stem}.dock.sdf"
        grid_payload = {
            "box_center": list(pair_box_center),
            "box_size": list(pair_box_size),
            "spacing": float(pair_spacing),
            "scoring_function": str(scoring_function),
        }
        if invalid_reason:
            failures.append(
                {
                    "complex_id": complex_id,
                    "run_kind": run_kind,
                    "ligand_id": ligand_id,
                    "receptor_id": receptor_id,
                    "ligand_path": ligand_path_logical,
                    "receptor_path": receptor_path_logical,
                    "error": invalid_reason,
                }
            )
            continue
        try:
            if not ligand_path.exists():
                raise FileNotFoundError(f"ligand file missing: {ligand_path}")
            if not receptor_path.exists():
                raise FileNotFoundError(f"receptor file missing: {receptor_path}")
            _require_pdbqt(ligand_path, kind="ligand")
            _require_pdbqt(receptor_path, kind="receptor")
            # Flexible-residue prep writes "<receptor>__flex.pdbqt" next to the rigid receptor;
            # if it's there, dock with flexible side chains. Deterministic name → no extra plumbing.
            explicit_flex = str(pair.get("flex_receptor_path") or "").strip()
            flex_candidate = receptor_path.with_name(f"{receptor_path.stem}__flex.pdbqt")
            flex_receptor_path = Path(explicit_flex).expanduser().resolve() if explicit_flex else (
                flex_candidate if flex_candidate.exists() else None
            )

            ad4_map_prefix = None
            if str(scoring_function).lower() == "ad4":
                ad4_map_prefix = _ad4_map_prefix(
                    cache=ad4_map_cache,
                    maps_dir=ad4_maps_dir,
                    receptor_path=receptor_path,
                    ligand_path=ligand_path,
                    box_center=pair_box_center,
                    box_size=pair_box_size,
                    spacing=pair_spacing,
                    flex_receptor_path=flex_receptor_path,
                )

            if normalized_backend == "python":
                vina_obj = _prepared_vina(
                    cache=vina_cache,
                    receptor_path=receptor_path,
                    flex_receptor_path=flex_receptor_path,
                    box_center=pair_box_center,
                    box_size=pair_box_size,
                    spacing=pair_spacing,
                    scoring_function=scoring_function,
                    cpu=vina_cpu,
                    seed=seed,
                    ad4_map_prefix=ad4_map_prefix,
                )
                vina_obj.set_ligand_from_file(str(ligand_path))
                vina_obj.dock(
                    exhaustiveness=int(pair.get("exhaustiveness") or 8),
                    n_poses=int(pair.get("num_modes") or 9),
                    min_rmsd=float(min_rmsd),
                )
                vina_obj.write_poses(
                    str(output_pdbqt_path),
                    n_poses=int(pair.get("num_modes") or 9),
                    energy_range=float(energy_range),
                    overwrite=True,
                )
                energies = vina_obj.energies(
                    n_poses=int(pair.get("num_modes") or 9),
                    energy_range=float(energy_range),
                )
                energies_list = energies.tolist() if hasattr(energies, "tolist") else list(energies)
            else:
                energies_list, _ = _run_vina_binary(
                    vina_command=vina_command,
                    receptor_path=receptor_path,
                    flex_receptor_path=flex_receptor_path,
                    ligand_path=ligand_path,
                    output_path=output_pdbqt_path,
                    box_center=pair_box_center,
                    box_size=pair_box_size,
                    spacing=pair_spacing,
                    scoring_function=scoring_function,
                    ad4_map_prefix=ad4_map_prefix,
                    cpu=vina_cpu,
                    seed=seed,
                    exhaustiveness=int(pair.get("exhaustiveness") or 8),
                    num_modes=int(pair.get("num_modes") or 9),
                    min_rmsd=float(min_rmsd),
                    energy_range=float(energy_range),
                )
            _convert_vina_pdbqt_to_sdf(
                pdbqt_path=output_pdbqt_path,
                sdf_path=output_sdf_path,
                only_cluster_leads=False,
            )
            score = float(energies_list[0][0]) if energies_list else 0.0
            payload_json = {
                "engine": "vina",
                "backend": normalized_backend,
                "complex_id": complex_id,
                "run_kind": run_kind,
                "scoring_function": str(scoring_function),
                "exhaustiveness": int(pair.get("exhaustiveness") or 8),
                "num_modes": int(pair.get("num_modes") or 9),
                "ligand_id": ligand_id,
                "receptor_id": receptor_id,
                "ligand_path": ligand_path_logical,
                "ligand_source_path": str(
                    pair.get("ligand_source_path_logical") or pair.get("ligand_source_path") or ""
                ),
                "receptor_path": receptor_path_logical,
                "reference_ligand_path": reference_ligand_logical,
                "reference_receptor_path": reference_receptor_logical,
                "selected_pose_path": str(output_sdf_path),
                "selected_pose_pdbqt_path": str(output_pdbqt_path),
                "selected_affinity": float(score),
                "grid": grid_payload,
                "energies": energies_list,
                "generated_at": datetime.now().isoformat(),
                "run_id": str(run_id or ""),
                "protocol": protocol_payload,
            }
        except Exception as exc:
            failures.append(
                {
                    "complex_id": complex_id,
                    "run_kind": run_kind,
                    "ligand_id": ligand_id,
                    "receptor_id": receptor_id,
                    "ligand_path": ligand_path_logical,
                    "receptor_path": receptor_path_logical,
                    "reference_ligand_path": reference_ligand_logical,
                    "reference_receptor_path": reference_receptor_logical,
                    "error": str(exc),
                    "grid": grid_payload,
                }
            )
            continue
        pose_path_text = str(output_sdf_path)
        if project_root is not None:
            with suppress(Exception):
                pose_path_text = str(output_sdf_path.relative_to(project_root))
        heavy_atoms = 0
        with suppress(Exception):
            heavy_atoms = _count_heavy_atoms_pdbqt(ligand_path)
        ligand_source_path = _resolve_optional_path(pair.get("ligand_source_path"), project_root)
        for index, energy_row in enumerate(energies_list, start=1):
            pose_score = float(energy_row[0]) if len(energy_row) else 0.0
            rmsd_detail = pose_rmsd_detail(
                reference_ligand_path=pair.get("reference_ligand_path"),
                pose_path=output_sdf_path,
                pose_rank=index,
            ) if str(run_kind or "") == "redocking" else None
            rmsd_vs_reference = None if rmsd_detail is None else rmsd_detail[0]
            rmsd_method = "" if rmsd_detail is None else rmsd_detail[1]
            metrics = docking_metrics(
                score=pose_score,
                ligand_source_path=ligand_source_path,
                heavy_atoms_fallback=heavy_atoms,
                descriptors=pair.get("ligand_descriptors"),
            )
            rows.append(
                {
                    "receptor_molecule_id": receptor_id,
                    "ligand_molecule_id": ligand_id,
                    "engine": "vina",
                    "pose_rank": index,
                    "score": pose_score,
                    "score_type": "vina_score",
                    "pose_path": pose_path_text,
                    "rmsd_vs_reference": rmsd_vs_reference,
                    "metrics": {
                        **payload_json,
                        "pose_index": index - 1,
                        "energy": [float(value) for value in energy_row],
                        "reference_ligand_path": reference_ligand_logical,
                        "reference_receptor_path": reference_receptor_logical,
                        "rmsd_method": rmsd_method,
                        **metrics,
                        "is_selected": index == 1,
                    },
                    "created_at": datetime.now(),
                }
            )
    if failures:
        report_dir = resolved_output_dir / "_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / (report_name or f"chunk_{uuid.uuid4().hex}.json")
        report_file.write_text(
            json.dumps(
                {
                    "engine": "vina",
                    "backend": normalized_backend,
                    "generated_at": datetime.now().isoformat(),
                    "pair_count": len(pairs),
                    "success_count": len({(row["ligand_molecule_id"], row["receptor_molecule_id"]) for row in rows}),
                    "failure_count": len(failures),
                    "run_id": str(run_id or ""),
                    "failures": failures,
                },
                indent=2,
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
    # Surface failed pairs as result rows (score=None, metrics.status="failed") so a genuine
    # docking failure doesn't silently vanish from stats/list_results. The report file above
    # is just extra diagnostics.
    for failure in failures:
        rows.append(
            {
                "receptor_molecule_id": int(failure.get("receptor_id") or 0),
                "ligand_molecule_id": int(failure.get("ligand_id") or 0),
                "engine": "vina",
                "pose_rank": 1,
                "score": None,
                "score_type": "vina_score",
                "pose_path": "",
                "rmsd_vs_reference": None,
                "metrics": {
                    "status": "failed",
                    "error": str(failure.get("error") or ""),
                    "run_kind": str(failure.get("run_kind") or "screening"),
                    "complex_id": failure.get("complex_id"),
                    "ligand_path": str(failure.get("ligand_path") or ""),
                    "receptor_path": str(failure.get("receptor_path") or ""),
                    "reference_ligand_path": str(failure.get("reference_ligand_path") or ""),
                    "reference_receptor_path": str(failure.get("reference_receptor_path") or ""),
                    "protocol": protocol_payload,
                },
                "created_at": datetime.now(),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Engine runner registry
#
# A docking chunk carries an "engine" key; the job dispatches to the registered
# runner for that engine. Each runner takes the chunk payload dict and returns
# the docking result rows. This is the seam for adding new docking programs
# (e.g. AutoDock4) without touching the job/sink plumbing.
# ---------------------------------------------------------------------------

def _vina_dock_runner(payload: dict) -> list[dict]:
    return run_vina_docking_rows(
        pairs=list(payload.get("pairs") or []),
        output_dir=str(payload.get("output_dir") or ""),
        box_center=[float(value) for value in (payload.get("box_center") or [])],
        box_size=[float(value) for value in (payload.get("box_size") or [])],
        scoring_function=str(payload.get("scoring_function") or "vina"),
        vina_backend=str(payload.get("vina_backend") or "python"),
        vina_command=str(payload.get("vina_command") or "vina"),
        vina_cpu=int(payload.get("vina_cpu") or 1),
        seed=int(payload.get("seed") or 0),
        spacing=float(payload.get("spacing") or 0.375),
        energy_range=float(payload.get("energy_range") or 3.0),
        min_rmsd=float(payload.get("min_rmsd") or 1.0),
        run_id=str(payload.get("run_id") or ""),
        protocol_metadata=dict(payload.get("protocol_metadata") or {}),
        report_name=str(payload.get("report_name") or "").strip() or None,
    )


DOCK_RUNNERS: dict = {
    "vina": _vina_dock_runner,
}


def register_dock_runner(engine: str, runner) -> None:
    DOCK_RUNNERS[str(engine).strip().lower()] = runner


def run_docking_chunk(payload: dict) -> list[dict]:
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
        # Same-process 2D diagrams, opt-in. Isolated per pose in a subprocess, so a
        # diagram failure never touches the docking rows we just produced.
        from amdockvs.docking.diagram import render_diagrams_for_result_rows

        render_diagrams_for_result_rows(
            rows, fmt=str(payload.get("diagram_format") or "png")
        )
    return rows


__all__ = [
    "run_vina_docking_rows",
    "DOCK_RUNNERS",
    "register_dock_runner",
    "run_docking_chunk",
]
