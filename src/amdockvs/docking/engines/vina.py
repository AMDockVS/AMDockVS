from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Callable


from amdockvs.docking.results.metrics import docking_row, pose_file_stem
from amdockvs.docking.results.rmsd import pose_rmsd_detail
from amdockvs.core.paths import get_default_project_root
from amdockvs.docking.engines.autogrid import map_prefix as _ad4_map_prefix
from amdockvs.docking.methods import check_program, check_receptor
from amdockvs.docking.preparation.adtools import dockable_ligand

def _require_pdbqt(path: Path, *, kind: str) -> None:
    if path.suffix.lower() != ".pdbqt":
        raise ValueError(
            f"AutoDock Vina requires {kind} in PDBQT format, got: {path.name}. "
            "Prepare inputs first, e.g. with Meeko."
        )


_PROGRESS_STARS = 51  # the line Vina and QuickVina fill while they search: one star per 2 %


# A batch prints one of these before each ligand it docks or skips: the ligands before it are written.
_NEXT_LIGAND = ("Performing docking", "Failed parsing")


def _follow_progress_line(stream, progress: Callable[[float], None] | None = None,
                          next_ligand: Callable[[], None] | None = None) -> str:
    """Read `stream` to its end and return it; tell `progress` how many ligands are docked, the one
    on its progress line counting by how full the line is (a batch prints one line per ligand), and
    call `next_ligand` as a batch moves to its next ligand."""
    if progress is None and next_ligand is None:
        return stream.read()
    text, line, finished = [], "", 0
    for char in iter(lambda: stream.read(1), ""):
        text.append(char)
        if char == "\n":
            finished += bool(line) and not line.strip("*")
            if next_ligand is not None and line.startswith(_NEXT_LIGAND):
                next_ligand()
            line = ""
            continue
        line += char
        if progress is not None and char == "*" and not line.strip("*"):  # the banner has no star-only line
            progress(finished + min(1.0, len(line) / _PROGRESS_STARS))
    return "".join(text)


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
    # G0/G1... are Meeko's ring-closure dummies and W its waters: not atoms of the ligand.
    count = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not (raw.startswith("ATOM") or raw.startswith("HETATM")):
            continue
        ad_type = raw.split()[-1] if raw.split() else ""
        if ad_type and not ad_type.upper().startswith(("H", "G", "W")):
            count += 1
    return count


def _ligand_efficiency(score: float, heavy_atoms: int) -> float | None:
    return round(float(score) / heavy_atoms, 4) if heavy_atoms > 0 else None


def count_heavy_atoms(path: Path) -> int:
    """Heavy-atom count from any ligand file. PDBQT via AD types (no RDKit needed),
    everything else (SDF/MOL/PDB) via RDKit. 0 if unreadable. Used to backfill LE for
    results without an LE."""
    if path.suffix.lower() == ".pdbqt":
        return _count_heavy_atoms_pdbqt(path)  # column scan: no RDKit, no bond perception
    from amdockvs.io.formats import read_mol

    mol = read_mol(path, sanitize=False, remove_hs=True)
    return mol.GetNumHeavyAtoms() if mol is not None else 0


def attach_flexres(sd_text: str, pdbqt_text: str) -> str:
    """The SD records with the side chains docked with each pose as their `flexres` property (a
    PDB block: MODEL n of the docked PDBQT goes with record n), for the viewer to draw beside the
    ligand; unchanged when nothing was flexible or the poses do not pair up one to one."""
    if "BEGIN_RES" not in pdbqt_text:
        return sd_text
    from ms_adtools import flexres_models

    models = [[line for line in block.splitlines() if line.startswith("ATOM")]
              for block in flexres_models(pdbqt_text).split("ENDMDL")]
    models = [atoms for atoms in models if atoms]
    records = [record for record in sd_text.split("$$$$\n") if record.strip()]
    if len(records) > len(models):  # ponytail: a pose the SD writer dropped; pairing by rank would lie
        return sd_text
    return "".join(f"{record.rstrip(chr(10))}\n> <flexres>\n" + "\n".join(atoms) + "\n\n$$$$\n"
                   for record, atoms in zip(records, models))


