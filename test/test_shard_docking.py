"""Docking a sharded library: the slice the engine reads, and the countdown that ends a shard.

The two pieces with arithmetic in them. Everything else (Vina, the gate) is already covered.
"""
import gzip
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from types import SimpleNamespace

from ms_flow.core.data.shard import PayloadKind, Serializer, Shard, ShardWriter

from amdockvs.docking import shard_jobs
from amdockvs.docking.shard_jobs import ShardHitWriter
from amdockvs.docking.preparation.shards import (
    extract_ligands,
    failure_report,
    read_failure_log,
    smiles_from_pdbqt,
    write_failure_log,
)


def _pdbqt_shard(path: Path, count: int = 5) -> Path:
    with ShardWriter(
        path,
        dataset_id=None,
        shard_id=0,
        base_id=0,
        kind=PayloadKind.PDBQT,
        serializer=Serializer.UTF8,
        slot_count=count,
        metadata={"source": "lib.smi"},
    ) as writer:
        for logical_id in range(count):
            writer.add(logical_id, f"REMARK SMILES CC{'O' * logical_id}\nATOM {logical_id}\n".encode())
    return path


def test_a_slice_comes_out_as_files_with_the_record_ids_and_their_smiles(tmp_path):
    shard = _pdbqt_shard(tmp_path / "shard_0.mshard")
    ligands = extract_ligands(shard, tmp_path / "staging", start=2, count=2)
    assert [ligand["id"] for ligand in ligands] == [2, 3]
    assert [Path(ligand["path"]).name for ligand in ligands] == ["2.pdbqt", "3.pdbqt"]
    assert ligands[0]["smiles"] == "CCOO"
    # Only the slice was written: a chunk never unpacks the whole shard.
    assert sorted(p.name for p in (tmp_path / "staging").iterdir()) == ["2.pdbqt", "3.pdbqt"]


def test_selected_record_ids_extract_only_the_reference_top_n(tmp_path):
    shard = _pdbqt_shard(tmp_path / "shard_0.mshard")
    ligands = extract_ligands(shard, tmp_path / "selected", record_ids=[1, 4])
    assert [ligand["id"] for ligand in ligands] == [1, 4]
    assert sorted(path.name for path in (tmp_path / "selected").iterdir()) == ["1.pdbqt", "4.pdbqt"]


def test_a_wrapped_smiles_remark_is_joined_and_the_idx_remark_ignored():
    text = "REMARK SMILES CCc1cc\nREMARK SMILES ccc1O\nREMARK SMILES IDX 1 1 2 2\nATOM\n"
    assert smiles_from_pdbqt(text) == "CCc1ccccc1O"


def test_the_header_tallies_the_reasons_and_the_log_beside_the_shard_holds_every_id(tmp_path):
    failures = [(1, "a"), (2, "b"), (3, "a")]
    assert failure_report(failures) == {"n_failed": 3, "reasons": {"a": 2, "b": 1}}
    shard = tmp_path / "shard_0.mshard"
    log = write_failure_log(shard, failures)
    assert log.name == "shard_0.mshard.failures.jsonl"
    assert read_failure_log(shard) == [
        {"id": 1, "reason": "a"}, {"id": 2, "reason": "b"}, {"id": 3, "reason": "a"}
    ]
    assert write_failure_log(tmp_path / "clean.mshard", []) is None


class _Recorder:
    """Stands in for the ShardStore: materialize() is §4's job, already tested."""

    def __init__(self):
        self.states = []
        self.hits = []
        self.gate = SimpleNamespace(take=lambda rows: rows, discarded=lambda: [])
        self.payload = "light"

    def materialize(self, rows):
        self.hits.append(rows)


