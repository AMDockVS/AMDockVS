"""Docking a sharded library: the slice the engine reads, and the countdown that ends a shard.

The two pieces with arithmetic in them. Everything else (Vina, the gate) is already covered.
"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from types import SimpleNamespace

from ms_flow.core.data.shard import Serializer, Shard, ShardWriter
from amdockvs.core.constants import ShardKind

from amdockvs.docking import shard_jobs, shard_tasks
from amdockvs.docking.shard_jobs import ShardHitWriter
from amdockvs.docking.preparation.shards import extract_ligands, smiles_from_pdbqt


def _batch(session, source: str) -> int:
    from amdockvs.models import ImportBatch

    batch = ImportBatch(source=source, quick_hash=source, kind="shards")
    session.add(batch)
    session.flush()
    return int(batch.id)


def _library_file(session, folder, source: str, *, n_records: int = 3, name: str = "library") -> int:
    """A library file on disk and its catalog row, in the caller's session: the id of its first shard."""
    from catalog_seed import write_library_file
    from ms_flow.core.data.catalog import shard_id
    from ms_flow.core.database.project_models import CatalogFile

    scope = _batch(session, source)
    path = write_library_file(Path(folder) / f"{name}.arrows", scope=scope, records=n_records)
    file = CatalogFile(path=str(path), n_shards=1, n_records=n_records, n_active=n_records, scopes=str(scope))
    session.add(file)
    session.flush()
    return shard_id(int(file.id), 0)


def _key(receptor_id, *, engine="vina", protocol_hash="", binding_site_id=0, input_key="", selection=""):
    from amdockvs.docking.results.dataset import state_key

    return state_key(engine=engine, protocol_hash=protocol_hash, receptor_id=receptor_id,
                     binding_site_id=binding_site_id, input_key=input_key, selection=selection)


def _docked(project_db, folder, *, key, shard_id, receptor_id, docking_run_id, ligand_ids=(0,),
            score=-9.0, narrowed_by="", smiles="C", input_key=""):
    """One shard docked for `key`: its result file written and recorded, as a worker's envelope
    would have it. Returns the key's folder (its containers)."""
    from amdockvs.docking.results.dataset import ResultFragmentWriter, result_row
    from amdockvs.screening.campaign import record_docked

    path = Path(folder) / "results" / key / "parts" / f"{shard_id}-{docking_run_id}.arrows"
    path.parent.mkdir(parents=True, exist_ok=True)
    with ResultFragmentWriter(path, metadata={"input_key": input_key}) as writer:
        for ligand_id in ligand_ids:
            writer.add(result_row(
                {"ligand_molecule_id": ligand_id, "receptor_id": receptor_id, "score": score,
                 "status": "done", "pose_rank": 1, "pose_text": "MODEL\n", "pose_suffix": ".pdbqt"},
                smiles=smiles, engine="vina", shard_id=shard_id, docking_run_id=docking_run_id,
            ))
    record_docked(project_db, key=key, shard_id=shard_id, result_path=str(path),
                  narrowed_by=narrowed_by, force=True)
    return path.parent.parent


def _plan(session, *, docking_run_id, receptor_id, keys, **fields):
    """The plan row a campaign writes, naming where its results are."""
    from amdockvs.models import ScreeningTarget

    row = ScreeningTarget(docking_run_id=docking_run_id, receptor_molecule_id=receptor_id,
                          state_keys=",".join(sorted(keys)), **fields)
    session.add(row)
    return row


def _pdbqt_shard(path: Path, count: int = 5) -> Path:
    with ShardWriter(
        path,
        dataset_id=None,
        shard_id=0,
        base_id=0,
        kind=ShardKind.PDBQT,
        serializer=Serializer.UTF8,
        slot_count=count,
        metadata={"source": "lib.smi"},
    ) as writer:
        for logical_id in range(count):
            writer.add(logical_id, f"REMARK SMILES CC{'O' * logical_id}\nATOM {logical_id}\n".encode())
    return path


def test_a_slice_comes_out_as_files_with_the_record_ids_and_their_smiles(tmp_path):
    shard = _pdbqt_shard(tmp_path / "shard_0.arrows")
    ligands = extract_ligands(shard, tmp_path / "staging", start=2, count=2)
    assert [ligand["id"] for ligand in ligands] == [2, 3]
    assert [Path(ligand["path"]).name for ligand in ligands] == ["2.pdbqt", "3.pdbqt"]
    assert ligands[0]["smiles"] == "CCOO"
    # Only the slice was written: a chunk never unpacks the whole shard.
    assert sorted(p.name for p in (tmp_path / "staging").iterdir()) == ["2.pdbqt", "3.pdbqt"]


def test_selected_record_ids_extract_only_the_reference_top_n(tmp_path):
    shard = _pdbqt_shard(tmp_path / "shard_0.arrows")
    ligands = extract_ligands(shard, tmp_path / "selected", record_ids=[1, 4])
    assert [ligand["id"] for ligand in ligands] == [1, 4]
    assert sorted(path.name for path in (tmp_path / "selected").iterdir()) == ["1.pdbqt", "4.pdbqt"]


def test_a_wrapped_smiles_remark_is_joined_and_the_idx_remark_ignored():
    text = "REMARK SMILES CCc1cc\nREMARK SMILES ccc1O\nREMARK SMILES IDX 1 1 2 2\nATOM\n"
    assert smiles_from_pdbqt(text) == "CCc1ccccc1O"


def test_prepared_shard_extracts_one_shard_of_a_file_holding_many(tmp_path):
    path = tmp_path / "merged.arrows"
    with ShardWriter(path, dataset_id=None, shard_id=0, base_id=0, kind=ShardKind.PDBQT,
                     serializer=Serializer.UTF8, slot_count=10) as writer:
        for lo in (0, 5):
            for logical_id in range(lo, lo + 5):
                writer.add(logical_id, f"REMARK SMILES C\nATOM {logical_id}\n".encode())
            writer.end_group(scope=1, base_id=lo, slot_count=5, origin=lo)
    ligands = extract_ligands(path, tmp_path / "staging", shard=1)
    assert [ligand["id"] for ligand in ligands] == [5, 6, 7, 8, 9]