def _convert_vina_pdbqt_to_sdf(*, pdbqt_path: Path, sdf_path: Path) -> Path:
    from amdockvs.docking.preparation.adtools import poses_sd

    pdbqt_text = pdbqt_path.read_text(encoding="utf-8", errors="replace")
    sd_string, _props = poses_sd(pdbqt_text, pdbqt_path.stem)
    if not sd_string.strip():
        raise RuntimeError(f"Could not convert docked PDBQT to SDF: {pdbqt_path.name}")
    sdf_path.parent.mkdir(parents=True, exist_ok=True)
    sdf_path.write_text(attach_flexres(sd_string, pdbqt_text), encoding="utf-8")
    return sdf_path


def _vina_args(
    *,
    vina_command: str,
    receptor_path: Path,
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
    quickvina: bool = False,
    progress: bool = False,
) -> list[str]:
    """The command line of one Vina run, all but its ligands and where the poses go."""
    resolved_command = shutil.which(str(vina_command)) if not Path(str(vina_command)).expanduser().exists() else str(
        Path(str(vina_command)).expanduser().resolve()
    )
    if not resolved_command:
        raise RuntimeError(
            f"vina binary is not available. Command not found: {vina_command!r}. "
            "Install AutoDock Vina (Programs) or set its path in the settings."
        )
    flex = ["--flex", str(flex_receptor_path)] if flex_receptor_path is not None else []
    if ad4_map_prefix:
        # ad4: the autogrid maps carry the rigid receptor and the box, so --maps replaces
        # --receptor, the box (--center_*/--size_*) and --spacing (docking_basic.html, ad4 section).
        grid_args = ["--maps", str(ad4_map_prefix), *flex]
    else:
        grid_args = [
            "--receptor", str(receptor_path), *flex,
            *(arg for axis, value in zip("xyz", box_center) for arg in (f"--center_{axis}", str(float(value)))),
            *(arg for axis, value in zip("xyz", box_size) for arg in (f"--size_{axis}", str(float(value)))),
        ]
    command = [
        str(resolved_command),
        *grid_args,
        "--cpu", str(int(cpu)),
        "--seed", str(int(seed)),
        "--exhaustiveness", str(int(exhaustiveness)),
        "--num_modes", str(int(num_modes)),
        "--energy_range", str(float(energy_range)),
    ]
    if not quickvina:
        # QuickVina 2.1 forked Vina 1.1.2 and exits with a usage error on any of these.
        command += [
            "--scoring", str(scoring_function),
            "--min_rmsd", str(float(min_rmsd)),
            "--verbosity", "1" if progress else "0",  # 1 prints the progress line
            *([] if ad4_map_prefix else ["--spacing", str(float(spacing))]),
        ]
    return command


def _run_vina(command: list[str], progress: Callable[[float], None] | None = None,
              next_ligand: Callable[[], None] | None = None) -> str:
    """Run a Vina command line to its end and return what it printed; raise if it failed."""
    process: subprocess.Popen[str] | None = None

    def _kill_child_process() -> None:
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
        # stderr goes to a file: nobody reads it while stdout is followed, and a full pipe
        # would stop the program.
        with tempfile.TemporaryFile("w+") as errors:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                start_new_session=(os.name == "posix"),
            )
            try:
                stdout_text = _follow_progress_line(process.stdout, progress, next_ligand)
            except BaseException:  # a callback failed or the task was cancelled: the run stops here
                _kill_child_process()
                raise
            process.wait()
            errors.seek(0)
            stderr_text = errors.read()
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
    return stdout_text


def _run_vina_binary(
    *, ligand_path: Path, output_path: Path, progress: Callable[[float], None] | None = None, **grid,
) -> tuple[list[list[float]], str]:
    """Dock one ligand: the energies of its poses, written to `output_path`, and what Vina printed."""
    command = [*_vina_args(**grid, progress=progress is not None),
               "--ligand", str(ligand_path), "--out", str(output_path)]
    stdout_text = _run_vina(command, progress)
    energies = _parse_vina_pose_energies(output_path=output_path, stdout_text=stdout_text)
    if not energies:
        raise RuntimeError(
            "vina binary did not produce parseable pose energies. "
            f"output_file={output_path} stdout={stdout_text.strip()!r}"
        )
    return energies, stdout_text