def test_a_shard_run_receives_one_campaign_scoped_update_per_receptor(tmp_path, monkeypatch):
    seen = _Recorder()
    monkeypatch.setattr(shard_jobs, "advance_target", lambda *a, **k: None)  # its own test
    monkeypatch.setattr(shard_jobs, "advance_shard_run", lambda _db, **kw: seen.states.append(kw))
    writer = ShardHitWriter(
        project_db=None,
        hit_cap=10,
        hit_threshold=-8.0,
        run_id="run-1",
        engine="vina",
        protocol_hash="proto-1",
    )
    writer.store = seen
    writer._materialize_selected = seen.materialize

    for _ in range(3):
        writer.handle("c", [{
            "shard_id": 4,
            "source": "lib.smi",
            "shard_index": 0,
            "chunks_total": 3,
            "hits": [{"score": -9}],
        }])

    assert len(seen.states) == 3
    assert all(row["run_id"] == "run-1" and row["shard_id"] == 4 for row in seen.states)
    assert all(row["engine"] == "vina" and row["protocol_hash"] == "proto-1" for row in seen.states)
    assert len(seen.hits) == 3


def test_the_run_panel_submits_a_campaign_instead_of_pairs():
    """The blocking dialog is gone: a sharded library submits `run_shards`, gate included."""
    import pytest

    pytest.importorskip("PySide6")
    from amdockvs.docking.readiness import EntityReadiness
    from amdockvs.ui.tools.docking.run_panel import RunPanel

    submitted = {}

    class _Panel(RunPanel):
        def __init__(self):
            self.runtime = SimpleNamespace(
                molecules=SimpleNamespace(shard_counts=lambda: {
                    "shards": 12, "records": 12000, "prepared": 12, "prepared_records": 11969,
                }),
                docking=SimpleNamespace(run_shards=lambda **kwargs: submitted.update(kwargs) or "job-1"),
            )
            self.readiness_service = SimpleNamespace(
                receptors=lambda _scope, program: EntityReadiness(total=1, ready=1, ready_ids=(7,))
            )

        def _resolve_receptor_scope(self, ids):
            return ("scope", tuple(ids or ()))

    panel = _Panel()
    inp = {"sharded": True, "program": "vina", "sel_rec": []}
    counts = panel._compute_requirement_counts(inp)
    assert counts["mode"] == "sharded" and counts["ready"] is True
    assert counts["records"] == 11969

    result = panel._submit_shard_docking(inp, {
        "protocols": [{"program": "vina", "label": "Vina", "hash": "abc12345", "config": {"exhaustiveness": 16}}],
        "batch_size": 64, "hit_threshold": -8.5, "hit_cap": 500,
        "executor_name": "local_cpu", "skip_existing": True, "run_id": "run-1",
    })
    assert result["job_ids"] == {"1:Vina:abc12345": "job-1"}
    assert submitted["hit_threshold"] == -8.5 and submitted["hit_cap"] == 500
    assert submitted["exhaustiveness"] == 16
    assert "batch_size" not in submitted
    assert submitted["receptor_set"] == ("scope", (7,))


def test_the_run_panel_chains_reference_then_off_target_jobs():
    import pytest

    pytest.importorskip("PySide6")
    from amdockvs.docking.readiness import EntityReadiness
    from amdockvs.ui.tools.docking.run_panel import RunPanel

    submitted = []

    def run_shards(**kwargs):
        submitted.append(kwargs)
        return f"job-{len(submitted)}"

    class _Panel(RunPanel):
        def __init__(self):
            self.runtime = SimpleNamespace(
                molecules=SimpleNamespace(
                    shard_counts=lambda: {
                        "shards": 4, "records": 4000, "prepared": 4, "prepared_records": 3990,
                    },
                    get=lambda receptor_id: SimpleNamespace(name=f"R{receptor_id}"),
                ),
                docking=SimpleNamespace(run_shards=run_shards),
            )
            self.readiness_service = SimpleNamespace(
                receptors=lambda _scope, program: EntityReadiness(
                    total=3, ready=3, ready_ids=(7, 8, 9)
                )
            )

        def _resolve_receptor_scope(self, ids):
            return ("scope", tuple(ids or ()))

    result = _Panel()._submit_shard_docking(
        {"sharded": True, "program": "vina", "sel_rec": []},
        {
            "protocols": [{"program": "vina", "label": "Vina", "hash": "proto"}],
            "hit_threshold": 0.0,
            "hit_cap": 50,
            "hit_mode": "top_n",
            "offtarget_reference_id": 7,
            "executor_name": "compute",
            "skip_existing": True,
            "run_id": "run",
        },
    )

    assert list(result["job_ids"].values()) == ["job-1", "job-2"]
    assert submitted[0]["receptor_set"] == ("scope", (7,))
    assert submitted[1]["receptor_set"] == ("scope", (8, 9))
    assert submitted[1]["depends_on"] == ["job-1"]
    assert submitted[1]["selected_from_receptor_id"] == 7
    assert submitted[1]["selected_top_n"] == 50


