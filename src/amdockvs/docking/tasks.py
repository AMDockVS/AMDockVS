"""What a worker runs to dock a chunk of pairs.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
The ligand arrives as bytes and its poses leave as payloads: the programs read and write files,
and those live in scratch space only while the chunk runs.
"""
from __future__ import annotations

from contextlib import ExitStack, suppress
from pathlib import Path

from ms_flow.payloads import scratch_file

from amdockvs.core.constants import SERIES_POSES
from amdockvs.core.paths import get_default_project_root
from amdockvs.core.payloads import structure_payload
from amdockvs.docking.engines.registry import run_docking_chunk


def _sdf_records(text: str) -> list[str]:
    """The molecules of an SDF, each with its own terminator. Split on the terminator line only:
    a molecule's first line (its name) may be blank."""
    text = text if text.endswith("\n") else text + "\n"
    return [record + "$$$$\n" for record in text.split("$$$$\n") if record.strip()]


def _poses_as_payloads(rows: list[dict]) -> list[dict]:
    """Each row's pose, the record of its rank in the engine's SDF, as a payload of the `poses`
    series; the pose files are deleted. Path keys leave the metrics: no file outlives the task."""
    root = get_default_project_root()
    files: dict[str, list[str]] = {}
    for row in rows:
        pose = str(row.pop("pose_path", "") or "")
        metrics = {key: value for key, value in dict(row.get("metrics") or {}).items() if not key.endswith("_path")}
        row["metrics"] = metrics
        row["pose_payload_id"] = None
        if not pose:
            continue
        path = Path(pose) if Path(pose).is_absolute() or root is None else root / pose
        if str(path) not in files:
            files[str(path)] = _sdf_records(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else []
        records = files[str(path)]
        index = int(metrics.get("pose_index", int(row.get("pose_rank") or 1) - 1))
        if 0 <= index < len(records):
            row["pose_payload_id"] = structure_payload(records[index], "sdf", SERIES_POSES)
    for path in files:
        _unlink(Path(path))
        _unlink(Path(path).with_suffix(".pdbqt"))  # Vina's own output, the SDF's source
    return rows


def _unlink(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def dock_pairs(payload: dict, progress_cb=None):
    rigid = Path(str(payload.get("output_dir") or ".")) / "_rigid"  # meeko.dockable_ligand's copies
    with ExitStack() as scratch:
        pairs = []
        for pair in payload.get("pairs") or ():
            pair = dict(pair)
            ligand = pair.pop("ligand", None)
            if ligand is not None:
                path = scratch.enter_context(scratch_file(ligand, ".pdbqt"))
                scratch.callback(_unlink, rigid / f"{path.stem}.rigid.pdbqt")
                pair["ligand_path"] = str(path)
            pairs.append(pair)
        rows = _poses_as_payloads(run_docking_chunk({**payload, "pairs": pairs}, progress_cb))
    # The receptor the pose was docked into, for what is computed from the pose later.
    receptors = {pair.get("receptor_id"): pair.get("receptor_payload_id") for pair in pairs}
    for row in rows:
        row["metrics"]["receptor_payload_id"] = receptors.get(row.get("receptor_molecule_id"))
    threshold = payload.get("hit_threshold")
    if threshold is None:
        return rows
    # The predicate half of the gate: stateless, so it runs where the poses are produced and
    # the non-hits never travel back. ponytail: the pair has no row, so a re-run with
    # skip_existing will dock it again — the campaign is capped anyway; add a "screened"
    # marker table if resuming a capped run matters.
    return [row for row in rows if row.get("score") is not None and float(row["score"]) <= float(threshold)]
