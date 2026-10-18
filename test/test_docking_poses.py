"""A docked pose leaves the worker as a payload: the ligand arrives as bytes, the engine reads and
writes files only while the chunk runs, and each result row carries the record of its rank."""
from pathlib import Path

import pytest

import amdockvs.docking.engines.registry as docking_registry
from amdockvs.docking.tasks import dock_pairs

MOL = "pose{n}\n  RDKit          3D\n\n  1  0  0  0  0  0  0  0  0  0999 V2000\n" \
      "    {n}.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\nM  END\n$$$$\n"


def test_each_row_keeps_the_pose_of_its_rank_and_no_file_survives(monkeypatch, tmp_path):
    monkeypatch.setattr(docking_registry, "DOCK_RUNNERS", dict(docking_registry.DOCK_RUNNERS))
    monkeypatch.setattr(docking_registry, "_BUILTINS_LOADED", docking_registry._BUILTINS_LOADED)  # loaded into the copy
    seen = {}

    def runner(payload):
        rows = []
        for pair in payload["pairs"]:
            ligand = Path(pair["ligand_path"])
            seen[pair["ligand_id"]] = ligand.read_bytes()
            sdf = tmp_path / f"lig_{pair['ligand_id']}.dock.sdf"
            sdf.write_text(MOL.format(n=1) + MOL.format(n=2))
            sdf.with_suffix(".pdbqt").write_text("MODEL 1\n")
            for rank in (1, 2):
                rows.append({"receptor_molecule_id": pair["receptor_id"], "ligand_molecule_id": pair["ligand_id"],
                             "pose_rank": rank, "score": -7.0 + rank, "pose_path": str(sdf),
                             "metrics": {"pose_index": rank - 1, "receptor_path": pair["receptor_path"]}})
        return rows

    docking_registry.register_dock_runner("test_poses", runner, replace=True)
    pair = {"ligand_id": 3, "receptor_id": 9, "ligand": b"REMARK ligand\n", "receptor_path": "/r/9.pdbqt",
            "receptor_payload_id": 41}
    rows = dock_pairs({"engine": "test_poses", "pairs": [pair], "output_dir": str(tmp_path)})

    assert seen == {3: b"REMARK ligand\n"}  # the program got a file holding the ligand's bytes
    assert [row["pose_payload_id"].data.decode().split("\n")[0] for row in rows] == ["pose1", "pose2"]
    assert {row["pose_payload_id"].series for row in rows} == {"poses"}
    assert all(row["metrics"] == {"pose_index": row["pose_rank"] - 1, "receptor_payload_id": 41} for row in rows)
    assert "pose_path" not in rows[0]
    assert list(tmp_path.iterdir()) == []  # the SDF and Vina's PDBQT went with the chunk


def test_the_interaction_job_reads_the_pose_bytes_and_the_receptor_it_was_docked_into(tmp_path, monkeypatch):
    from ms_flow.core.database.project import ProjectStore
    from payload_seed import seed_payload

    from amdockvs.core.constants import SERIES_POSES
    from amdockvs.docking.results.jobs import InteractionJobSpec
    from amdockvs.models.docking import DockingResult
    from amdockvs.models.molecules import MoleculeRecord

    store = ProjectStore.open_at(tmp_path / "project.db")
    source = seed_payload(store, "ATOM source\n", "pdb")
    docked = seed_payload(store, "ATOM prepared\n", "pdbqt")
    pose = seed_payload(store, MOL.format(n=1), "sdf", SERIES_POSES)
    with store.get_session() as session:
        receptor = MoleculeRecord(name="r", is_receptor=True, current_payload_id=source)
        ligand = MoleculeRecord(name="l", is_ligand=True)
        session.add_all([receptor, ligand])
        session.commit()
        session.add(DockingResult(receptor_molecule_id=receptor.id, ligand_molecule_id=ligand.id, engine="vina",
                                  score=-8.0, pose_payload_id=pose, metrics={"receptor_payload_id": docked}))
        session.commit()

    (chunk,) = InteractionJobSpec.build_chunks({}, {"project_db": store})
    (row,) = chunk["rows"]
    assert row["pose"] == MOL.format(n=1).encode()
    assert Path(row["receptor_path"]).read_text() == "ATOM prepared\n"

    from amdockvs.docking.results import tasks

    asked = []
    monkeypatch.setattr(tasks, "detect_interactions", lambda work: asked.extend(work) or [[
        {"interaction_type": "hbond", "residue": "ASP", "residue_index": 86, "distance": 2.9, "geometry": {}}]])
    files = set(tmp_path.rglob("*"))
    (out,) = tasks.interactions_of_poses(chunk)
    assert asked == [(MOL.format(n=1).encode(), b"ATOM prepared\n", "pdbqt")]
    assert out["interaction_type"] == "hbond" and (out["receptor_payload_id"], out["pose_payload_id"]) == (docked, pose)
    assert set(tmp_path.rglob("*")) == files  # the rows are the result: no report file per pose
    store.dispose()