def _scored(score, index):
    return {"score": score, "source_index": index, "metrics": {"selected_pose_path": ""}}


def test_ranked_mode_keeps_the_best_n_of_everything_and_writes_only_at_the_end():
    from amdockvs.screening.materialize import TopNGate

    gate = TopNGate(cap=3, threshold=-8.0)
    # Arrives worst-first, in three batches: order must not decide what survives.
    assert gate.take([_scored(-8.5, 0), _scored(-12.0, 1), _scored(-7.0, 2)]) == []
    assert gate.take([_scored(-9.0, 3), _scored(-11.0, 4)]) == []
    assert gate.full is False  # a ranked run only ends when the library does
    assert [row["source_index"] for row in gate.discarded()] == [0]  # -8.5, pushed out by -9.0
    assert [row["score"] for row in gate.drain()] == [-12.0, -11.0, -9.0]
    assert gate.drain() == []


def test_the_streaming_gate_still_writes_as_it_goes():
    from amdockvs.screening.materialize import HitGate

    gate = HitGate(cap=2, threshold=-8.0)
    assert len(gate.take([_scored(-9.0, 0), _scored(-7.0, 1), _scored(-8.5, 2)])) == 2
    assert gate.full and gate.drain() == [] and gate.discarded() == []


def test_docking_streams_each_ligand_into_a_result_shard_and_removes_loose_poses(tmp_path, monkeypatch):
    prepared = _pdbqt_shard(tmp_path / "prepared.mshard", count=2)
    output = tmp_path / "results" / "docking" / "screening"
    progress = []

    def fake_dock(payload):
        callback = payload["_pair_callback"]
        assert payload["_collect_rows"] is False
        for pair, score in zip(payload["pairs"], (-9.0, -7.0)):
            ligand_id = int(pair["ligand_id"])
            pdbqt = Path(payload["output_dir"]) / f"{ligand_id}.dock.pdbqt"
            sdf = Path(payload["output_dir"]) / f"{ligand_id}.dock.sdf"
            pdbqt.write_text("MODEL 1\nENDMDL\n")
            sdf.write_text(f"ligand-{ligand_id}\n$$$$\n")
            callback([{
                "receptor_molecule_id": 7,
                "ligand_molecule_id": ligand_id,
                "engine": "vina",
                "pose_rank": 1,
                "score": score,
                "score_type": "vina_score",
                "pose_path": str(sdf),
                "metrics": {
                    "selected_pose_path": str(sdf),
                    "selected_pose_pdbqt_path": str(pdbqt),
                },
            }])
        return []

    monkeypatch.setattr(shard_jobs, "run_docking_chunk", fake_dock)
    envelope = shard_jobs.DockShardsJobSpec._dock_one_shard(
        {
            "output_dir": str(output),
            "receptor_id": 7,
            "receptor_path": "receptor.pdbqt",
            "hit_threshold": -8.0,
            "hit_cap": 25,
            "hit_mode": "top_n",
            "run_id": "run-1",
            "protocol_metadata": {"hash": "proto-1"},
        },
        {
            "shard_id": 4,
            "shard_path": str(prepared),
            "source": "library.sdf",
            "shard_index": 0,
            "n_records": 2,
        },
        progress_cb=progress.append,
    )

    assert envelope["scored"] == 2
    assert envelope["hits"] == []  # ranked rows travel through Parquet, not the result envelope
    assert envelope["hits_found"] == 1
    assert progress == [1, 2]
    result_path = Path(envelope["result_shard"])
    assert result_path.name.endswith(".results.parquet")
    assert shard_jobs.result_metadata(result_path)["selection"] == {
        "mode": "top_n", "cap": 25, "threshold": -8.0,
    }
    records = pq.read_table(result_path).to_pylist()
    assert [record["score"] for record in records] == [-9.0, -7.0]
    assert records[0]["pose_text"].startswith("ligand-0")
    assert records[0]["pose_suffix"] == ".sdf"
    assert list(output.glob("*.dock.pdbqt")) == []
    assert list(output.glob("*.dock.sdf")) == []


