"""What a worker runs to dock a chunk of pairs.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
The ligand arrives as bytes and its poses leave as payloads: the programs read and write files,
and those live in scratch space only while the chunk runs.
"""
from __future__ import annotations

from contextlib import ExitStack, suppress
from pathlib import Path

from ms_flow.engine import chain, report, task
from ms_flow.payloads import PayloadGroup, PayloadPart, scratch_file

from amdockvs.core.constants import SERIES_POSES
from amdockvs.core.paths import get_default_project_root
from amdockvs.core.rows import interaction_rows
from amdockvs.docking.results.tasks import detect_interactions
from amdockvs.docking.engines.registry import run_docking_chunk


def _sdf_records(text: str) -> list[str]:
    """The molecules of an SDF, each with its own terminator. Split on the terminator line only:
    a molecule's first line (its name) may be blank."""
    text = text if text.endswith("\n") else text + "\n"
    return [record + "$$$$\n" for record in text.split("$$$$\n") if record.strip()]


def _poses_as_payloads(rows: list[dict]) -> list[dict]:
    """Each row's pose, the record of its rank in the engine's SDF, as a part of one payload group
    per file (the poses of a pair are one needle, each with its own id); the pose files are deleted."""
    root = get_default_project_root()
    groups: dict[str, tuple[list[str], PayloadGroup]] = {}
    for row in rows:
        pose = str(row.pop("pose_path", "") or "")
        row["pose_payload_id"] = None
        if not pose:
            continue
        path = Path(pose) if Path(pose).is_absolute() or root is None else root / pose
        if str(path) not in groups:
            records = _sdf_records(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else []
            groups[str(path)] = records, PayloadGroup(kind="sdf", series=SERIES_POSES)
        records, group = groups[str(path)]
        index = int(row.get("pose_rank") or 1) - 1
        if 0 <= index < len(records):
            row["pose_payload_id"] = group.add(records[index].encode())
    for path in groups:
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
        row["docking_run_id"] = payload.get("docking_run_id")
        row["receptor_payload_id"] = receptors.get(row.get("receptor_molecule_id"))
    threshold = payload.get("hit_threshold")
    if threshold is None:
        return rows
    # The predicate half of the gate: stateless, so it runs where the poses are produced and
    # the non-hits never travel back. ponytail: the pair has no row, so a re-run with
    # skip_existing will dock it again — the campaign is capped anyway; add a "screened"
    # marker table if resuming a capped run matters.
    return [row for row in rows if row.get("score") is not None and float(row["score"]) <= float(threshold)]


# The steps of a docking chunk, each named after the table its rows go to (`DockingJobSpec.output_spec`).
# ponytail: `thread=True` is what the chain declares; the job decides per submit (`_waits_for_program`).
@task(thread=True)
def docking_results(payload: dict):
    return dock_pairs(payload, lambda percent: report(float(percent) / 100.0))


@task(thread=True)
def interaction_results(payload: dict, previous: dict):
    """The interactions of the poses just docked, when the chunk asks for them: the pose payload
    the docking row stores, in the receptor file the pair docked into."""
    if not payload.get("interactions"):
        return []
    files = {pair.get("receptor_payload_id"): Path(str(pair.get("receptor_path"))) for pair in payload.get("pairs") or ()}
    # ponytail: `.data` is the raw record: a group is only compressed when pickled, after the chain.
    poses = [row for row in previous.get("docking_results") or ()
             if isinstance(row.get("pose_payload_id"), PayloadPart) and row.get("receptor_payload_id") in files]
    receptors = {key: (files[key].read_bytes(), files[key].suffix.lstrip(".") or "pdbqt")
                 for key in {row["receptor_payload_id"] for row in poses}}
    found = detect_interactions([(row["pose_payload_id"].data, *receptors[row["receptor_payload_id"]])
                                 for row in poses])
    rows = []
    for row, interactions in zip(poses, found):
        if interactions is not None:  # the same part object: the sink stores it once, one id for both rows
            rows.extend(interaction_rows(row["receptor_payload_id"], row["pose_payload_id"], interactions))
    return rows


dock_with_interactions = chain(docking_results, interaction_results)
