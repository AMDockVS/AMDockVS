"""What a worker runs to dock a chunk of shards of a library.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from ms_flow.core.data.shard import Shard

from amdockvs.core.constants import SERIES_POSES
from amdockvs.core.payloads import structure_payload
from amdockvs.docking.engines.registry import run_docking_chunk
from amdockvs.docking.preparation.shards import extract_ligands
from amdockvs.docking.results.dataset import ResultFragmentWriter, local_pose_path, result_fragment_path, result_row

# How the cap is spent: the best `cap` of everything scored per receptor, picked from the result
# fragments when the run ends. Only an off-target stage, already narrowed to a ranking, streams
# its hits back as they come.
HIT_MODE_TOP_N = "top_n"


def _pose_files(row: dict) -> list[str]:
    pose = str(row.get("pose_path") or "")
    return [pose, str(Path(pose).with_suffix(".pdbqt"))] if pose else []  # Vina's own output too


def _discard_poses(rows: list[dict]) -> None:
    """Delete the poses of everything that missed the threshold.

    The non-hits are the whole library minus a handful. Their files are written before the
    score is known, so this is where the campaign stops filling the disk.
    """
    for row in rows:
        for value in set(_pose_files(row)):
            path = local_pose_path(value)
            if path is not None:
                with suppress(OSError):
                    path.unlink(missing_ok=True)


def _hit_row(row: dict, *, smiles: str, shard: dict, docking_run_id: int | None) -> dict[str, Any]:
    """A docking result row turned into what `ingest_hits` promotes: a molecule and its result
    columns (its `ligand_molecule_id` is the shard record until then); its pose is added by the
    caller as a payload."""
    ligand_id = int(row.get("ligand_molecule_id") or 0)
    return {
        **{key: value for key, value in row.items() if key != "pose_path"},
        "docking_run_id": docking_run_id,
        "name": f"{shard.get('library') or 'library'}:{ligand_id}",
        "import_batch_id": shard.get("import_batch_id"),
        # The shard record id *is* the identity of a library molecule: the shard's
        # `source_indices` metadata is what maps it back to a line in the original file.
        "source_index": ligand_id,
        "smiles": smiles,
    }


def dock_shards(payload: dict, progress_cb=None):
    shards = list(payload.get("shards") or [])
    total = max(1, sum(max(0, int(shard.get("n_records") or 0)) for shard in shards))
    completed = 0
    envelopes: list[dict] = []
    for shard in shards:
        shard_total = max(0, int(shard.get("n_records") or 0))

        def report(done: int, *, before: int = completed) -> None:
            if progress_cb is not None:
                progress_cb(min(99.0, 100.0 * (before + int(done)) / total))

        envelopes.append(dock_one_shard(payload, shard, progress_cb=report))
        completed += shard_total
    return envelopes


def dock_one_shard(payload: dict, shard: dict, progress_cb=None) -> dict:
    """One whole shard against the chunk's receptor: extract, dock, keep the hits.

    Split out of `run_chunk` because a chunk may carry several shards and each one is its
    own engine call — the ligand ids inside a shard start at 0, so two shards in one call
    would write over each other's poses.
    """
    shard_index = int(shard.get("shard_index") or 0)
    envelope = {
        "shard_id": int(shard.get("shard_id") or 0),
        "shard_index": shard_index,
        "chunks_total": int(payload.get("chunks_total") or 1),
        "receptor_id": int(payload.get("receptor_id") or 0),
        # What the campaign progress counts: dockings finished, hit or not.
        "scored": 0,
        "hits": [],
        "hits_found": 0,
        "result_shard": "",
    }
    shard_path = str(shard.get("shard_path") or "")
    if not shard_path:
        return envelope
    output_dir = Path(str(payload["output_dir"])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    threshold = float(payload["hit_threshold"])
    result_path = result_fragment_path(output_dir, payload, shard)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ligands_", dir=str(output_dir)) as staging:
        ligands = extract_ligands(
            shard_path,
            staging,
            record_ids=shard.get("record_ids"),
        )
        smiles = {int(ligand["id"]): str(ligand["smiles"]) for ligand in ligands}
        pairs = [
            {
                "run_kind": "screening",
                "ligand_id": int(ligand["id"]),
                "receptor_id": int(payload["receptor_id"]),
                "ligand_path": ligand["path"],
                "receptor_path": str(payload["receptor_path"]),
                "exhaustiveness": int(payload.get("exhaustiveness") or 8),
                "num_modes": int(payload.get("num_modes") or 9),
                "box_center": list(payload.get("box_center") or []),
                "box_size": list(payload.get("box_size") or []),
                "spacing": float(payload.get("spacing") or 0.375),
            }
            for ligand in ligands
        ]
        with Shard.open(shard_path) as source_shard:
            header = {
                "dataset_id": source_shard.dataset_id,
                "shard_id": source_shard.shard_id,
                "base_id": source_shard.base_id,
                "slot_count": source_shard.slot_count,
            }
        processed = 0
        hits: list[dict] = []
        hits_found = 0
        with ResultFragmentWriter(
            result_path,
            metadata={
                "dataset_id": header["dataset_id"],
                "shard_id": header["shard_id"],
                "base_id": header["base_id"],
                "slot_count": header["slot_count"],
                "import_batch_id": shard.get("import_batch_id"),
                "run_id": str(payload.get("run_id") or ""),
                "protocol": dict(payload.get("protocol_metadata") or {}),
                "receptor_id": envelope["receptor_id"],
                "selection": {
                    "mode": HIT_MODE_TOP_N,
                    "cap": int(payload.get("hit_cap") or 0),
                    "threshold": threshold,
                    **(
                        {"selected_from_receptor_id": int(payload["selected_from_receptor_id"])}
                        if payload.get("selected_from_receptor_id") else {}
                    ),
                },
            },
        ) as writer:
            def consume_pair(pair_rows: list[dict]) -> None:
                nonlocal hits_found, processed
                if not pair_rows:
                    return
                row = next(
                    (item for item in pair_rows if int(item.get("pose_rank") or 1) == 1),
                    pair_rows[0],
                )
                ligand_id = int(row.get("ligand_molecule_id") or 0)
                stored = result_row(row, smiles=smiles.get(ligand_id, ""), engine=str(payload.get("engine") or ""))
                writer.add(stored)
                is_hit = (
                    row.get("score") is not None
                    and bool(stored.get("has_pose"))
                    and (
                        bool(payload.get("selected_from_receptor_id"))
                        or float(row["score"]) <= threshold
                    )
                )
                if is_hit:
                    hits_found += 1
                    if payload.get("selected_from_receptor_id"):
                        hit = _hit_row(
                            row,
                            smiles=smiles.get(ligand_id, ""),
                            shard=shard,
                            docking_run_id=payload.get("docking_run_id"),
                        )
                        # Compressed when pickled, so in the worker: the parent only appends it.
                        hit["pose"] = structure_payload(stored["pose_text"], stored["pose_suffix"], SERIES_POSES)
                        hits.append(hit)
                # The result shard is now the durable copy. No engine-specific loose pose
                # survives the callback; a hit carries its pose as a payload.
                _discard_poses([row])
                processed += 1
                if progress_cb is not None:
                    progress_cb(processed)

            rows = run_docking_chunk({
                **payload,
                "pairs": pairs,
                "report_name": f"shard_{shard_index:05d}_r{envelope['receptor_id']:05d}.json",
                "_pair_callback": consume_pair,
                "_collect_rows": False,
            })
            # Registered engines predating the streaming callback still work; they merely
            # flush at the end of their engine call until they adopt the callback contract.
            if rows:
                by_ligand: dict[int, list[dict]] = {}
                for row in rows:
                    by_ligand.setdefault(int(row.get("ligand_molecule_id") or 0), []).append(row)
                for pair_rows in by_ligand.values():
                    consume_pair(pair_rows)
    envelope["scored"] = processed
    envelope["hits"] = hits
    envelope["hits_found"] = hits_found
    envelope["result_shard"] = str(result_path)
    return envelope