def test_result_shard_cleanup_is_not_tied_to_sdf(tmp_path):
    pose = tmp_path / "ligand.ad4.pdbqt"
    pose.write_text("MODEL 1\nENDMDL\n")
    row = {
        "ligand_molecule_id": 3,
        "receptor_molecule_id": 7,
        "engine": "autodock4",
        "score": -8.5,
        "pose_path": str(pose),
        "metrics": {},
    }

    payload = shard_jobs.result_row(row, smiles="CCO")
    shard_jobs._discard_poses([row])

    assert payload["pose_text"] == "MODEL 1\nENDMDL\n"
    assert payload["pose_suffix"] == ".pdbqt"
    assert not pose.exists()


def test_parquet_fragment_is_atomic(tmp_path):
    import pytest

    path = tmp_path / "failed.results.parquet"
    with pytest.raises(RuntimeError):
        with shard_jobs.ResultFragmentWriter(path, metadata={}) as fragment:
            fragment.add({
                "ligand_id": 1, "receptor_id": 2, "score": -9.0,
                "score_type": "vina_score", "status": "done", "engine": "vina",
                "smiles": "CCO", "metrics_json": "{}", "pose_text": "pose",
                "pose_suffix": ".sdf",
            })
            raise RuntimeError("interrupted")
    assert not path.exists()
    assert not path.with_name(f"{path.name}.tmp").exists()


def test_select_hits_projects_scores_then_loads_only_selected_poses(tmp_path, monkeypatch):
    from sqlmodel import select

    from amdockvs.docking.results import dataset as result_dataset
    from amdockvs.screening.campaign import advance_shard_run
    from amdockvs.models import MoleculeRecord, ScreeningShard
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="columnar-ranking", folder=tmp_path)
    try:
        project_db = runtime.molsuite.project_db
        with project_db.get_session() as session:
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            source_shard = ScreeningShard(source="lib.smi", shard_index=0, path="lib.mshard", n_records=3)
            session.add(receptor)
            session.add(source_shard)
            session.commit()
            receptor_id, shard_id = int(receptor.id), int(source_shard.id)
        path = tmp_path / "scores.results.parquet"
        with shard_jobs.ResultFragmentWriter(
            path, metadata={"protocol": {"hash": "proto"}},
        ) as fragment:
            for ligand_id, score in enumerate((-7.0, -10.0, -9.0)):
                fragment.add({
                    "ligand_id": ligand_id, "receptor_id": receptor_id, "score": score,
                    "score_type": "vina_score", "status": "done", "engine": "vina",
                    "smiles": "CCO", "metrics_json": "{}", "pose_text": f"pose-{ligand_id}",
                    "pose_suffix": ".sdf",
                })
        advance_shard_run(
            project_db,
            run_id="run",
            shard_id=shard_id,
            receptor_id=receptor_id,
            engine="vina",
            protocol_hash="proto",
            scored=3,
            hits=2,
            result_path=str(path),
        )

        calls = []
        read_table = result_dataset.pq.read_table

        def observed_read(*args, **kwargs):
            calls.append(tuple(kwargs.get("columns") or ()))
            return read_table(*args, **kwargs)

        monkeypatch.setattr(result_dataset.pq, "read_table", observed_read)
        selected = shard_jobs.select_hits(
            project_db,
            project_root=tmp_path,
            run_id="run",
            protocol_hash="proto",
            filters={"score__lte": -8.0, "receptor_id": receptor_id},
            top_n=1,
        )

        assert [(row["source_index"], row["score"], row["_pose_text"]) for row in selected] == [
            (1, -10.0, "pose-1")
        ]
        assert "pose_text" not in calls[0]
        assert "pose_text" in calls[1]
        with project_db.get_session() as session:
            assert session.exec(select(MoleculeRecord)).all()
    finally:
        runtime.close_project()


