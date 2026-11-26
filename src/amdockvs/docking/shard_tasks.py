"""What a worker runs to dock a chunk of shards of a library.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import numpy as np
from ms_flow.core.data.shard import Shard
from ms_flow.engine import task

from amdockvs.core.constants import SERIES_POSES
from amdockvs.core.payloads import structure_payload
from amdockvs.docking.engines.registry import run_docking_chunk
from amdockvs.docking.rescoring import rescore_rows
from amdockvs.docking.preparation.shards import extract_ligands
from amdockvs.docking.results.dataset import (
    ResultFragmentWriter, _pose, _selected_payloads, _summary_table, _top, load_hits, local_pose_path, payload_part,
    payload_role, result_fragment_path, result_metadata, result_row, summary_role,
)

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
        # Which of the receptor's sites this piece of work docked into (0: none).
        "binding_site_id": int(payload.get("binding_site_id") or 0),
        # What the campaign progress counts: dockings finished, hit or not.
        "scored": 0,
        "hits": [],
        "hits_found": 0,
        "result_shard": "",
        # The shard's ligands docked (a score and a pose) and failed (the rest, also those the
        # engine never returned): the parent's done-set lists the fewer of the two.
        "done_ids": [],
        "failed_ids": [],
        # Where the parent files this: the target's key, under which cuts this shard was docked
        # (the exclusions may have left this one partial) and the generation of its id.
        "state_key": str(payload.get("state_key") or ""),
        "narrowed_by": str(shard.get("narrowed_by") or ""),
        "generation_id": shard.get("generation_id"),
        "docking_run_id": payload.get("docking_run_id"),
        "input_key": str(payload.get("input_key") or ""),
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
            shard=shard.get("prepared_shard"),
        )
        smiles = {int(ligand["id"]): str(ligand["smiles"]) for ligand in ligands}
        pairs = [
            {
                "run_kind": "screening",
                "ligand_id": int(ligand["id"]),
                "receptor_id": int(payload["receptor_id"]),
                "binding_site_id": int(payload.get("binding_site_id") or 0) or None,
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
        with Shard.open(shard_path, shard=shard.get("prepared_shard")) as source_shard:
            header = {
                "dataset_id": source_shard.dataset_id,
                "shard_id": source_shard.shard_id,
                "base_id": source_shard.base_id,
                "slot_count": source_shard.slot_count,
            }
        processed = 0
        done: list[int] = []
        hits: list[dict] = []
        hits_found = 0
        # ponytail: the first rescoring function of the protocol, as in `docking.tasks`.
        rescoring = next((str(item.get("program") or "")
                          for item in (payload.get("protocol_metadata") or {}).get("rescoring") or ()), "")
        with ResultFragmentWriter(
            result_path,
            metadata={
                "dataset_id": header["dataset_id"],
                "shard_id": header["shard_id"],
                "base_id": header["base_id"],
                "slot_count": header["slot_count"],
                "import_batch_id": shard.get("import_batch_id"),
                "run_id": str(payload.get("run_id") or ""),
                "input_key": str(payload.get("input_key") or ""),
                "protocol": dict(payload.get("protocol_metadata") or {}),
                "receptor_id": envelope["receptor_id"],
                "binding_site_id": envelope["binding_site_id"],
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
            # The library shard docked: a newer result of it replaces this one's rows.
            origin=int(shard.get("shard_id") or 0), base_id=header["base_id"], slot_count=header["slot_count"],
        ) as writer:
            def consume_pair(pair_rows: list[dict]) -> None:
                nonlocal hits_found, processed
                if not pair_rows:
                    return
                if rescoring:
                    # Rescored where the pose was made: the stored score is the rescoring's,
                    # and the hit test below reads it, so hits are hits of the new function.
                    pair_rows = rescore_rows(pair_rows, function=rescoring, pairs=pairs,
                                             config=payload.get("engine_config"))
                row = next(
                    (item for item in pair_rows if int(item.get("pose_rank") or 1) == 1),
                    pair_rows[0],
                )
                ligand_id = int(row.get("ligand_molecule_id") or 0)
                stored = result_row(row, smiles=smiles.get(ligand_id, ""), engine=str(payload.get("engine") or ""),
                                    shard_id=envelope["shard_id"], docking_run_id=payload.get("docking_run_id"))
                writer.add(stored)
                if row.get("score") is not None and stored.get("has_pose"):
                    done.append(ligand_id)
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
                        hit["input_key"] = str(payload.get("input_key") or "")
                        # Compressed when pickled, so in the worker: the parent only appends it.
                        hit["pose"] = structure_payload(stored["pose_text"], stored["pose_suffix"], SERIES_POSES).packed()
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
                "report_name": f"shard_{envelope['shard_id']:06d}_r{envelope['receptor_id']:05d}.json",
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
    envelope["done_ids"] = np.unique(done).astype(int).tolist()
    envelope["failed_ids"] = np.setdiff1d([pair["ligand_id"] for pair in pairs], done).astype(int).tolist()
    envelope["hits"] = hits
    envelope["hits_found"] = hits_found
    envelope["result_shard"] = str(result_path)
    return envelope


# --- The stages of a campaign, run on the cluster (see `amdockvs.docking.campaign`) ----------


def _kept(element: dict) -> list[dict]:
    """The rows of what an earlier campaign of this project already left on this disk, so a
    second one over the same library only points at it (its folder purged = prepared again)."""
    from ms_flow.core.data.catalog import file_row

    rows = []
    for shard in element["shards"]:
        where = Path(str(shard["output_dir"])).expanduser().resolve() / str(shard["output_name"])
        if not where.is_file():
            return []
        rows.append(file_row(where, role=str(element["role"]), generation_id=shard.get("generation_id")))
    return rows


@task()
def prepare_stage(element: dict) -> dict:
    """A shard of a library prepared there, by a campaign that found it unprepared here: left
    where `located` finds it; only its catalog row comes back, never the file."""
    from amdockvs.docking.preparation.tasks import prepare_ligand_shards

    rows = _kept(element) or prepare_ligand_shards(element)
    if not any(row.get("n_records") for row in rows):  # else it would count as prepared there
        raise RuntimeError(f"shard {element['shards'][0]['shard_id']}: nothing of it could be prepared")
    # Its file stays here: what comes back is its row, so the campaign registers where it is and
    # the next one to this host does not prepare it again.
    files = [{key: row[key] for key in ("path", "role", "n_shards", "n_records", "n_active", "scopes")}
             for row in rows]
    return {"stage": "prep", "shard_id": element["shards"][0]["shard_id"], "files": files}


def located(shard: dict) -> dict:
    """A shard as `dock_shards` reads it: one the campaign prepared there is where it left it."""
    if shard.get("shard_path") or not shard.get("prepared_in"):
        return shard
    return {**shard, "shard_path": os.path.join(shard["prepared_in"], shard["prepared_as"])}


def prepared_elements(_prepared, elements: list[dict]):
    """The elements of `vs` after `prep`: those whose shards are all there. A shard that could not
    be prepared failed its own element of `prep`; its chunks are not docked."""
    for element in elements:
        shards = [located(shard) for shard in element["shards"]]
        if all(os.path.isfile(shard["shard_path"]) for shard in shards):
            yield {**element, "shards": shards}


@task()
def dock_stage(element: dict) -> dict:
    """`dock_shards` as a stage's task: its envelopes, said which stage and which library they are of.
    A shard the campaign prepared on this disk (in an earlier campaign too) is where `located` says."""
    element = {**element, "shards": [located(shard) for shard in element["shards"]]}
    envelopes = dock_shards(element)
    for envelope, shard in zip(envelopes, element["shards"]):
        envelope.update(library=str(shard.get("library") or "library"), import_batch_id=shard.get("import_batch_id"))
    return {"stage": element["stage"], "envelopes": envelopes}


def campaign_receipts(result: dict) -> list[dict]:
    """The receipts of a `dock_stage` result: each docked shard's two parts, committed there into
    its key's containers (`Stage.commit`), payload and summary as one, as `ShardHitWriter` does here."""
    receipts = []
    for envelope in result["envelopes"]:
        path = str(envelope.get("result_shard") or "")
        if path:
            key = str(envelope["state_key"])
            receipts.append({"part": f"{key}:{int(envelope['shard_id'])}", "path": path, "role": summary_role(key),
                             "bulk": str(payload_part(path)), "bulk_role": payload_role(key),
                             "generation_id": envelope.get("generation_id"), "folder": str(Path(path).parent.parent)})
    return receipts


