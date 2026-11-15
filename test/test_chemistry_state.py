import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "//src")


def test_molecule_state_metadata_reports_2d_molecule_without_explicit_hydrogens():
    pytest.importorskip("rdkit")

    from rdkit import Chem

    from amdockvs.chemistry.state import mol_has_3d, mol_has_explicit_hs, molecule_state_metadata

    mol = Chem.MolFromSmiles("CCO")
    assert mol is not None

    assert mol_has_3d(mol) is False
    assert mol_has_explicit_hs(mol) is False
    assert molecule_state_metadata(mol) == {
        "state": {
            "has_3d": False,
            "has_hs": False,
            "conformer_count": 0,
        }
    }


def test_molecule_state_metadata_reports_3d_molecule_with_explicit_hydrogens():
    pytest.importorskip("rdkit")

    from rdkit import Chem
    from rdkit.Chem import AllChem

    from amdockvs.chemistry.state import mol_has_3d, mol_has_explicit_hs, molecule_state_metadata

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    assert mol is not None
    assert AllChem.EmbedMolecule(mol, randomSeed=0xF00D) == 0

    assert mol_has_3d(mol) is True
    assert mol_has_explicit_hs(mol) is True

    state = molecule_state_metadata(mol)
    assert state["state"]["has_3d"] is True
    assert state["state"]["has_hs"] is True
    assert state["state"]["conformer_count"] == 1


def _prepared_ligand(tmp_path):
    """A project with one ligand that is prepared for docking, and the payload its prep holds."""
    from ms_flow.core.database import ProjectStore

    from amdockvs.core.payloads import payload_store, structure_payload
    from amdockvs.core.constants import SERIES_MOLECULES
    from amdockvs.models import LigandPrep, MoleculeRecord

    store = ProjectStore.open_at(tmp_path / "project.db")
    with store.get_session() as session:
        ligand = MoleculeRecord(name="lig", molecule_type="small_molecule", is_ligand=True, has_3d=True)
        session.add(ligand)
        session.flush()
        payloads = payload_store(store)
        current, prepared = payloads.put(session, [
            structure_payload("built\n$$$$\n", ".sdf", SERIES_MOLECULES),
            structure_payload("REMARK prepared\n", ".pdbqt", SERIES_MOLECULES),
        ])
        # As an import leaves it: the structure it came with, and the one it docks from now.
        ligand.stored_payload_id = current
        ligand.current_payload_id = current
        session.add(ligand)
        session.add(LigandPrep(molecule_id=int(ligand.id), family="ad4", payload_id=prepared))
        session.commit()
        return store, int(ligand.id), int(prepared)


def test_building_a_ligand_again_invalidates_its_docking_preparation(tmp_path):
    """RR-06: nothing records which geometry a preparation was made from, so pointing a ligand
    at a new conformer left its old preparation standing and docking reported itself ready --
    it would have docked a structure the ligand no longer has."""
    pytest.importorskip("rdkit")
    from sqlmodel import select
    from ms_flow.core.database import ProjectStore

    from amdockvs.chemistry.jobs import ligand_chemistry_finalize
    from amdockvs.models import LigandPrep

    store, ligand_id, prepared_payload = _prepared_ligand(tmp_path)
    try:
        with store.get_session() as session:
            assert session.exec(select(LigandPrep)).all()

        result = ligand_chemistry_finalize(
            {"operation": "generate_3d", "params": {"random_seed": 7}},
            {"project_db": store, "terminal_status": "completed"},
        )

        assert result == {"invalidated_preparations": 1}
        with store.get_session() as session:
            assert session.exec(select(LigandPrep)).all() == []
            # The ligand itself, and the structure it now docks from, are untouched.
            from amdockvs.models import MoleculeRecord

            assert session.get(MoleculeRecord, ligand_id).current_payload_id is not None
    finally:
        ProjectStore.clear_cached_stores()


def test_a_preparation_of_a_ligand_outside_the_run_is_left_alone(tmp_path):
    """Only the run's own scope: a set of one ligand does not invalidate the others."""
    pytest.importorskip("rdkit")
    from sqlmodel import select
    from ms_flow.core.database import ProjectStore

    from amdockvs.chemistry.jobs import ligand_chemistry_finalize
    from amdockvs.models import LigandPrep, MoleculeRecord

    store, ligand_id, _payload = _prepared_ligand(tmp_path)
    try:
        with store.get_session() as session:
            other = MoleculeRecord(name="other", molecule_type="small_molecule",
                                   is_ligand=True, has_3d=True)
            session.add(other)
            session.flush()
            other.stored_payload_id = session.get(MoleculeRecord, ligand_id).stored_payload_id
            session.add(other)
            session.add(LigandPrep(molecule_id=int(other.id), family="ad4", payload_id=None))
            session.commit()
            other_id = int(other.id)

        ligand_chemistry_finalize(
            {"operation": "minimize", "params": {}, "ligand_filters": {"id": ligand_id}},
            {"project_db": store, "terminal_status": "completed"},
        )

        with store.get_session() as session:
            assert [int(row.molecule_id) for row in session.exec(select(LigandPrep)).all()] == [other_id]
    finally:
        ProjectStore.clear_cached_stores()