def test_top_n_writes_nothing_until_the_campaign_closes(tmp_path):
    """The trade the ranked mode makes, end to end: empty database, then the best N at close."""
    from sqlmodel import select

    from amdockvs.models import DockingResultRecord, MoleculeRecord, ScreeningShard
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="campaign", folder=tmp_path)
    try:
        project_db = runtime.molsuite.project_db
        with project_db.get_session() as session:
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            source_shard = ScreeningShard(
                source="lib.smi", shard_index=0, path="lib.mshard", n_records=3
            )
            session.add(receptor)
            session.add(source_shard)
            session.commit()
            receptor_id = int(receptor.id)
            shard_id = int(source_shard.id)
        writer = ShardHitWriter(
            project_db=project_db,
            hit_cap=2,
            hit_threshold=-8.0,
            hit_mode="top_n",
            run_id="run-top",
            engine="vina",
            protocol_hash="proto-top",
        )
        result_shard = tmp_path / "ranked.results.parquet"
        with shard_jobs.ResultFragmentWriter(
            result_shard,
            metadata={
                "protocol": {"hash": "proto-top", "label": "Vina"},
                "selection": {"mode": "top_n", "cap": 2, "threshold": -8.0},
            },
        ) as fragment:
            for index, score in enumerate((-9.0, -12.0, -8.5)):
                fragment.add({
                    "ligand_id": index,
                    "receptor_id": receptor_id,
                    "score": score,
                    "score_type": "vina_score",
                    "status": "done",
                    "engine": "vina",
                    "smiles": "CCO",
                    "metrics_json": "{}",
                    "pose_text": f"pose-{index}\n",
                    "pose_suffix": ".sdf",
                })
        writer.handle("c0", [{
            "source": "lib.smi",
            "shard_index": 0,
            "shard_id": shard_id,
            "receptor_id": receptor_id,
            "scored": 3,
            "hits": [],
            "hits_found": 3,
            "result_shard": str(result_shard),
        }])
        with project_db.get_session() as session:
            assert session.exec(select(DockingResultRecord)).all() == []
        writer.close()
        with project_db.get_session() as session:
            names = [row.name for row in session.exec(
                select(MoleculeRecord).where(MoleculeRecord.is_ligand.is_(True))
            ).all()]
            results = session.exec(select(DockingResultRecord)).all()
        assert names == ["lib:1", "lib:0"]  # -12.0 then -9.0; -8.5 was pushed out
        assert sorted((tmp_path / row.pose_path).read_text() for row in results) == [
            "pose-0\n", "pose-1\n",
        ]
    finally:
        runtime.close_project()


def test_general_top_n_is_ranked_per_receptor_and_deduplicates_ligands(tmp_path):
    from sqlmodel import select

    from amdockvs.screening.campaign import advance_shard_run
    from amdockvs.models import DockingResultRecord, MoleculeRecord, ScreeningShard
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="per-receptor-ranking", folder=tmp_path)
    try:
        project_db = runtime.molsuite.project_db
        with project_db.get_session() as session:
            receptors = [
                MoleculeRecord(name=name, molecule_type="protein", is_receptor=True)
                for name in ("reference", "off-target")
            ]
            source_shard = ScreeningShard(
                source="library.smi", shard_index=0, path="library.mshard", n_records=2
            )
            session.add_all([*receptors, source_shard])
            session.commit()
            receptor_ids = [int(row.id) for row in receptors]
            shard_id = int(source_shard.id)

        for receptor_id, scores in zip(receptor_ids, ((-11.0, -8.0), (-10.0, -7.0))):
            result_path = tmp_path / f"r{receptor_id}.results.parquet"
            with shard_jobs.ResultFragmentWriter(
                result_path, metadata={"protocol": {"hash": "proto"}},
            ) as fragment:
                for ligand_id, score in enumerate(scores):
                    fragment.add({
                        "ligand_id": ligand_id, "receptor_id": receptor_id, "score": score,
                        "score_type": "vina_score", "status": "done", "engine": "vina",
                        "smiles": "CCO", "metrics_json": "{}", "pose_text": f"pose-{receptor_id}",
                        "pose_suffix": ".sdf",
                    })
            advance_shard_run(
                project_db, run_id="run", shard_id=shard_id, receptor_id=receptor_id,
                engine="vina", protocol_hash="proto", scored=2, hits=2,
                result_path=str(result_path),
            )

        ShardHitWriter(
            project_db=project_db, hit_cap=1, hit_threshold=0.0, hit_mode="top_n",
            run_id="run", engine="vina", protocol_hash="proto",
        ).close()

        with project_db.get_session() as session:
            ligands = session.exec(
                select(MoleculeRecord).where(MoleculeRecord.is_ligand.is_(True))
            ).all()
            results = session.exec(select(DockingResultRecord)).all()
        assert len(ligands) == 1  # ligand 0 won both receptor-specific rankings
        assert len(results) == 2
        assert {row.receptor_molecule_id for row in results} == set(receptor_ids)
        assert {row.ligand_molecule_id for row in results} == {ligands[0].id}
    finally:
        runtime.close_project()


