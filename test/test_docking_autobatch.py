"""Several docking chunks to one task (`autobatch`): docked as one, each row back to its chunk."""
from amdockvs.core.jobs import _run_chunk
from amdockvs.docking.tasks import dock_together


def _chunk(*pairs):
    return {"engine": "unidock", "output_dir": "/tmp/x",
            "pairs": [{"receptor_id": r, "ligand_id": l} for r, l in pairs]}


def test_the_rows_of_one_run_go_back_to_the_chunk_of_their_pair():
    seen = []

    def run(payload):  # like `dock_with_interactions`: poses, then the interactions of each pose
        seen.append(payload)
        poses = [{"receptor_molecule_id": p["receptor_id"], "ligand_molecule_id": p["ligand_id"],
                  "pose_payload_id": object()} for p in payload["pairs"]]
        return {"docking_results": poses,
                "interaction_results": [{"pose_payload_id": pose["pose_payload_id"]} for pose in poses]}

    first, second = _chunk((1, 10), (2, 10)), _chunk((1, 11))
    parts = _run_chunk([first, second], run, dock_together)
    assert len(seen) == 1 and len(seen[0]["pairs"]) == 3 and seen[0]["engine"] == "unidock"
    assert [row["ligand_molecule_id"] for row in parts[0]["docking_results"]] == [10, 10]
    assert [row["ligand_molecule_id"] for row in parts[1]["docking_results"]] == [11]
    for part in parts:  # an interaction goes with its pose
        assert [r["pose_payload_id"] for r in part["interaction_results"]] == [
            r["pose_payload_id"] for r in part["docking_results"]]


def test_plain_rows_split_the_same_way():
    def run(payload, progress_cb):  # like `dock_pairs`
        return [{"receptor_molecule_id": p["receptor_id"], "ligand_molecule_id": p["ligand_id"]}
                for p in payload["pairs"]]

    parts = _run_chunk([_chunk((1, 10)), _chunk((1, 11), (1, 12))], run, dock_together)
    assert [len(part) for part in parts] == [1, 2]


def test_a_unidock_screening_packs_its_chunks_and_every_pair_gets_its_rows(tmp_path, monkeypatch):
    import pytest

    from amdockvs.docking.engines import unidock
    try:  # found from the real home: the test runs in a fake one
        monkeypatch.setenv("AMDOCK_UNIDOCK", str(unidock.unidock_command()))
    except RuntimeError:
        pytest.skip("Uni-Dock not installed")
    import test_amdock_runtime as rt

    rt._require_docking_stack()
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = rt.AMDockVSRuntime()
    try:
        runtime.create_project(name="autobatch", folder=tmp_path / "p", description="")
        rt._make_smiles_file(tmp_path / "l.smi", count=6)
        rt._make_receptor_pdb(tmp_path / "r.pdb")
        rt._wait_completed(runtime, [*runtime.loader.load_ligands([tmp_path / "l.smi"], batch_size=6, executor_name="thread"),
                                     *runtime.loader.load_receptors([tmp_path / "r.pdb"], batch_size=1, executor_name="thread")])
        receptor = next(runtime.molecules.stream(rt._role_scope(runtime, "receptor", limit=1)))
        runtime.docking.set_grid(receptor_id=int(receptor.id), center=(12.0, 13.0, 10.0), size=(20.0, 20.0, 20.0))
        rt._wait_completed(runtime, [runtime.chemistry.generate_3d_ligands(batch_size=6, executor_name="thread")])
        rt._wait_completed(runtime, [runtime.docking.prepare_ligands(batch_size=6, executor_name="thread"),
                                     runtime.docking.prepare_receptors(batch_size=1, executor_name="thread")])
        assert runtime.docking.autobatch(runtime.docking.get_program_spec("unidock"), 2) == 64
        job = runtime.docking.run(program="unidock", batch_size=2)  # 3 chunks of 2, one task: they start at 64
        rt._wait_completed(runtime, [job], timeout_s=300)
        results = runtime.docking.list_results()
        assert all(r.status == "completed" for r in results), [(r.ligand_id, r.status) for r in results]
        assert {r.ligand_id for r in results} == {
            m.id for m in runtime.molecules.stream(rt._role_scope(runtime, "ligand"))}
    finally:
        runtime.shutdown()