def rank(results, *, cap: int, threshold: float) -> list[dict]:
    """The best `cap` of each receptor below `threshold`, over every fragment the stage wrote
    there (in the containers they were committed to, `_committed`): as references into them,
    which is what `hit_poses` and `off_target_elements` read."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc

    tables, tags = [], []
    for result in results:
        committed = iter(result.get("_committed") or ())
        for envelope in result["envelopes"]:
            path = str(envelope.get("result_shard") or "")
            if not path:
                continue
            where = next(committed, None) or {"path": path, "shards": None}  # a part not committed: itself
            table = _summary_table(Path(where["path"]), {"status": "done", "score__lte": float(threshold)},
                                   where["shards"])
            tables.append(table.append_column("_tag", pa.array(np.full(table.num_rows, len(tags)))))
            tags.append({"_result_shard": where["path"], "_shard_id": int(envelope["shard_id"]),
                         "_library": envelope["library"], "_import_batch_id": envelope["import_batch_id"],
                         "_payload": where.get("bulk") or {"path": str(payload_part(path)), "shards": None}})
    if not tables:
        return []
    table = pa.concat_tables(tables)
    return [{**row, **tags[row.pop("_tag")]}
            for receptor in pc.unique(table["receptor_id"]).to_pylist()
            for row in _top(table.filter(pc.equal(table["receptor_id"], receptor)), int(cap)).to_pylist()]


def by_fragment(refs: list[dict]) -> list[dict]:
    """The elements of the stage that reads the poses of the ranked: one for each fragment."""
    fragments: dict[str, list[dict]] = {}
    for ref in refs:
        fragments.setdefault(ref["_result_shard"], []).append(ref)
    return [{"fragment": path, "refs": rows} for path, rows in fragments.items()]


def top_n_hits(refs: dict, progress_cb=None) -> dict:
    """A chunk of `shard_top_n_job` (`hit_refs` of one receptor): its hits, each with its pose
    read and compressed here, so the parent only inserts them."""
    return {"hits": load_hits(refs)}


@task()
def hit_poses(element: dict) -> dict:
    """The ranked of one fragment as hits (the rows `ingest_hits` takes), each with its pose (the
    payload part beside the fragment), compressed here."""
    refs = element["refs"]
    keys = {(int(r["_shard_id"]), int(r["ligand_id"]), int(r["receptor_id"])) for r in refs}
    bulks: dict[str, set | None] = {}  # the payload containers of these refs, and which of their shards
    for ref in refs:
        bulk = ref.get("_payload") or {"path": str(payload_part(element["fragment"])), "shards": None}
        if bulk["shards"] is None or bulks.get(bulk["path"], set()) is None:
            bulks[bulk["path"]] = None
        else:
            bulks.setdefault(bulk["path"], set()).update(bulk["shards"])
    payloads = {}
    for path, shards in bulks.items():
        payloads.update(_selected_payloads(Path(path), None if shards is None else sorted(shards), keys))
    input_key = str(result_metadata(element["fragment"]).get("input_key") or "")
    hits = []
    for ref in refs:
        payload = payloads.get((int(ref["_shard_id"]), int(ref["ligand_id"]), int(ref["receptor_id"])))
        payload = None if payload is None else {**ref, **payload}
        if payload is None:
            raise ValueError(f"{element['fragment']}: ligand {ref['ligand_id']} is ranked but not in it")
        pose_text, pose_suffix = _pose(payload)
        ligand_id = int(payload["ligand_id"])
        if not pose_text:
            raise ValueError(f"{element['fragment']}: ligand {ligand_id} has no embedded pose")
        hits.append({
            **json.loads(payload.get("metrics_json") or "{}"),
            "name": f"{ref['_library']}:{ligand_id}",
            "import_batch_id": ref["_import_batch_id"],
            "source_index": ligand_id,
            "smiles": str(payload.get("smiles") or ""),
            "score": float(payload["score"]),
            "score_type": str(payload.get("score_type") or ""),
            "pose_rank": 1,
            "receptor_molecule_id": int(payload["receptor_id"]),
            "binding_site_id": int(payload.get("binding_site_id") or 0) or None,
            "input_key": input_key,
            "pose": structure_payload(pose_text, pose_suffix, SERIES_POSES).packed(),  # compressed here
            "_result_shard": element["fragment"],
            "_result_record_id": ligand_id,
        })
    return {"stage": "hits", "hits": hits}


def off_target_elements(refs: list[dict], template: dict, targets: list[dict], shards: dict, reference: int,
                        top: int):
    """The best `top` of the `reference` receptor docked against each off-target: only their
    records are taken out of their shards. `refs` are what `rank` made, the best first."""
    picked: dict[int, set[int]] = {}
    for ref in [ref for ref in refs if int(ref["receptor_id"]) == int(reference)][:int(top)]:
        picked.setdefault(int(ref["_shard_id"]), set()).add(int(ref["ligand_id"]))
    for shard_id, ids in sorted(picked.items()):
        shard = {**located(shards[shard_id]), "record_ids": sorted(ids), "n_records": len(ids)}
        for target in targets:
            yield {**template, **target, "stage": "off", "shards": [shard], "chunks_total": len(targets),
                   "selected_from_receptor_id": int(reference)}
