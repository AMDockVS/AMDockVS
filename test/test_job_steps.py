"""A job says what each of its chunks goes through, for the job monitor (`JobSnapshot.task_steps`)."""

from amdockvs.chemistry.jobs import ligand_chemistry_job, receptor_chemistry_job, shard_chemistry_job
from amdockvs.docking.preparation.jobs import PrepareLigandShardsJobSpec


def _said(definition, params):
    steps = definition.engine.__job__["task"].__task__["steps"]
    return list(steps(params) if callable(steps) else steps)


def test_a_chemistry_job_names_the_functions_its_chunks_run_with_their_settings():
    lines = _said(ligand_chemistry_job, {
        "operation": [["protonate", {"ph": 6.5}], ["generate_3d", {"attempts": 3}], ["minimize", {}]],
        "params": {"method": "dimorphite"},
    })
    assert lines[0] == "read the molecules" and lines[-1] == "store them"
    assert "protonate_molecule_batch (dimorphite, pH 6.5)" in lines[1]
    assert "generate_ligand_3d" in lines[2] and "best of 3 by MMFF" in lines[2]
    assert lines[3].endswith("minimize_ligand_molecule (MMFF)")
    shard = _said(shard_chemistry_job, {"operation": "generate_3d", "params": {}})
    assert shard[0] == "read the shard" and "one embedding, not optimized" in shard[1]
    receptor = _said(receptor_chemistry_job, {"operation": "protonate", "params": {"method": "pdb2pqr"}})
    assert "pdb2pqr, AMBER, pH 7.0" in receptor[1]


def test_shard_preparation_says_it_makes_3d_before_the_pdbqt():
    lines = _said(PrepareLigandShardsJobSpec.to_job_definition(), {})
    assert [i for i, line in enumerate(lines) if "generate_ligand_3d" in line] == [1]
    assert "ms_adtools" in lines[2]