def _run_vina_batch(
    *, ligand_paths: list[Path], output_dir: Path, progress: Callable[[float], None] | None = None,
    next_ligand: Callable[[], None] | None = None, **grid,
) -> None:
    """Dock many ligands into one receptor and box with one `vina --batch`: the maps are computed
    once. Each ligand's poses go to `<output_dir>/<stem>_out.pdbqt` as soon as it is docked, and
    `next_ligand` is called then; Vina skips a ligand it cannot read and writes nothing for it."""
    command = [*_vina_args(**grid, progress=True),  # verbosity 1 marks where each ligand starts
               *(arg for path in ligand_paths for arg in ("--batch", str(path))), "--dir", str(output_dir)]
    _run_vina(command, progress, next_ligand)


# ponytail: characters of ligand paths per `vina --batch`, so the command line stays under the
# ~32k of Windows (a few hundred ligands). The maps are computed once per batch; rows come per ligand.
_BATCH_CHARS = 30_000


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
    vina_command: str = "vina",
    run_id: str = "",
    protocol_metadata: dict | None = None,
    report_name: str | None = None,
    pair_callback: Callable[[list[dict]], None] | None = None,
    pair_progress: Callable[[float], None] | None = None,
    collect_rows: bool = True,
    engine: str = "vina",
    method: str = "standard",
) -> list[dict]:
    """Dock `pairs` with the Vina (or QuickVina) binary. Consecutive pairs with the same receptor,
    box and settings dock together in one `vina --batch`; what a batch did not dock is docked alone,
    which says why it fails. QuickVina has no batch mode: it docks one ligand per run."""
    resolved_output_dir = Path(output_dir).expanduser().resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    quickvina = engine == "qvina"  # same search and files as Vina, through its own binary
    docking_method = check_program(method, engine, scoring_function)

    rows: list[dict] = []
    failures: list[dict] = []
    success_count = 0
    project_root = get_default_project_root()
    ad4_map_cache: dict[tuple, str] = {}
    ad4_maps_dir = resolved_output_dir / "_ad4_maps"
    protocol_payload = dict(protocol_metadata or {})

    def report(pair_rows: list[dict]) -> None:
        if collect_rows:
            rows.extend(pair_rows)
        if pair_callback is not None:
            pair_callback(pair_rows)

    def fail(pair: dict, error: str, **detail) -> None:
        failures.append({
            "complex_id": int(pair.get("complex_id") or 0) or None,
            "run_kind": str(pair.get("run_kind") or "screening"),
            "ligand_id": int(pair.get("ligand_id") or 0),
            "receptor_id": int(pair.get("receptor_id") or 0),
            "ligand_path": str(pair.get("ligand_path_logical") or _resolved(pair.get("ligand_path"))),
            "receptor_path": str(pair.get("receptor_path_logical") or _resolved(pair.get("receptor_path"))),
            **detail,
            "error": error,
        })
        report([docking_row(pair, score_type="vina_score", error=error)])

    def prepare(pair: dict) -> dict:
        """What docking `pair` takes: its files checked and made dockable, and its Vina arguments."""
        ligand_path, receptor_path = _resolved(pair.get("ligand_path")), _resolved(pair.get("receptor_path"))
        pair_box_center = [float(value) for value in (pair.get("box_center") or box_center or [])]
        pair_box_size = [float(value) for value in (pair.get("box_size") or box_size or [])]
        pair_spacing = float(pair.get("spacing") or spacing)
        if not ligand_path.exists():
            raise FileNotFoundError(f"ligand file missing: {ligand_path}")
        if not receptor_path.exists():
            raise FileNotFoundError(f"receptor file missing: {receptor_path}")
        _require_pdbqt(ligand_path, kind="ligand")
        _require_pdbqt(receptor_path, kind="receptor")
        check_receptor(docking_method, receptor_path, pair_box_center, pair_box_size)
        ligand_path = dockable_ligand(ligand_path, program=engine, scoring_function=scoring_function,
                                      output_dir=resolved_output_dir)
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
                zinc=docking_method.zinc_site,
            )
        output_stem = pose_file_stem(pair, protocol_payload)
        return {
            "pair": pair,
            "ligand_path": ligand_path,
            "pdbqt": resolved_output_dir / f"{output_stem}.dock.pdbqt",
            "sdf": resolved_output_dir / f"{output_stem}.dock.sdf",
            "grid": {
                "vina_command": vina_command,
                "receptor_path": receptor_path,
                "flex_receptor_path": flex_receptor_path,
                "box_center": pair_box_center,
                "box_size": pair_box_size,
                "spacing": pair_spacing,
                "scoring_function": scoring_function,
                "cpu": vina_cpu,
                "seed": seed,
                "exhaustiveness": int(pair.get("exhaustiveness") or 8),
                "num_modes": int(pair.get("num_modes") or 9),
                "min_rmsd": float(min_rmsd),
                "energy_range": float(energy_range),
                "ad4_map_prefix": ad4_map_prefix,
                "quickvina": quickvina,
            },
        }

    def finish(item: dict, energies: list[list[float]]) -> None:
        nonlocal success_count
        pair, ligand_path, output_sdf_path = item["pair"], item["ligand_path"], item["sdf"]
        _convert_vina_pdbqt_to_sdf(pdbqt_path=item["pdbqt"], sdf_path=output_sdf_path)
        pose_path_text = str(output_sdf_path)
        if project_root is not None:
            with suppress(Exception):
                pose_path_text = str(output_sdf_path.relative_to(project_root))
        heavy_atoms = 0
        with suppress(Exception):
            heavy_atoms = _count_heavy_atoms_pdbqt(ligand_path)
        pair_rows = [
            docking_row(
                pair,
                score_type="vina_score",
                pose_rank=index,
                score=float(energy_row[0]) if len(energy_row) else 0.0,
                pose_path=pose_path_text,
                rmsd=pose_rmsd_detail(
                    reference_ligand_path=pair.get("reference_ligand_path"),
                    pose_path=output_sdf_path,
                    pose_rank=index,
                ) if str(pair.get("run_kind") or "") == "redocking" else None,
                heavy_atoms=heavy_atoms,
            )
            for index, energy_row in enumerate(energies, start=1)
        ]
        success_count += 1
        report(pair_rows)

    def dock(items: list[dict]) -> None:
        """Dock items that share their Vina arguments: in one batch when there are several, each
        ligand reported as soon as the batch has docked it."""
        batch_dir = resolved_output_dir / f"_batch_{uuid.uuid4().hex[:12]}"
        reported: set[int] = set()

        def collect() -> None:
            """Report the ligands whose batch poses are written."""
            for index, item in enumerate(items):
                batched = batch_dir / f"{item['ligand_path'].stem}_out.pdbqt"
                energies = _parse_vina_pose_energies(output_path=batched) if (
                    index not in reported and batched.exists()) else []
                if energies:
                    reported.add(index)
                    settle(item, energies, batched)

        def batch_progress(docked: float) -> None:  # pair_progress counts past the reported pairs
            if pair_progress is not None:
                pair_progress(docked - len(reported))

        try:
            if len(items) > 1 and not quickvina:
                batch_dir.mkdir()
                # ponytail: a failed batch (a crash, a kill) keeps what it wrote; the rest docks alone below.
                with suppress(RuntimeError, OSError):
                    _run_vina_batch(ligand_paths=[item["ligand_path"] for item in items], output_dir=batch_dir,
                                    progress=batch_progress, next_ligand=collect, **items[0]["grid"])
                collect()
            for index, item in enumerate(items):
                if index not in reported:
                    settle(item, [], None)
        finally:
            shutil.rmtree(batch_dir, ignore_errors=True)

    def settle(item: dict, energies: list[list[float]], batched: Path | None) -> None:
        """Report `item` from its batch poses, or dock it alone when the batch has none for it."""
        pair = item["pair"]
        try:
            if energies:
                os.replace(batched, item["pdbqt"])
            else:
                energies, _ = _run_vina_binary(ligand_path=item["ligand_path"], output_path=item["pdbqt"],
                                               progress=pair_progress, **item["grid"])
            finish(item, energies)
        except Exception as exc:
            fail(
                pair, str(exc),
                reference_ligand_path=str(pair.get("reference_ligand_path_logical")
                                          or pair.get("reference_ligand_path") or ""),
                reference_receptor_path=str(pair.get("reference_receptor_path_logical")
                                            or pair.get("reference_receptor_path") or ""),
                grid={key: item["grid"][key] for key in ("box_center", "box_size", "spacing",
                                                          "scoring_function")},
            )

    batch: list[dict] = []
    for pair in pairs:
        invalid_reason = str(pair.get("invalid_reason") or "").strip()
        if invalid_reason:
            fail(pair, invalid_reason)
            continue
        try:
            item = prepare(pair)
        except Exception as exc:
            fail(pair, str(exc), grid={
                "box_center": [float(value) for value in (pair.get("box_center") or box_center or [])],
                "box_size": [float(value) for value in (pair.get("box_size") or box_size or [])],
                "spacing": float(pair.get("spacing") or spacing),
                "scoring_function": str(scoring_function),
            })
            continue
        # Vina names a batch's outputs after the ligand files, so two of one name go apart.
        if batch and (item["grid"] != batch[0]["grid"]
                      or sum(len(str(other["ligand_path"])) + 9 for other in batch) >= _BATCH_CHARS
                      or item["ligand_path"].stem in {other["ligand_path"].stem for other in batch}):
            dock(batch)
            batch = []
        batch.append(item)
    if batch:
        dock(batch)
    if failures:
        report_dir = resolved_output_dir / "_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_file = report_dir / (report_name or f"chunk_{uuid.uuid4().hex}.json")
        report_file.write_text(
            json.dumps(
                {
                    "engine": engine,
                    "generated_at": datetime.now().isoformat(),
                    "pair_count": len(pairs),
                    "success_count": success_count,
                    "failure_count": len(failures),
                    "run_id": str(run_id or ""),
                    "failures": failures,
                },
                indent=2,
                ensure_ascii=True,
                default=str,
            ),
            encoding="utf-8",
        )
    return rows