class _Recorder:
    """Stands in for the ShardStore: materialize() is §4's job, already tested."""

    def __init__(self):
        self.states = []
        self.hits = []
        self.gate = SimpleNamespace(take=lambda rows: rows, discarded=lambda: [])
        self.payload = "light"

    def materialize(self, rows):
        self.hits.append(rows)


def _run(project_db, run_id, *, engine="vina", protocol_hash="") -> int:
    """The `docking_runs` row a job's feed would have created."""
    from amdockvs.docking.results.repository import docking_run_id

    return docking_run_id(project_db, run_id=run_id, engine=engine, run_kind="screening",
                          protocol={"hash": protocol_hash})


def test_a_shard_run_receives_one_campaign_scoped_update_per_receptor(tmp_path, monkeypatch):
    seen = _Recorder()
    monkeypatch.setattr(shard_jobs, "advance_target", lambda *a, **k: None)  # its own test
    monkeypatch.setattr(shard_jobs, "record_docked", lambda _db, **kw: seen.states.append(kw) or True)
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
    writer._docking_run_id = 7  # the run the feed created
    writer.selected_from_receptor_id = 2  # only an off-target stage streams hits

    for _ in range(3):
        writer.handle("c", [{
            "shard_id": 4,
            "state_key": "dock-abc",
            "source": "lib.smi",
            "shard_index": 0,
            "chunks_total": 3,
            "hits": [{"score": -9}],
        }])

    assert len(seen.states) == 3
    assert all(row["key"] == "dock-abc" and row["shard_id"] == 4 for row in seen.states)
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
            self.runtime = SimpleNamespace(is_queue=lambda _name: False,
                molecules=SimpleNamespace(shard_counts=lambda: {
                    "shards": 12, "records": 12000, "prepared": 12, "prepared_records": 11969,
                }),
                docking=SimpleNamespace(run_shards=lambda **kwargs: submitted.update(kwargs) or "job-1",
                                        check_protocol=lambda **_kwargs: None),
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
            self.runtime = SimpleNamespace(is_queue=lambda _name: False,
                molecules=SimpleNamespace(
                    shard_counts=lambda: {
                        "shards": 4, "records": 4000, "prepared": 4, "prepared_records": 3990,
                    },
                    get=lambda receptor_id: SimpleNamespace(name=f"R{receptor_id}"),
                ),
                docking=SimpleNamespace(run_shards=run_shards, check_protocol=lambda **_kwargs: None),
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
    return {"score": score, "source_index": index}


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
    prepared = _pdbqt_shard(tmp_path / "prepared.arrows", count=2)
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
                "pose_rank": 1,
                "score": score,
                "score_type": "vina_score",
                "pose_path": str(sdf),  # Vina's PDBQT is its sibling
            }])
        return []

    monkeypatch.setattr(shard_tasks, "run_docking_chunk", fake_dock)
    envelope = shard_tasks.dock_one_shard(
        {
            "output_dir": str(output),
            "receptor_id": 7,
            "receptor_path": "receptor.pdbqt",
            "hit_threshold": -8.0,
            "hit_cap": 25,
            "run_id": "run-1",
            "protocol_metadata": {"hash": "proto-1"},
            "input_key": "inputs-1",
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
    assert result_path.name.endswith(".arrows") and result_path.parent.name == "parts"
    assert shard_jobs.result_metadata(result_path)["selection"] == {
        "mode": "top_n", "cap": 25, "threshold": -8.0,
    }
    # What the fragment was docked against: its hits' results carry it (skip_existing compares).
    assert shard_jobs.result_metadata(result_path)["input_key"] == "inputs-1"
    from ms_flow.core.data.shard import read_rows

    from amdockvs.docking.results.dataset import payload_part

    assert read_rows(result_path)["score"].to_pylist() == [-9.0, -7.0]
    records = read_rows(payload_part(result_path)).to_pylist()  # the poses: beside the summary
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
        "score": -8.5,
        "pose_path": str(pose),
    }

    payload = shard_tasks.result_row(row, smiles="CCO")
    shard_tasks._discard_poses([row])

    assert payload["pose_text"] == "MODEL 1\nENDMDL\n"
    assert payload["pose_suffix"] == ".pdbqt"
    assert not pose.exists()


def test_parquet_fragment_is_atomic(tmp_path):
    import pytest

    path = tmp_path / "failed.arrows"
    with pytest.raises(RuntimeError):
        with shard_tasks.ResultFragmentWriter(path, metadata={}) as fragment:
            fragment.add({
                "ligand_id": 1, "receptor_id": 2, "score": -9.0,
                "score_type": "vina_score", "status": "done", "engine": "vina",
                "smiles": "CCO", "metrics_json": "{}", "pose_text": "pose",
                "pose_suffix": ".sdf",
            })
            raise RuntimeError("interrupted")
    assert not path.exists()
    assert not path.with_name(f"{path.name}.tmp").exists()


def test_select_hits_projects_scores_then_loads_only_selected_poses(tmp_path):
    from sqlmodel import select

    from amdockvs.screening.campaign import record_docked
    from amdockvs.models import MoleculeRecord
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="columnar-ranking", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        with project_db.get_session() as session:
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            shard_id = _library_file(session, tmp_path, "lib.smi", n_records=3)
            session.add(receptor)
            session.commit()
            receptor_id = int(receptor.id)
        path = tmp_path / "results" / "parts" / "scores.arrows"
        with shard_tasks.ResultFragmentWriter(
            path, metadata={"protocol": {"hash": "proto"}},
        ) as fragment:
            for ligand_id, score in enumerate((-7.0, -10.0, -9.0)):
                fragment.add({
                    "ligand_id": ligand_id, "shard_id": shard_id, "receptor_id": receptor_id,
                    "score": score, "score_type": "vina_score", "status": "done", "engine": "vina",
                    "smiles": "CCO", "metrics_json": "{}", "pose_text": f"pose-{ligand_id}",
                    "pose_suffix": ".sdf",
                })
        key = _key(receptor_id, protocol_hash="proto")
        record_docked(project_db, key=key, shard_id=shard_id, result_path=str(path))

        selected = shard_jobs.select_hits(
            project_db,
            keys=[key],
            filters={"score__lte": -8.0, "receptor_id": receptor_id},
            top_n=1,
        )

        assert [(row["source_index"], row["score"], row["_pose_text"]) for row in selected] == [
            (1, -10.0, "pose-1")
        ]
        with project_db.get_session() as session:
            assert session.exec(select(MoleculeRecord)).all()
    finally:
        runtime.close_project()


def test_top_n_writes_nothing_until_the_campaign_closes(tmp_path):
    """The trade the ranked mode makes, end to end: empty database, then the best N at close."""
    from sqlmodel import select

    from amdockvs.models import DockingResultRecord, MoleculeRecord
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="campaign", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        with project_db.get_session() as session:
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            shard_id = _library_file(session, tmp_path, "lib.smi", n_records=3)
            session.add(receptor)
            session.commit()
            receptor_id = int(receptor.id)
        writer = ShardHitWriter(
            project_db=project_db,
            hit_cap=2,
            hit_threshold=-8.0,
            run_id="run-top",
            engine="vina",
            protocol_hash="proto-top",
        )
        run = _run(project_db, "run-top", protocol_hash="proto-top")  # the feed's, before any chunk
        key = _key(receptor_id, protocol_hash="proto-top")
        with project_db.get_session() as session:
            _plan(session, docking_run_id=run, receptor_id=receptor_id, keys=[key])
            session.commit()
        result_shard = tmp_path / "results" / "parts" / "ranked.arrows"
        with shard_tasks.ResultFragmentWriter(
            result_shard,
            metadata={
                "protocol": {"hash": "proto-top", "label": "Vina"},
                "selection": {"mode": "top_n", "cap": 2, "threshold": -8.0},
            },
        ) as fragment:
            for index, score in enumerate((-9.0, -12.0, -8.5)):
                fragment.add({
                    "ligand_id": index,
                    "shard_id": shard_id,
                    "docking_run_id": run,
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
            "state_key": key,
            "docking_run_id": run,
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
        from amdockvs.core.payloads import read_structure

        assert sorted(read_structure(project_db, row.pose_payload_id) for row in results) == [
            ("pose-0\n", "sdf"), ("pose-1\n", "sdf"),
        ]
    finally:
        runtime.close_project()


def test_general_top_n_is_ranked_per_receptor_and_deduplicates_ligands(tmp_path):
    from sqlmodel import select

    from amdockvs.screening.campaign import record_docked
    from amdockvs.models import DockingResultRecord, MoleculeRecord
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="per-receptor-ranking", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        with project_db.get_session() as session:
            receptors = [
                MoleculeRecord(name=name, molecule_type="protein", is_receptor=True)
                for name in ("reference", "off-target")
            ]
            shard_id = _library_file(session, tmp_path, "library.smi", n_records=2)
            session.add_all(receptors)
            session.commit()
            receptor_ids = [int(row.id) for row in receptors]

        run = _run(project_db, "run", protocol_hash="proto")
        for receptor_id, scores in zip(receptor_ids, ((-11.0, -8.0), (-10.0, -7.0))):
            key = _key(receptor_id, protocol_hash="proto")
            with project_db.get_session() as session:
                _plan(session, docking_run_id=run, receptor_id=receptor_id, keys=[key])
                session.commit()
            result_path = tmp_path / "results" / "parts" / f"r{receptor_id}.arrows"
            with shard_tasks.ResultFragmentWriter(
                result_path, metadata={"protocol": {"hash": "proto"}},
            ) as fragment:
                for ligand_id, score in enumerate(scores):
                    fragment.add({
                        "ligand_id": ligand_id, "shard_id": shard_id, "docking_run_id": run,
                        "receptor_id": receptor_id, "score": score,
                        "score_type": "vina_score", "status": "done", "engine": "vina",
                        "smiles": "CCO", "metrics_json": "{}", "pose_text": f"pose-{receptor_id}",
                        "pose_suffix": ".sdf",
                    })
            record_docked(project_db, key=key, shard_id=shard_id, result_path=str(result_path))

        ShardHitWriter(
            project_db=project_db, hit_cap=1, hit_threshold=0.0,
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
        ScreeningTarget,
    )
    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.core.vocab import TargetState

    runtime = AMDockVSRuntime()
    runtime.create_project(name="recover-ranked", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        run = _run(project_db, "interrupted", protocol_hash="proto-recover")
        with project_db.get_session() as session:
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            session.add(receptor)
            session.flush()
            shard_ids = [_library_file(session, tmp_path, "library.smi", n_records=2, name=f"input-{index}")
                         for index in range(2)]
            receptor_id = int(receptor.id)
            key = _key(receptor_id, protocol_hash="proto-recover")
            session.add(ScreeningTarget(
                docking_run_id=run, receptor_molecule_id=receptor_id, receptor_name="R",
                engine="vina", ligands_total=4, ligands_done=4, state=TargetState.CANCELED,
                state_keys=key,
            ))
            session.commit()

        protocol = {"program": "vina", "label": "Vina", "hash": "proto-recover"}
        for shard_index, scores in enumerate(((-7.0, -10.0), (-9.0, -8.0))):
            result_path = tmp_path / "results" / "parts" / f"result-{shard_index}.arrows"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            with shard_tasks.ResultFragmentWriter(
                result_path,
                metadata={
                    "run_id": "interrupted",
                    "input_key": "inputs-recover",
                    "protocol": protocol,
                    "selection": {"mode": "top_n", "cap": 2, "threshold": 0.0},
                },
            ) as writer:
                for offset, score in enumerate(scores):
                    ligand_id = shard_index * 2 + offset
                    writer.add({
                        "ligand_id": ligand_id,
                        "shard_id": shard_ids[shard_index],
                        "docking_run_id": run,
                        "receptor_id": receptor_id,
                        "score": score,
                        "score_type": "vina_score",
                        "status": "done",
                        "engine": "vina",
                        "smiles": "CCO",
                        "metrics_json": "{}",
                        "pose_text": f"ligand-{ligand_id}\n$$$$\n",
                        "pose_suffix": ".sdf",
                    })
            from amdockvs.screening.campaign import record_docked

            record_docked(project_db, key=key, shard_id=shard_ids[shard_index],
                          result_path=str(result_path))

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
        assert {row.input_key for row in results} == {"inputs-recover"}
        from amdockvs.core.payloads import read_structure

        assert [read_structure(project_db, row.pose_payload_id)[0] for row in results] == [
            "ligand-1\n$$$$\n", "ligand-2\n$$$$\n",
        ]
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
        project_db = runtime.workspace.project_db
        plan_targets(
            project_db,
            docking_run_id=_run(project_db, "run-canceled"),
            receptors=[{"receptor_id": 1, "receptor_name": "4UWG"}],
            ligands_total=100,
            engine="vina",
        )
        writer = ShardHitWriter(
            project_db=project_db,
            hit_cap=10,
            hit_threshold=-8.0,
            run_id="run-canceled",
            engine="vina",
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
        project_db = runtime.workspace.project_db
        run = _run(project_db, "cascade")
        plan_targets(
            project_db, docking_run_id=run, receptors=[{"receptor_id": 1}],
            ligands_total=10, engine="vina",
        )
        stop_run(project_db, docking_run_id=run, state=TargetState.FAILED)
        stop_run(project_db, docking_run_id=run, state=TargetState.CANCELED)
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
        project_db = runtime.workspace.project_db
        run = _run(project_db, "run-1")
        plan_targets(
            project_db,
            docking_run_id=run,
            receptors=[{"receptor_id": 1, "receptor_name": "1ATP"}, {"receptor_id": 2, "receptor_name": "4DFR"}],
            ligands_total=12000,
            engine="vina",
        )
        scheduled = list_targets(project_db, run_id="run-1")
        assert [row["receptor_name"] for row in scheduled] == ["1ATP", "4DFR"]
        assert scheduled[0] == {**scheduled[0], "ligands_done": 0, "pending": 12000, "hits": 0,
                                "state": TargetState.SCHEDULED}

        advance_target(project_db, docking_run_id=run, receptor_id=1, scored=64, hits=2)
        advance_target(project_db, docking_run_id=run, receptor_id=1, scored=64, hits=0)
        first = list_targets(project_db, run_id="run-1")[0]
        assert (first["ligands_done"], first["pending"], first["hits"]) == (128, 11872, 2)
        assert first["state"] == TargetState.RUNNING

        assert len(list_targets(project_db, active_only=True)) == 2
        finish_run(project_db, docking_run_id=run)
        assert list_targets(project_db, active_only=True) == []
    finally:
        runtime.close_project()


def test_both_stores_dock_the_table_then_the_shards():
    """Both "Ligands from" boxes: the rows' docking goes first, the campaign waits on it."""
    import pytest

    pytest.importorskip("PySide6")
    from amdockvs.docking.readiness import EntityReadiness
    from amdockvs.ui.tools.docking.run_panel import RunPanel

    shards = []

    class _Panel(RunPanel):
        def __init__(self):
            self.runtime = SimpleNamespace(is_queue=lambda _name: False,
                molecules=SimpleNamespace(
                    shard_counts=lambda: {"shards": 2, "records": 20, "prepared": 2, "prepared_records": 20},
                    get=lambda receptor_id: SimpleNamespace(name=f"R{receptor_id}"),
                ),
                docking=SimpleNamespace(run_shards=lambda **kw: shards.append(kw) or "shard-job",
                                        check_protocol=lambda **_kw: None),
            )
            rows = {"ligands": {"ready": 3, "total": 3}, "receptors": {"ready": 1, "total": 1},
                    "ready_receptor_ids": [7], "ready": True, "pairs": {"total": 3}}
            self.readiness_service = SimpleNamespace(
                evaluate=lambda **_kw: SimpleNamespace(as_mapping=lambda: dict(rows)),
                receptors=lambda _scope, program: EntityReadiness(total=1, ready=1, ready_ids=(7,)),
            )
            self.submission_service = SimpleNamespace(submit=lambda request, ready_receptor_ids: SimpleNamespace(
                job_ids={"1:Vina:p": "row-job"}, interaction_job_id="", receptor_ids=(7,), complex_ids=()))

        def _resolve_ligand_scope(self, ids, run_kind="docking"):
            return ("ligands", tuple(ids or ()))

        def _resolve_receptor_scope(self, ids):
            return ("scope", tuple(ids or ()))

    panel = _Panel()
    inp = {"sharded": False, "sources": ("rows", "shards"), "program": "vina", "sel_lig": [], "sel_rec": []}
    counts = panel._compute_requirement_counts(inp)
    assert counts["ready"] is True and counts["shards"]["prepared_records"] == 20
    result = panel._submit_docking(inp, {
        "protocols": [{"program": "vina", "label": "Vina", "hash": "p"}], "sel_lig": [],
        "batch_size": 8, "hit_threshold": 0.0, "hit_cap": 50, "executor_name": "local_cpu",
        "skip_existing": True, "run_id": "run",
    })
    assert result["job_ids"] == {"1:Vina:p": "row-job", "shards:1:Vina:p": "shard-job"}
    assert "depends_on" not in shards[0]  # side by side; run_shards gives them the lower priority


def _prepared_shard_scope(monkeypatch, *, receptor_id: int, shard_id: int, n_records: int = 3,
                          input_key: str = ""):
    """One prepared shard against one prepared receptor with a box, without preparing either."""
    monkeypatch.setattr(shard_jobs, "receptor_targets", lambda *a, **k: [{
        "receptor_id": receptor_id, "receptor_name": "R", "receptor_path": "receptor.pdbqt",
        "flex_receptor_path": "", "box_center": [0.0, 0.0, 0.0], "box_size": [20.0, 20.0, 20.0],
        "spacing": 0.375, "input_key": input_key,
    }])
    monkeypatch.setattr(shard_jobs, "iter_prepared_shards", lambda *a, **k: [{
        "id": shard_id, "prepared_path": "", "path": "library.parquet", "import_batch_id": 1,
        "library": "library", "shard_index": 0, "n_records": n_records, "sha256": "",
    }])


def _shard_params(tmp_path, *, run_id: str, skip_existing: bool):
    return shard_jobs.DockShardsJobParams(
        output_dir=str(tmp_path / "docking"), engine="vina", preparation_engine="ad4",
        hit_threshold=0.0, hit_cap=10, run_id=run_id, skip_existing=skip_existing,
        protocol_metadata={"program": "vina", "label": "Vina", "hash": "proto-skip"},
    ).model_dump(mode="python")


def test_an_unchanged_campaign_submitted_again_docks_the_library_a_second_time(tmp_path, monkeypatch):
    """RR-01: a campaign's run id is new on every submission, so a skip keyed by it skipped
    nothing and six compounds came back as twelve rows."""
    from sqlmodel import select

    from amdockvs.models import DockingRun, MoleculeRecord
    from amdockvs.core.vocab import TargetState
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="repeat-campaign", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        with project_db.get_session() as session:
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            shard_id = _library_file(session, tmp_path, "library.smi", n_records=3)
            session.add(receptor)
            session.commit()
            receptor_id = int(receptor.id)
        _prepared_shard_scope(monkeypatch, receptor_id=receptor_id, shard_id=shard_id)

        first = list(shard_jobs.DockShardsJobSpec.build_chunks(
            _shard_params(tmp_path, run_id="run-first", skip_existing=True), {"project_db": project_db}))
        assert [chunk["shards"][0]["shard_id"] for chunk in first] == [shard_id]
        _docked(project_db, tmp_path, key=first[0]["state_key"], shard_id=shard_id,  # what that run records
                receptor_id=receptor_id, docking_run_id=first[0]["docking_run_id"])

        params = _shard_params(tmp_path, run_id="run-second", skip_existing=True)
        assert shard_jobs.DockShardsJobSpec.count_chunks(params, {"project_db": project_db}) == 1
        again = list(shard_jobs.DockShardsJobSpec.build_chunks(params, {"project_db": project_db}))
        assert [chunk["shards"] for chunk in again] == [[]]  # the no-op sentinel: nothing to dock
        with project_db.get_session() as session:  # and no empty run of its own in Results
            assert [row.run_id for row in session.exec(select(DockingRun)).all()] == ["run-first"]

        forced = list(shard_jobs.DockShardsJobSpec.build_chunks(
            _shard_params(tmp_path, run_id="run-forced", skip_existing=False), {"project_db": project_db}))
        assert [chunk["shards"][0]["shard_id"] for chunk in forced] == [shard_id]
    finally:
        runtime.close_project()


def test_a_forced_campaign_replaces_what_the_pairs_it_docked_again_had(tmp_path, monkeypatch):
    """The other half of RR-01: forced, the new run's results replace the older run's instead
    of accumulating beside them, and the rows and plan row that held them go too."""
    from sqlmodel import select

    from amdockvs.docking.rerun import supersede_shard_runs
    from amdockvs.docking.results.dataset import docked_pairs, run_counts
    from amdockvs.models import DockingResultRecord, MoleculeRecord, ScreeningTarget
    from amdockvs.core.vocab import TargetState
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="forced-campaign", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        with project_db.get_session() as session:
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            ligands = [MoleculeRecord(name=name, molecule_type="small_molecule", is_ligand=True)
                       for name in ("old-hit", "new-hit")]
            shard_id = _library_file(session, tmp_path, "library.smi", n_records=3)
            session.add_all([receptor, *ligands])
            session.commit()
            receptor_id = int(receptor.id)
            old_ligand, new_ligand = (int(row.id) for row in ligands)

        old_run = _run(project_db, "run-old", protocol_hash="proto-forced")
        new_run = _run(project_db, "run-new", protocol_hash="proto-forced")
        key = _key(receptor_id, protocol_hash="proto-forced")
        _docked(project_db, tmp_path, key=key, shard_id=shard_id, receptor_id=receptor_id,
                           docking_run_id=old_run, score=-9.0)
        _docked(project_db, tmp_path, key=key, shard_id=shard_id, receptor_id=receptor_id,
                docking_run_id=new_run, score=-11.0)
        with project_db.get_session() as session:
            session.add(ScreeningTarget(
                docking_run_id=old_run, receptor_molecule_id=receptor_id, receptor_name="R",
                engine="vina", ligands_total=3, ligands_done=3, state=TargetState.DONE,
                state_keys=key,
            ))
            for run, ligand, score in ((old_run, old_ligand, -9.0), (new_run, new_ligand, -11.0)):
                session.add(DockingResultRecord(
                    docking_run_id=run, receptor_molecule_id=receptor_id,
                    ligand_molecule_id=ligand, pose_rank=1, score=score, engine="vina",
                ))
            session.commit()

        assert supersede_shard_runs(project_db, docking_run_id=new_run) == 1
        with project_db.get_session() as session:
            results = session.exec(select(DockingResultRecord)).all()
            targets = session.exec(select(ScreeningTarget)).all()
        # The older rows stay in the containers, hidden: a read sees the new run's alone.
        assert docked_pairs(project_db, [key]) == {(old_run, shard_id), (new_run, shard_id)}
        assert run_counts(project_db, [key], old_run) == (0, 0)
        assert run_counts(project_db, [key], new_run) == (1, 1)
        assert [(int(row.docking_run_id), int(row.ligand_molecule_id)) for row in results] == [
            (new_run, new_ligand)
        ]
        assert targets == []
    finally:
        runtime.close_project()


def test_a_forced_off_target_stage_keeps_the_ligands_it_never_docked_again(tmp_path):
    """RR-01, the subset half: an off-target stage docks one reference receptor's Top-N, not the
    shard. Its key stands for that selection alone, so it must not retire what an older run
    holds for the same pair under the library key — nor the file and the results of the
    ligands outside the Top-N behind it."""
    from sqlmodel import select

    from amdockvs.docking.results.dataset import docked_pairs, off_target_selection
    from amdockvs.models import DockingResultRecord, MoleculeRecord, ScreeningTarget
    from amdockvs.core.vocab import TargetState
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="off-target-subset", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        with project_db.get_session() as session:
            receptors = [MoleculeRecord(name=name, molecule_type="protein", is_receptor=True)
                         for name in ("reference", "off-target")]
            ligands = [MoleculeRecord(name=name, molecule_type="small_molecule", is_ligand=True)
                       for name in ("picked", "left-out")]
            shard_id = _library_file(session, tmp_path, "library.smi", n_records=2)
            session.add_all([*receptors, *ligands])
            session.commit()
            reference_id, off_target_id = (int(row.id) for row in receptors)
            picked, left_out = (int(row.id) for row in ligands)

        old_run = _run(project_db, "run-library", protocol_hash="proto-off")
        library_key = _key(off_target_id, protocol_hash="proto-off")
        whole = _docked(  # the whole library against the off-target receptor
            project_db, tmp_path, key=library_key, shard_id=shard_id, receptor_id=off_target_id,
            docking_run_id=old_run, ligand_ids=(0, 1),
        )
        with project_db.get_session() as session:
            session.add(ScreeningTarget(
                docking_run_id=old_run, receptor_molecule_id=off_target_id, receptor_name="off-target",
                engine="vina", ligands_total=2, ligands_done=2, state=TargetState.DONE,
                state_keys=library_key,
            ))
            for ligand, score in ((picked, -9.0), (left_out, -8.0)):
                session.add(DockingResultRecord(
                    docking_run_id=old_run, receptor_molecule_id=off_target_id,
                    ligand_molecule_id=ligand, pose_rank=1, score=score, engine="vina",
                ))
            session.commit()

        # Forced, the off-target stage docks only the reference receptor's Top-N of that shard.
        new_run = _run(project_db, "run-off", protocol_hash="proto-off")
        selection = off_target_selection(reference_id, "run-off")
        off_key = _key(off_target_id, protocol_hash="proto-off", selection=selection)
        with project_db.get_session() as session:
            _plan(session, docking_run_id=new_run, receptor_id=off_target_id, keys=[off_key],
                  receptor_name="off-target", engine="vina", ligands_total=1)
            session.commit()
        subset = _fragment(tmp_path / "results" / "parts" / "off.arrows", receptor_id=off_target_id, records={0: -10.0},
                           run_id="run-off", protocol={"hash": "proto-off"},
                           shard_id=shard_id, docking_run_id=new_run)
        writer = ShardHitWriter(
            project_db=project_db, hit_cap=10, hit_threshold=0.0, run_id="run-off",
            engine="vina", protocol_hash="proto-off",
            selected_from_receptor_id=reference_id, skip_existing=False,
        )
        writer.handle("chunk", [{
            "shard_id": shard_id, "receptor_id": off_target_id, "scored": 1, "hits": [],
            "hits_found": 1, "result_shard": str(subset), "state_key": off_key,
            "docking_run_id": new_run,
        }])
        with project_db.get_session() as session:  # what materializing that one hit stores
            session.add(DockingResultRecord(
                docking_run_id=new_run, receptor_molecule_id=off_target_id,
                ligand_molecule_id=picked, pose_rank=1, score=-10.0, engine="vina",
            ))
            session.commit()
        writer.close()

        with project_db.get_session() as session:
            results = sorted((int(row.docking_run_id), int(row.ligand_molecule_id)) for row
                             in session.exec(select(DockingResultRecord)).all())
            targets = [int(row.docking_run_id) for row in session.exec(select(ScreeningTarget)).all()]
        # The whole-shard rows stay under their own key: the subset never covered them.
        assert docked_pairs(project_db, [library_key]) == {(old_run, shard_id)}
        assert docked_pairs(project_db, [off_key]) == {(new_run, shard_id)}
        assert list(whole.glob("summary_*.arrows"))
        assert targets == [old_run, new_run]
        # Only the pair it docked again was replaced; the ligand outside the Top-N keeps its row.
        assert results == sorted([(old_run, left_out), (new_run, picked)])
    finally:
        runtime.close_project()


def _fragment(path: Path, *, receptor_id: int, records, run_id: str, protocol: dict,
              selection: dict | None = None, shard_id: int = 0, docking_run_id: int = 0) -> Path:
    """A real result fragment holding `records` as `{record id: score}`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with shard_tasks.ResultFragmentWriter(path, metadata={
        "run_id": run_id, "protocol": protocol,
        "selection": selection or {"mode": "top_n", "cap": 10, "threshold": 0.0},
    }, origin=int(shard_id)) as writer:
        for record_id, score in dict(records).items():
            writer.add({
                "ligand_id": int(record_id), "shard_id": int(shard_id),
                "docking_run_id": int(docking_run_id),
                "receptor_id": int(receptor_id), "score": float(score),
                "score_type": "vina_score", "status": "done", "engine": "vina", "smiles": "CCO",
                "metrics_json": "{}", "pose_text": f"ligand-{record_id}\n$$$$\n", "pose_suffix": ".sdf",
            })
    return path


def test_an_off_target_subset_does_not_satisfy_a_full_library_skip(tmp_path, monkeypatch):
    """RR-01: `completed_protocol_targets` counted every DONE receipt, so one ligand docked by an
    off-target stage read as the whole shard and a later full screen skipped the rest of it."""
    from amdockvs.models import MoleculeRecord
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="subset-skip", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        with project_db.get_session() as session:
            receptors = [MoleculeRecord(name=name, molecule_type="protein", is_receptor=True)
                         for name in ("reference", "off-target")]
            shard_id = _library_file(session, tmp_path, "library.smi", n_records=100)
            session.add_all(receptors)
            session.commit()
            reference_id, off_target_id = (int(row.id) for row in receptors)
        _prepared_shard_scope(monkeypatch, receptor_id=off_target_id, shard_id=shard_id, n_records=100)

        # One selected ligand of that shard, docked against the off-target receptor.
        _run(project_db, "run-off", protocol_hash="proto-skip")
        ShardHitWriter(
            project_db=project_db, hit_cap=10, hit_threshold=0.0, run_id="run-off",
            engine="vina", protocol_hash="proto-skip", selected_from_receptor_id=reference_id,
        ).handle("chunk", [{
            "shard_id": shard_id, "receptor_id": off_target_id, "scored": 1, "hits": [],
            "hits_found": 1, "result_shard": "results/off.parquet",
            "selection": f"top:{reference_id}",
        }])

        # A full library screen against the same receptor still has the other 99 records to dock.
        params = _shard_params(tmp_path, run_id="run-library", skip_existing=True)
        assert shard_jobs.DockShardsJobSpec.count_chunks(params, {"project_db": project_db}) == 1
        chunks = list(shard_jobs.DockShardsJobSpec.build_chunks(params, {"project_db": project_db}))
        assert [chunk["shards"][0]["shard_id"] for chunk in chunks] == [shard_id]
        assert chunks[0]["shards"][0]["n_records"] == 100  # the whole shard, not the one record
    finally:
        runtime.close_project()


def test_a_receptor_prepared_again_is_docked_again_despite_skip_existing(tmp_path, monkeypatch):
    """The cross-run skip matches receipts by protocol; without the inputs they were docked
    against, a receptor prepared again (or given a new box) would keep its old shard results."""
    from amdockvs.models import MoleculeRecord
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="input-skip", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        with project_db.get_session() as session:
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            shard_id = _library_file(session, tmp_path, "library.smi", n_records=3)
            session.add(receptor)
            session.commit()
            receptor_id = int(receptor.id)

        old_run = _run(project_db, "run-old", protocol_hash="proto-skip")
        _docked(project_db, tmp_path, key=_key(receptor_id, protocol_hash="proto-skip", input_key="prep-1"),
                shard_id=shard_id, receptor_id=receptor_id, docking_run_id=old_run, input_key="prep-1")
        config = {"project_db": project_db}
        params = _shard_params(tmp_path, run_id="run-new", skip_existing=True)

        def shards_to_dock(input_key):
            _prepared_shard_scope(monkeypatch, receptor_id=receptor_id, shard_id=shard_id, input_key=input_key)
            return [chunk["shards"] for chunk in shard_jobs.DockShardsJobSpec.build_chunks(params, config)]

        assert shards_to_dock("prep-1") == [[]]  # same inputs: the no-op sentinel
        assert [s[0]["shard_id"] for s in shards_to_dock("prep-2")] == [shard_id]  # prepared again
    finally:
        runtime.close_project()


def test_a_skipped_reference_still_supplies_its_ranking_to_a_new_off_target(tmp_path, monkeypatch):
    """RR-01: the reference stage skips the shards an older run of the protocol docked, so it
    writes no receipt under the new submission. The off-target stage that depends on it has to
    rank what the library holds, or it selects nothing and the new receptor is never docked."""
    from amdockvs.models import MoleculeRecord
    from amdockvs.core.vocab import TargetState
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="skipped-reference", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        root = Path(project_db.db_path).parent
        with project_db.get_session() as session:
            receptors = [MoleculeRecord(name=name, molecule_type="protein", is_receptor=True)
                         for name in ("reference", "off-target")]
            shard_id = _library_file(session, tmp_path, "library.smi", n_records=3)
            session.add_all(receptors)
            session.commit()
            reference_id, off_target_id = (int(row.id) for row in receptors)

        # An older submission docked the library against the reference receptor and ranked it.
        old_run = _run(project_db, "run-old", protocol_hash="proto-skip")
        reference_key = _key(reference_id, protocol_hash="proto-skip")
        path = _fragment(root / "results" / "parts" / "reference.arrows", receptor_id=reference_id,
                         records={0: -11.0, 1: -6.0}, run_id="run-old",
                         protocol={"program": "vina", "label": "Vina", "hash": "proto-skip"},
                         shard_id=shard_id, docking_run_id=old_run)
        from amdockvs.screening.campaign import record_docked

        record_docked(project_db, key=reference_key, shard_id=shard_id, result_path=str(path))

        # The new submission's reference stage has nothing left to dock.
        _prepared_shard_scope(monkeypatch, receptor_id=reference_id, shard_id=shard_id)
        reference = list(shard_jobs.DockShardsJobSpec.build_chunks(
            _shard_params(tmp_path, run_id="run-new", skip_existing=True), {"project_db": project_db}))
        assert [chunk["shards"] for chunk in reference] == [[]]  # the no-op sentinel

        # Its dependent off-target stage must still receive the reference's Top-N. Both
        # receptors are prepared: the stage ranks the reference's key and docks the other.
        _prepared_shard_scope(monkeypatch, receptor_id=off_target_id, shard_id=shard_id)
        off_target_only = shard_jobs.receptor_targets(None, None, None)
        monkeypatch.setattr(shard_jobs, "receptor_targets", lambda *a, **k: [
            {**off_target_only[0], "receptor_id": reference_id, "receptor_name": "reference"},
            *off_target_only,
        ])
        params = {
            **_shard_params(tmp_path, run_id="run-new", skip_existing=True),
            "selected_from_receptor_id": reference_id, "selected_top_n": 1,
        }
        off = list(shard_jobs.DockShardsJobSpec.build_chunks(params, {"project_db": project_db}))
        assert [chunk["shards"][0]["shard_id"] for chunk in off] == [shard_id]
        assert off[0]["shards"][0]["record_ids"] == [0]  # the qualifying ligand, not the other
        assert off[0]["receptor_id"] == off_target_id
    finally:
        runtime.close_project()


def test_a_partial_replacement_retires_the_hits_of_the_shard_it_replaced(tmp_path):
    """RR-01: an old run with two shards against one receptor is not 'emptied' when only one of
    them is redocked, so its hits used to go only where the new run promoted the same ligand. A
    hit of the replaced shard that no longer qualifies stayed behind without its fragment."""
    from sqlmodel import select

    from amdockvs.models import DockingResultRecord, MoleculeRecord, ScreeningTarget
    from amdockvs.core.vocab import TargetState
    from amdockvs.docking.rerun import supersede_shard_runs
    from amdockvs.docking.results.dataset import _summaries
    from amdockvs.screening.campaign import record_docked
    from amdockvs.runtime import AMDockVSRuntime
    from catalog_seed import seed_library

    protocol = {"program": "vina", "label": "Vina", "hash": "proto-partial"}
    runtime = AMDockVSRuntime()
    runtime.create_project(name="partial-replacement", folder=tmp_path)
    try:
        project_db = runtime.workspace.project_db
        root = Path(project_db.db_path).parent
        with project_db.get_session() as session:
            batch_id = _batch(session, "library.smi")
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            # Library molecules are addressed by (import batch, shard record): records 0 and 1
            # belong to the first shard, record 2 to the second.
            ligands = [MoleculeRecord(name=f"lib:{index}", molecule_type="small_molecule",
                                      is_ligand=True, import_batch_id=batch_id, source_index=index)
                       for index in (0, 1, 2)]
            session.add_all([receptor, *ligands])
            session.commit()
            receptor_id = int(receptor.id)
            stale, fresh, untouched = (int(row.id) for row in ligands)
        # Two shards of one file: records 0-1 and 2-3 (the shard header names the batch).
        _file, (first, second) = seed_library(project_db, tmp_path, scope=batch_id, shards=2, records=2)

        old_run = _run(project_db, "run-old", protocol_hash="proto-partial")
        new_run = _run(project_db, "run-new", protocol_hash="proto-partial")
        key = _key(receptor_id, protocol_hash="proto-partial")
        for name, run, shard, records in (
            ("old-first", old_run, first, {0: -9.0, 1: -7.0}),
            ("old-second", old_run, second, {2: -9.5}),
            ("new-first", new_run, first, {0: -8.0, 1: -11.0}),
        ):
            path = _fragment(root / "results" / "parts" / f"{name}.arrows", receptor_id=receptor_id,
                             records=records, run_id=str(run), protocol=protocol,
                             shard_id=shard, docking_run_id=run)
            record_docked(project_db, key=key, shard_id=shard, result_path=str(path), force=True)
        with project_db.get_session() as session:
            session.add(ScreeningTarget(
                docking_run_id=old_run, receptor_molecule_id=receptor_id, receptor_name="R",
                engine="vina", ligands_total=3, ligands_done=3, state=TargetState.DONE,
                state_keys=key,
            ))
            # The old run promoted record 0 of the first shard and record 2 of the second; the
            # new run ranks record 1 of the first shard above record 0, which no longer makes it.
            for run, ligand, score in ((old_run, stale, -9.0), (old_run, untouched, -9.5),
                                       (new_run, fresh, -11.0)):
                session.add(DockingResultRecord(
                    docking_run_id=run, receptor_molecule_id=receptor_id,
                    ligand_molecule_id=ligand, pose_rank=1, score=score, engine="vina",
                ))
            session.commit()

        assert supersede_shard_runs(project_db, docking_run_id=new_run) == 1
        with project_db.get_session() as session:
            results = sorted((int(row.docking_run_id), int(row.ligand_molecule_id))
                             for row in session.exec(select(DockingResultRecord)).all())
            targets = [int(row.docking_run_id) for row in session.exec(select(ScreeningTarget)).all()]
        rows = _summaries(project_db, [key], ("shard_id", "ligand_id", "docking_run_id"))[0].select(
            ["shard_id", "ligand_id", "docking_run_id"]).to_pylist()
        # What a read sees: the replaced shard's rows are the new run's; the untouched shard keeps the old run's.
        assert sorted(tuple(row.values()) for row in rows) == sorted([
            (first, 0, new_run), (first, 1, new_run), (second, 2, old_run),
        ])
        # The hit of the replaced shard goes with its rows; the untouched shard keeps both.
        assert results == sorted([(old_run, untouched), (new_run, fresh)])
        assert targets == [old_run]  # the old run still has a shard against that receptor
    finally:
        runtime.close_project()