def test_interrupted_top_n_is_recovered_from_completed_result_shards(tmp_path):
    from sqlmodel import select

    from amdockvs.models import (
        DockingResultRecord,
        MoleculeRecord,
        ScreeningShard,
        ScreeningShardRun,
        ScreeningTarget,
    )
    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.core.vocab import TargetState

    runtime = AMDockVSRuntime()
    runtime.create_project(name="recover-ranked", folder=tmp_path)
    try:
        project_db = runtime.molsuite.project_db
        with project_db.get_session() as session:
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            session.add(receptor)
            session.flush()
            source_shards = [
                ScreeningShard(source="library.smi", shard_index=index, path=f"input-{index}.mshard", n_records=2)
                for index in range(2)
            ]
            session.add_all(source_shards)
            session.flush()
            session.add(ScreeningTarget(
                run_id="interrupted", receptor_molecule_id=int(receptor.id), receptor_name="R",
                engine="vina", ligands_total=4, ligands_done=4, state=TargetState.CANCELED,
            ))
            session.commit()
            receptor_id = int(receptor.id)
            shard_ids = [int(row.id) for row in source_shards]

        protocol = {"program": "vina", "label": "Vina", "hash": "proto-recover"}
        for shard_index, scores in enumerate(((-7.0, -10.0), (-9.0, -8.0))):
            result_path = tmp_path / "results" / f"result-{shard_index}.mshard"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            with ShardWriter(
                result_path,
                dataset_id=None,
                shard_id=shard_index,
                base_id=shard_index * 2,
                kind=PayloadKind.GENERIC_BYTES,
                serializer=Serializer.RAW,
                slot_count=2,
                metadata={
                    "record_format": shard_jobs.RESULT_SHARD_FORMAT,
                    "run_id": "interrupted",
                    "protocol": protocol,
                    "selection": {"mode": "top_n", "cap": 2, "threshold": 0.0},
                },
            ) as writer:
                for offset, score in enumerate(scores):
                    ligand_id = shard_index * 2 + offset
                    payload = {
                        "ligand_id": ligand_id,
                        "receptor_id": receptor_id,
                        "engine": "vina",
                        "score": score,
                        "score_type": "vina_score",
                        "smiles": "CCO",
                        "metrics": {"run_id": "interrupted", "protocol": protocol},
                        "pose_text": f"ligand-{ligand_id}\n$$$$\n",
                        "pose_suffix": ".sdf",
                    }
                    writer.add(ligand_id, gzip.compress(json.dumps(payload).encode()))
            with project_db.get_session() as session:
                session.add(ScreeningShardRun(
                    run_id="interrupted",
                    shard_id=shard_ids[shard_index],
                    receptor_molecule_id=receptor_id,
                    engine="vina",
                    protocol_hash="proto-recover",
                    ligands_scored=2,
                    hits=2,
                    result_path=str(result_path.relative_to(tmp_path)),
                    state=TargetState.DONE,
                ))
                session.commit()

        candidates = shard_jobs.recoverable_ranked_runs(project_db, project_root=tmp_path)
        assert [(row["run_id"], row["top_n"], row["scored"]) for row in candidates] == [
            ("interrupted", 2, 4)
        ]

        recovered = shard_jobs.recover_ranked_hits(
            project_db,
            project_root=tmp_path,
            run_id="interrupted",
            protocol_hash="proto-recover",
            top_n=2,
        )
        assert recovered == {"shards": 2, "scored": 4, "selected": 2}
        with project_db.get_session() as session:
            results = session.exec(select(DockingResultRecord).order_by(DockingResultRecord.score)).all()
        assert [row.score for row in results] == [-10.0, -9.0]
        assert all((tmp_path / str(row.pose_path)).is_file() for row in results)
        assert shard_jobs.recoverable_ranked_runs(project_db, project_root=tmp_path) == []
    finally:
        runtime.close_project()


