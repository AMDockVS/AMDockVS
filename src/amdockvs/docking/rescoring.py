"""Rescoring a pose: a second scoring function over a pose the search already found.

Two ways in, both ending here:

* **With the docking.** `rescore_rows` runs in the worker as the chunk ends, while the pose
  files are still around. Only the rescored value is recorded — the row is the docking program
  *and* its rescoring function (the protocol hash carries both), and the search score it
  replaces is not stored anywhere.
* **Over stored results.** `amdockvs.docking.rescore_jobs` reads poses the database already
  has, so the docking row exists: the rescoring is a protocol of its own and writes rows of
  its own, pointing at the same stored pose.

No SQL here: a worker loads this module alone.
"""
from __future__ import annotations

import re
from contextlib import suppress
from pathlib import Path
from typing import Any, Mapping, Sequence

from amdockvs.core.constants import DEFAULT_VINA_COMMAND
from amdockvs.docking.results.metrics import SCORE_METRICS, docking_metrics, docking_row

#: Vina scores a pose where it stands with `--score_only`; `ad4` is out because its score needs
#: the autogrid maps of the box, which a rescoring run does not build.
VINA_FUNCTIONS = ("vina", "vinardo")
RESCORING_FUNCTIONS = (*VINA_FUNCTIONS, "gnina")

_VINA_SCORE = re.compile(r"Estimated Free Energy of Binding\s*:\s*(-?\d+(?:\.\d+)?)")
# gnina's kcal/mol line, not CNNaffinity: the rest of AMDock reads a score as "lower is better",
# and CNNaffinity is a pK-like value that grows the other way.
_GNINA_SCORE = re.compile(r"Affinity:\s*(-?\d+(?:\.\d+)?)")


def sdf_records(text: str) -> list[str]:
    """The molecules of an SDF, each with its own terminator. Split on the terminator line only:
    a molecule's first line (its name) may be blank."""
    text = text if text.endswith("\n") else text + "\n"
    return [record + "$$$$\n" for record in text.split("$$$$\n") if record.strip()]


def rescore_pose(
    *,
    function: str,
    receptor_path: str | Path,
    pose_path: str | Path,
    box_center: Sequence[float],
    box_size: Sequence[float],
    flex_receptor_path: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> float | None:
    """The score `function` gives one pose (`pose_path`: a single-pose SDF or PDBQT), in kcal/mol
    as every other score of the project, or None when the program said nothing usable."""
    name = str(function or "").strip().lower()
    settings = dict(config or {})
    if name in VINA_FUNCTIONS:
        return _vina_score_only(
            scoring_function=name, receptor_path=Path(receptor_path), pose_path=Path(pose_path),
            box_center=box_center, box_size=box_size, flex_receptor_path=flex_receptor_path,
            vina_command=str(settings.get("vina_command") or DEFAULT_VINA_COMMAND),
        )
    if name == "gnina":
        return _gnina_score_only(
            receptor_path=Path(receptor_path), pose_path=Path(pose_path),
            box_center=box_center, box_size=box_size,
            cnn_scoring=str(settings.get("scoring_function") or "rescore"),
        )
    raise ValueError(f"Unknown rescoring function '{function}'. Known: {sorted(RESCORING_FUNCTIONS)}.")


def rescore_rows(rows: list[dict], *, function: str, pairs: Sequence[Mapping[str, Any]],
                 config: Mapping[str, Any] | None = None) -> list[dict]:
    """Rescoring applied with the docking: each row's score becomes the rescoring's and the poses
    of a pair are ranked by it. The rows are the ones the engine just produced, so their pose
    files are still on disk; a pose the function could not score keeps no score at all."""
    name = str(function or "").strip().lower()
    if not name:
        return rows
    boxes = {(pair.get("receptor_id"), pair.get("ligand_id")): pair for pair in pairs}
    poses: dict[str, list[str]] = {}
    for row in rows:
        pair = boxes.get((row.get("receptor_molecule_id"), row.get("ligand_molecule_id")))
        path = Path(str(row.get("pose_path") or ""))
        if pair is None or row.get("score") is None or not path.is_file():
            continue
        if str(path) not in poses:
            poses[str(path)] = sdf_records(path.read_text(encoding="utf-8", errors="replace"))
        records = poses[str(path)]
        index = int(row.get("pose_rank") or 1) - 1
        if not 0 <= index < len(records):
            continue
        single = path.with_name(f"{path.stem}_rescore_{index + 1}.sdf")
        single.write_text(records[index], encoding="utf-8")
        try:
            row["score"] = rescore_pose(
                function=name, receptor_path=pair.get("receptor_path") or "", pose_path=single,
                box_center=pair.get("box_center") or (), box_size=pair.get("box_size") or (),
                flex_receptor_path=pair.get("flex_receptor_path") or None, config=config,
            )
        except Exception as exc:  # noqa: BLE001 - one unscorable pose must not lose the chunk
            row["score"], row["error"] = None, f"rescoring failed: {exc}"
        finally:
            with suppress(OSError):
                single.unlink()
        row["score_type"] = f"{name}_rescore"
        _scored(row, pair)
    return reranked(rows)


def reranked(rows: list[dict]) -> list[dict]:
    """Poses re-ranked per pair by their new score: rescoring is a reranking, and rank 1 must be
    the best the stored score knows of. Rows without a score keep their place at the end."""
    for key in {(row.get("receptor_molecule_id"), row.get("ligand_molecule_id")) for row in rows}:
        pair_rows = [row for row in rows if (row.get("receptor_molecule_id"), row.get("ligand_molecule_id")) == key]
        if len(pair_rows) < 2:
            continue
        ranks = sorted(int(row.get("pose_rank") or 1) for row in pair_rows)
        order = sorted(pair_rows, key=lambda row: (row.get("score") is None, float(row.get("score") or 0.0)))
        for rank, row in zip(ranks, order):
            row["pose_rank"] = rank
    return rows


def rescore_stored_poses(payload: dict) -> list[dict]:
    """Rescoring over results the project already has: one chunk of stored poses, each scored
    where it stands. The rows are new `docking_results` rows of the rescoring run, pointing at
    the same stored pose — the docking row they come from stays as it is."""
    rows = []
    for row in payload.get("rows") or ():
        score, error = None, ""
        try:
            score = rescore_pose(
                function=str(payload["function"]), receptor_path=row.get("receptor_path") or "",
                pose_path=Path(str(row["pose_path"])), box_center=row.get("box_center") or (),
                box_size=row.get("box_size") or (), config=payload.get("config"),
            )
        except Exception as exc:  # noqa: BLE001 - one unscorable pose must not lose the chunk
            error = f"rescoring failed: {exc}"
        new = docking_row(
            {"receptor_id": row["receptor_molecule_id"], "ligand_id": row["ligand_molecule_id"],
             "binding_site_id": row.get("binding_site_id") or 0, "complex_id": row.get("complex_id") or 0,
             "ligand_descriptors": row.get("ligand_descriptors")},
            score_type=f"{payload['function']}_rescore", pose_rank=int(row.get("pose_rank") or 1),
            score=score, error=error, extra={"rescored_from": row.get("id")},
        )
        new.pop("pose_path", None)
        rows.append({**new, "docking_run_id": payload["docking_run_id"],
                     "pose_payload_id": row.get("pose_payload_id"),
                     "receptor_payload_id": row.get("receptor_payload_id"),
                     "input_key": str(row.get("input_key") or "")})
    return reranked(rows)


def _scored(row: dict, pair: Mapping[str, Any]) -> None:
    """The columns derived from the score, derived again: they were the search score's."""
    row.update(dict.fromkeys(SCORE_METRICS))
    if row.get("score") is not None:
        row.update(docking_metrics(score=float(row["score"]),
                                   ligand_source_path=pair.get("ligand_source_path"),
                                   descriptors=pair.get("ligand_descriptors")))


def _vina_score_only(*, scoring_function: str, receptor_path: Path, pose_path: Path,
                     box_center: Sequence[float], box_size: Sequence[float],
                     flex_receptor_path: str | Path | None, vina_command: str) -> float | None:
    from amdockvs.core.worker_io import run_program

    ligand = _as_pdbqt(pose_path)
    command = [
        vina_command,
        "--score_only",
        "--scoring", scoring_function,
        "--receptor", str(receptor_path),
        "--ligand", str(ligand),
        *(("--flex", str(flex_receptor_path)) if flex_receptor_path else ()),
        # The box is not optional even for --score_only: without it Vina reads an uninitialized
        # grid and the score is noise.
        *(arg for axis, value in zip("xyz", box_center) for arg in (f"--center_{axis}", str(float(value)))),
        *(arg for axis, value in zip("xyz", box_size) for arg in (f"--size_{axis}", str(float(value)))),
    ]
    try:
        result = run_program(command)
    finally:
        if ligand != pose_path:
            with suppress(OSError):
                Path(ligand).unlink()
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip()[-400:] or "vina --score_only failed")
    found = _VINA_SCORE.search(result.stdout or "")
    return float(found.group(1)) if found else None