@pytest.mark.parametrize("where", ["process", "ray"])
def test_vina_docks_with_the_ligand_as_bytes_and_the_receptor_carried(where, tmp_path):
    """A real Vina run away from the parent: the receptor is a `File` mark, so on Ray without a shared
    disk (`shared=False`) ms_flow carries it; the pose comes back as bytes, nothing stays on disk."""
    pytest.importorskip("vina")
    from ms_flow.engine import Engine, File, ProcessExecutor
    from rdkit import Chem

    data = Path(__file__).resolve().parent / "data"
    receptor = tmp_path / "site.pdbqt"  # the zinc example's site without its Zn: plain Vina scoring
    receptor.write_text("".join(line for line in (data / "1s63_zn_site.pdbqt").open() if " Zn" not in line[66:]))
    pair = {"ligand_id": 1, "receptor_id": 2, "ligand": (data / "1s63_ligand.pdbqt").read_bytes(),
            "receptor_path": File(receptor), "receptor_payload_id": 7,
            "exhaustiveness": 1, "num_modes": 2,
            "box_center": [18.0, 134.0, -1.0], "box_size": [15.0, 11.25, 18.75], "spacing": 0.375}
    chunk = {"engine": "vina", "pairs": [pair], "output_dir": str(tmp_path / "out"), "scoring_function": "vina",
             "vina_backend": "python", "vina_cpu": 1, "seed": 1, "energy_range": 3.0, "min_rmsd": 1.0}
    if where == "ray":
        pytest.importorskip("ray")
        from ms_flow.engine import RayExecutor
        executor = RayExecutor(num_cpus=1, shared=False)
    else:
        executor = ProcessExecutor(1)
    got = []
    with Engine(cpus=1) as engine:
        engine.register(where, executor)
        job = engine.submit(where, dock_pairs, [chunk], on_results=got.extend)
        assert engine.wait(job, 300).status == "completed"
    ((_key, rows),) = got
    assert rows and all((row["score"] or 0) < -4 for row in rows), rows[0]["metrics"]
    import zstandard  # the pose crossed a process: pickling compressed it in the worker
    sdf = lambda pose: zstandard.ZstdDecompressor().decompress(pose.data, max_output_size=pose.raw_length)
    poses = [Chem.MolFromMolBlock(sdf(row["pose_payload_id"]).decode()) for row in rows]
    assert all(pose is not None and pose.GetNumAtoms() > 10 for pose in poses)
    assert {row["metrics"]["receptor_payload_id"] for row in rows} == {7}
    assert not list((tmp_path / "out").rglob("*.*"))  # the poses went as bytes


def test_a_chunk_finds_the_interactions_of_its_poses_and_stores_each_pose_once(tmp_path):
    """Docking and interactions in one task (`dock_with_interactions`): the detection runs in an
    interpreter of its own on the pose bytes, and the sink gives the pose one id in both tables."""
    pytest.importorskip("vina")
    from ms_flow.core.database.project import ProjectStore
    from ms_flow.engine import writer

    from amdockvs.docking.jobs import DockingJobSpec
    from amdockvs.docking.tasks import dock_with_interactions
    from amdockvs.models.molecules import MoleculeRecord

    data = Path(__file__).resolve().parent / "data"
    receptor = tmp_path / "site.pdbqt"
    receptor.write_text("".join(line for line in (data / "1s63_zn_site.pdbqt").open() if " Zn" not in line[66:]))
    pair = {"ligand_id": 2, "receptor_id": 1, "ligand": (data / "1s63_ligand.pdbqt").read_bytes(),
            "receptor_path": str(receptor), "receptor_payload_id": 7, "exhaustiveness": 1, "num_modes": 2,
            "box_center": [18.0, 134.0, -1.0], "box_size": [15.0, 11.25, 18.75], "spacing": 0.375}
    chunk = {"engine": "vina", "pairs": [pair], "output_dir": str(tmp_path / "out"), "scoring_function": "vina",
             "vina_backend": "python", "vina_cpu": 1, "seed": 1, "energy_range": 3.0, "min_rmsd": 1.0}

    assert dock_with_interactions(chunk)["interaction_results"] == []  # not asked for
    out = dock_with_interactions({**chunk, "interactions": True})
    docked, found = out["docking_results"], out["interaction_results"]
    assert docked and {row["receptor_payload_id"] for row in found} == {7}
    assert {id(row["pose_payload_id"]) for row in found} == {id(row["pose_payload_id"]) for row in docked}
    assert {row["interaction_type"] for row in found} - {"none"}  # 1s63's ligand touches its site
    assert all(row["geometry"]["provider_payload"]["ligand_atoms"] for row in found)  # what PyMOL draws from

    store = ProjectStore.open_at(tmp_path / "project.db")
    with store.get_session() as session:
        session.add_all([MoleculeRecord(id=1, name="r", is_receptor=True), MoleculeRecord(id=2, name="l", is_ligand=True)])
        session.commit()
    writer(DockingJobSpec.output_spec, store).write([out])
    with store.get_session() as session:
        poses = lambda table: {row[0] for row in session.connection().exec_driver_sql(f"SELECT pose_payload_id FROM {table}")}
        assert poses("interaction_results") == poses("docking_results") and None not in poses("docking_results")
        assert len(poses("docking_results")) == len(docked)
    store.dispose()


def test_a_project_with_interactions_keyed_by_result_gets_the_table_keyed_by_structures(tmp_path):
    from types import SimpleNamespace

    from ms_flow.core.database.project import ProjectStore

    from amdockvs.runtime import AMDockVSRuntime

    store = ProjectStore.open_at(tmp_path / "project.db")
    with store.get_session() as session:
        conn = session.connection()
        conn.exec_driver_sql("DROP TABLE interaction_results")
        conn.exec_driver_sql("CREATE TABLE interaction_results (id INTEGER PRIMARY KEY, docking_result_id INTEGER)")
        conn.exec_driver_sql("INSERT INTO interaction_results (docking_result_id) VALUES (1)")
        session.commit()
    runtime = SimpleNamespace(workspace=SimpleNamespace(project_db=store))
    AMDockVSRuntime._rekey_interactions(runtime)
    AMDockVSRuntime._rekey_interactions(runtime)  # once made again, left alone
    with store.get_session() as session:
        conn = session.connection()
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(interaction_results)")}
        assert {"receptor_payload_id", "pose_payload_id"} <= columns and "docking_result_id" not in columns
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM interaction_results").scalar() == 0
    store.dispose()