def _resolved(path) -> Path:
    return Path(str(path or "")).expanduser().resolve()


# ---------------------------------------------------------------------------
# Engine runner registry
#
# A docking chunk carries an "engine" key; the job dispatches to the registered
# runner for that engine. Each runner takes the chunk payload dict and returns
# the docking result rows. This is the seam for adding new docking programs
# (e.g. AutoDock-GPU) without touching the job/sink plumbing.
# ---------------------------------------------------------------------------

def _vina_here(command: str) -> str:
    """The Vina executable for the machine that runs the chunk.

    The payload carries what the submitting machine found; on another host that path
    may not exist, so the worker looks again (as qvina does).
    """
    if Path(command).expanduser().exists() or shutil.which(command):
        return command
    from amdockvs.core.constants import vina_command

    return vina_command()


def _vina_dock_runner(payload: dict) -> list[dict]:
    return run_vina_docking_rows(
        pairs=list(payload.get("pairs") or []),
        output_dir=str(payload.get("output_dir") or ""),
        box_center=[float(value) for value in (payload.get("box_center") or [])],
        box_size=[float(value) for value in (payload.get("box_size") or [])],
        scoring_function=str(payload.get("scoring_function") or "vina"),
        vina_command=_vina_here(str(payload.get("vina_command") or "vina")),
        vina_cpu=int(payload.get("vina_cpu") or 1),
        seed=int(payload.get("seed") or 0),
        spacing=float(payload.get("spacing") or 0.375),
        energy_range=float(payload.get("energy_range") or 3.0),
        min_rmsd=float(payload.get("min_rmsd") or 1.0),
        run_id=str(payload.get("run_id") or ""),
        protocol_metadata=dict(payload.get("protocol_metadata") or {}),
        report_name=str(payload.get("report_name") or "").strip() or None,
        pair_callback=payload.get("_pair_callback"),
        pair_progress=payload.get("_pair_progress"),
        collect_rows=bool(payload.get("_collect_rows", True)),
        method=str(payload.get("method") or "standard"),
    )


from amdockvs.docking.engines.registry import (  # noqa: E402 - compatibility exports
    DOCK_RUNNERS,
    load_builtin_docking_engines,
    register_dock_runner,
    register_docking_engine,
    run_docking_chunk,
)


__all__ = [
    "run_vina_docking_rows",
    "DOCK_RUNNERS",
    "register_dock_runner",
    "register_docking_engine",
    "load_builtin_docking_engines",
    "run_docking_chunk",
]