def test_canceled_job_marks_campaign_targets_canceled(tmp_path):
    from amdockvs.screening.campaign import list_targets, plan_targets
    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.core.vocab import TargetState

    runtime = AMDockVSRuntime()
    runtime.create_project(name="canceled-campaign", folder=tmp_path)
    try:
        project_db = runtime.molsuite.project_db
        plan_targets(
            project_db,
            run_id="run-canceled",
            receptors=[{"receptor_id": 1, "receptor_name": "4UWG"}],
            ligands_total=100,
            engine="vina",
        )
        writer = ShardHitWriter(
            project_db=project_db,
            hit_cap=10,
            hit_threshold=-8.0,
            run_id="run-canceled",
        )
        writer.on_job_terminal("canceled")
        writer.close()
        assert list_targets(project_db, run_id="run-canceled")[0]["state"] == TargetState.CANCELED
        assert list_targets(project_db, active_only=True) == []
    finally:
        runtime.close_project()


def test_dependency_cancellation_does_not_overwrite_reference_failure(tmp_path):
    from amdockvs.screening.campaign import list_targets, plan_targets, stop_run
    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.core.vocab import TargetState

    runtime = AMDockVSRuntime()
    runtime.create_project(name="failed-reference", folder=tmp_path)
    try:
        project_db = runtime.molsuite.project_db
        plan_targets(
            project_db, run_id="cascade", receptors=[{"receptor_id": 1}],
            ligands_total=10, engine="vina",
        )
        stop_run(project_db, run_id="cascade", state=TargetState.FAILED)
        stop_run(project_db, run_id="cascade", state=TargetState.CANCELED)
        assert list_targets(project_db, run_id="cascade")[0]["state"] == TargetState.FAILED
    finally:
        runtime.close_project()


def test_a_scheduled_receptor_is_visible_before_it_has_a_single_result(tmp_path):
    """What the Results view reads while a ranked campaign is running: the plan and its counters."""
    from amdockvs.screening.campaign import advance_target, finish_run, list_targets, plan_targets
    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.core.vocab import TargetState

    runtime = AMDockVSRuntime()
    runtime.create_project(name="campaign-plan", folder=tmp_path)
    try:
        project_db = runtime.molsuite.project_db
        plan_targets(
            project_db,
            run_id="run-1",
            receptors=[{"receptor_id": 1, "receptor_name": "1ATP"}, {"receptor_id": 2, "receptor_name": "4DFR"}],
            ligands_total=12000,
            engine="vina",
        )
        scheduled = list_targets(project_db, run_id="run-1")
        assert [row["receptor_name"] for row in scheduled] == ["1ATP", "4DFR"]
        assert scheduled[0] == {**scheduled[0], "ligands_done": 0, "pending": 12000, "hits": 0,
                                "state": TargetState.SCHEDULED}

        advance_target(project_db, run_id="run-1", receptor_id=1, scored=64, hits=2)
        advance_target(project_db, run_id="run-1", receptor_id=1, scored=64, hits=0)
        first = list_targets(project_db, run_id="run-1")[0]
        assert (first["ligands_done"], first["pending"], first["hits"]) == (128, 11872, 2)
        assert first["state"] == TargetState.RUNNING

        assert len(list_targets(project_db, active_only=True)) == 2
        finish_run(project_db, run_id="run-1")
        assert list_targets(project_db, active_only=True) == []
    finally:
        runtime.close_project()
