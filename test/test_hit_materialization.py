"""§4 of `docs/plan_modos_vs_htpvs_2026-09-03.md`: threshold AND cap, whichever binds first.

The check the plan asks for: a gate that lets everything through with `cap=10` materializes
exactly 10 rows, and a `light` ingest leaves rows with SMILES and pose but no descriptors.
"""
from pathlib import Path
import sys

import pytest
from sqlmodel import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.screening.materialize import HitGate, ingest_hits
from amdockvs.models import DockingResult, MoleculeModel, MoleculeRecord
from amdockvs.molecules.storage import ShardStore
from amdockvs.runtime import AMDockVSRuntime


@pytest.fixture
def project(tmp_path):
    rt = AMDockVSRuntime()
    rt.create_project(name="hits", folder=tmp_path)
    yield rt
    rt.close_project()


def hit_rows(count, *, score=-9.0, receptor_id=1):
    return [
        {
            "name": f"hit_{index}",
            "smiles": "CCO",
            "source": "/data/library.smi",
            "source_index": index,
            "engine": "vina",
            "score": score,
            "score_type": "vina_score",
            "pose_path": f"poses/hit_{index}.pdbqt",
            "pose_rank": 1,
            "receptor_molecule_id": receptor_id,
            "metrics": {"run_id": "run-1"},
            # Only a `full` payload keeps these.
            "logp": 1.23,
            "heavy_atom_count": 3,
            "models": [f"models/hit_{index}_0.sdf"],
        }
        for index in range(count)
    ]


def test_cap_binds_before_a_permissive_threshold(project):
    """The plan's check: predicate passes everything, cap=10 → exactly 10 rows."""
    gate = HitGate(cap=10)
    project_db = project.molsuite.project_db
    ingest_hits(project_db, hit_rows(40), gate=gate)
    ingest_hits(project_db, hit_rows(40), gate=gate)  # incremental: the count carries over
    with project_db.get_session() as session:
        assert len(session.exec(select(MoleculeRecord)).all()) == 10
        assert len(session.exec(select(DockingResult)).all()) == 10
    assert gate.full


def test_threshold_binds_before_the_cap(project):
    gate = HitGate(cap=100, threshold=-8.5)
    kept = ingest_hits(project.molsuite.project_db, hit_rows(3, score=-9.0) + hit_rows(5, score=-7.0), gate=gate)
    assert len(kept) == 3


def test_light_payload_keeps_smiles_and_pose_but_no_descriptors(project):
    project_db = project.molsuite.project_db
    ingest_hits(project_db, hit_rows(1), gate=HitGate(cap=5))
    with project_db.get_session() as session:
        molecule = session.exec(select(MoleculeRecord)).one()
        result = session.exec(select(DockingResult)).one()
        assert molecule.extra_data["smiles"] == "CCO"
        assert molecule.logp is None and molecule.heavy_atom_count is None
        assert not session.exec(select(MoleculeModel)).all()
    assert result.pose_path == "poses/hit_0.pdbqt"
    assert result.score == -9.0
    assert result.ligand_molecule_id == molecule.id


def test_full_payload_brings_descriptors_and_conformers(project):
    project_db = project.molsuite.project_db
    ingest_hits(project_db, hit_rows(1), gate=HitGate(cap=5), payload="full")
    with project_db.get_session() as session:
        molecule = session.exec(select(MoleculeRecord)).one()
        assert molecule.logp == 1.23 and molecule.heavy_atom_count == 3
        assert len(session.exec(select(MoleculeModel)).all()) == 1


def test_a_cap_is_not_optional(project):
    with pytest.raises(ValueError):
        HitGate(cap=0)
    with pytest.raises(ValueError, match="HitGate"):
        ShardStore(project.molsuite.project_db).materialize(hit_rows(1))


def test_shard_store_materializes_through_its_gate(project):
    store = ShardStore(project.molsuite.project_db, gate=HitGate(cap=2))
    assert len(store.materialize(hit_rows(5))) == 2
    assert store.materialize(hit_rows(5)) == []


def test_off_target_results_reuse_the_reference_hit_molecule(project):
    project_db = project.molsuite.project_db
    with project_db.get_session() as session:
        receptors = [
            MoleculeRecord(name=name, molecule_type="protein", is_receptor=True)
            for name in ("reference", "off-target")
        ]
        session.add_all(receptors)
        session.commit()
        receptor_ids = [int(row.id) for row in receptors]
    reference = hit_rows(1, receptor_id=receptor_ids[0])
    off_target = hit_rows(1, receptor_id=receptor_ids[1])
    ingest_hits(project_db, reference, gate=HitGate(cap=1), reuse_existing=True)
    ingest_hits(project_db, off_target, gate=HitGate(cap=1), reuse_existing=True)
    ingest_hits(project_db, off_target, gate=HitGate(cap=1), reuse_existing=True)  # retry is idempotent

    with project_db.get_session() as session:
        molecules = session.exec(select(MoleculeRecord).where(MoleculeRecord.is_ligand.is_(True))).all()
        results = session.exec(select(DockingResult).order_by(DockingResult.receptor_molecule_id)).all()
    assert len(molecules) == 1
    assert [row.receptor_molecule_id for row in results] == receptor_ids
    assert {row.ligand_molecule_id for row in results} == {molecules[0].id}