def _gnina_score_only(*, receptor_path: Path, pose_path: Path, box_center: Sequence[float],
                      box_size: Sequence[float], cnn_scoring: str) -> float | None:
    from amdockvs.core.worker_io import run_program
    from amdockvs.docking.engines.gnina import _gnina_env, gnina_binary, normalize_cnn_mode

    binary = gnina_binary()
    mode = normalize_cnn_mode(cnn_scoring)
    command = [
        binary, "--score_only", "-r", str(receptor_path), "-l", str(pose_path),
        *(arg for axis, value in zip("xyz", box_center) for arg in (f"--center_{axis}", str(float(value)))),
        *(arg for axis, value in zip("xyz", box_size) for arg in (f"--size_{axis}", str(float(value)))),
        *(("--cnn_scoring", "none", "--no_gpu") if mode == "none" else ("--cnn_scoring", mode)),
    ]
    result = run_program(command, env=_gnina_env(binary))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip()[-400:] or "gnina --score_only failed")
    found = _GNINA_SCORE.search(result.stdout or "")
    return float(found.group(1)) if found else None


def _as_pdbqt(pose_path: Path) -> Path:
    """The pose as the PDBQT Vina needs: itself when it already is one, else rebuilt from the SDF
    with the same coordinates (`ms_adtools`), next to it."""
    if pose_path.suffix.lower() == ".pdbqt":
        return pose_path
    from ms_adtools import prepare_ligand
    from rdkit import Chem

    mol = Chem.MolFromMolBlock(pose_path.read_text(encoding="utf-8", errors="replace"), removeHs=False)
    if mol is None:
        raise RuntimeError(f"Could not read the pose to rescore it: {pose_path.name}")
    out = pose_path.with_suffix(".rescore.pdbqt")
    out.write_text(prepare_ligand(mol, conf_id=mol.GetConformers()[0].GetId()), encoding="utf-8")
    return out


__all__ = [
    "RESCORING_FUNCTIONS",
    "VINA_FUNCTIONS",
    "reranked",
    "rescore_pose",
    "rescore_rows",
    "rescore_stored_poses",
    "sdf_records",
]
