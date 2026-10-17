"""The 2D editor's result: unchanged compound -> None, 2D stays 2D, 3D comes back where it was."""
import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem

from amdockvs.molecules.editing import edited_structure


def _phenol_3d():
    mol = Chem.AddHs(Chem.MolFromSmiles("c1ccccc1O"))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    return mol


def _flat(mol):
    """What Ketcher hands back: the same atoms, drawn flat."""
    flat = Chem.Mol(mol)
    AllChem.Compute2DCoords(flat)
    return flat


def test_same_compound_is_no_edit():
    original = _phenol_3d()
    assert edited_structure(Chem.MolToMolBlock(_flat(original)), original) is None


def test_2d_edit_stays_2d():
    drawn = Chem.MolFromSmiles("c1ccccc1N")
    AllChem.Compute2DCoords(drawn)
    original = Chem.MolFromSmiles("c1ccccc1O")
    AllChem.Compute2DCoords(original)
    edited = edited_structure(Chem.MolToMolBlock(drawn), original)
    assert Chem.MolToSmiles(edited) == "Nc1ccccc1"
    assert not edited.GetConformer().Is3D()



def test_2d_edit_gives_drawn_atoms_explicit_hs_like_the_rest():
    original = Chem.AddHs(Chem.MolFromSmiles("c1ccccc1C"))
    AllChem.Compute2DCoords(original)
    drawn = Chem.RWMol(original)
    oxygen = drawn.AddAtom(Chem.Atom(8))  # what Ketcher returns for a drawn "OH": the H implicit
    drawn.AddBond(0, oxygen, Chem.BondType.SINGLE)
    drawn.RemoveAtom(next(a.GetIdx() for a in drawn.GetAtomWithIdx(0).GetNeighbors() if a.GetAtomicNum() == 1))
    drawn = drawn.GetMol()
    Chem.SanitizeMol(drawn)
    AllChem.Compute2DCoords(drawn)
    edited = edited_structure(Chem.MolToMolBlock(drawn), original)
    assert all(atom.GetNumImplicitHs() == 0 for atom in edited.GetAtoms())
    assert not edited.GetConformer().Is3D()

def test_3d_edit_keeps_the_untouched_atoms_in_place():
    original = _phenol_3d()
    drawn = Chem.RWMol(Chem.RemoveHs(_flat(original)))
    oxygen = next(a.GetIdx() for a in drawn.GetAtoms() if a.GetSymbol() == "O")
    carbon = drawn.AddAtom(Chem.Atom(6))  # phenol -> anisole, drawn flat
    drawn.AddBond(oxygen, carbon, Chem.BondType.SINGLE)
    edited = edited_structure(Chem.MolToMolBlock(drawn), original)
    assert Chem.MolToSmiles(Chem.RemoveHs(edited)) == "COc1ccccc1"
    assert edited.GetNumAtoms() > edited.GetNumHeavyAtoms()  # the original had explicit Hs
    reference = Chem.RemoveHs(original).GetConformer().GetPositions()
    moved = Chem.RemoveHs(edited).GetConformer().GetPositions()[:7]  # ring + O keep their indices
    assert np.sqrt(((moved - reference) ** 2).sum(axis=1).mean()) < 0.3


if __name__ == "__main__":
    test_same_compound_is_no_edit()
    test_2d_edit_stays_2d()
    test_3d_edit_keeps_the_untouched_atoms_in_place()
    print("OK")


def _project(tmp_path, monkeypatch):
    """A one-ligand project (real ProjectStore, structure in its volumes) with an activity."""
    from types import SimpleNamespace

    from ms_flow.core.database.project import ProjectStore
    from payload_seed import seed_payload

    from amdockvs.models import LigandActivity, MoleculeRecord
    from amdockvs.molecules.api import MoleculeAPI

    # clear_derived still reads the removed file columns until the deletion milestone (A7); an edit
    # that replaces a molecule calls it, so the molecule-edit tests stand it in.
    monkeypatch.setattr("amdockvs.project.deletion.clear_derived", lambda *_a, **_k: 0)
    db = ProjectStore.open_at(tmp_path / "project.db")
    imported = seed_payload(db, Chem.MolToMolBlock(_phenol_3d()).encode(), "sdf")
    with db.get_session() as s:
        s.add(MoleculeRecord(id=1, name="phenol", is_ligand=True, molecule_type="small_molecule",
                             stored_payload_id=imported, current_payload_id=imported, has_activity=True))
        s.add(LigandActivity(molecule_id=1, value=5.0))
        s.commit()
    runtime = SimpleNamespace(workspace=SimpleNamespace(project_db=db), _require_active_project=lambda: None)
    return MoleculeAPI(runtime), Chem.MolToMolBlock(_flat(Chem.AddHs(Chem.MolFromSmiles("c1ccccc1N")))), imported


def _smiles_of(api, molecule_id):
    return Chem.MolToSmiles(Chem.RemoveHs(Chem.MolFromMolBlock(api.molblock(molecule_id), removeHs=False)))


def test_edit_a_copy_leaves_the_molecule_and_its_activity(tmp_path, monkeypatch):
    api, drawn, imported = _project(tmp_path, monkeypatch)
    assert api.dependents(1) == {"activities": 1}
    new_id = api.edit(1, drawn, copy=True)
    assert new_id != 1
    assert api.get(1).current_payload_id == imported and api.dependents(1) == {"activities": 1}
    copy = api.get(new_id)
    assert copy.name == "phenol (edited)" and not copy.has_activity and api.dependents(new_id) == {}
    assert copy.stored_payload_id == copy.current_payload_id != imported
    assert _smiles_of(api, new_id) == "Nc1ccccc1" and _smiles_of(api, 1) == "Oc1ccccc1"


def test_replacing_drops_the_activity_and_keeps_the_original(tmp_path, monkeypatch):
    api, drawn, imported = _project(tmp_path, monkeypatch)
    assert api.edit(1, drawn) == 1
    edited = api.get(1)
    assert edited.stored_payload_id == imported and edited.current_payload_id != imported
    assert not edited.has_activity and api.dependents(1) == {}
    assert _smiles_of(api, 1) == "Nc1ccccc1"


def test_editing_again_releases_the_earlier_edit_but_never_the_import(tmp_path, monkeypatch):
    from amdockvs.core.payloads import payload_store

    api, drawn, imported = _project(tmp_path, monkeypatch)
    api.edit(1, drawn)
    first = api.get(1).current_payload_id
    other = Chem.MolToMolBlock(_flat(Chem.AddHs(Chem.MolFromSmiles("c1ccccc1Cl"))))
    api.edit(1, other)

    store = payload_store(api.runtime.workspace.project_db)
    assert api.get(1).current_payload_id not in (first, imported)
    assert set(store.kinds([first, imported])) == {imported}  # the first edit's bytes are gone
    assert _smiles_of(api, 1) == "Clc1ccccc1"


def test_select_fragment_cuts_it_from_the_original_and_keeps_the_activity(tmp_path, monkeypatch):
    from sqlmodel import select

    from amdockvs.models import LigandActivity, MoleculeRecord
    from amdockvs.molecules.fragments import ligand_fragments
    from payload_seed import seed_payload

    api, _drawn, imported = _project(tmp_path, monkeypatch)
    db = api.runtime.workspace.project_db
    salt = Chem.AddHs(Chem.MolFromSmiles("Oc1ccccc1.CC(=O)O"))
    salted = seed_payload(db, Chem.MolToMolBlock(salt).encode(), "sdf")
    fragmentation, _mols = ligand_fragments(salt)
    with db.get_session() as s:
        row = s.get(MoleculeRecord, 1)
        row.stored_payload_id = row.current_payload_id = salted
        row.extra_data = {"fragmentation": fragmentation}
        s.add(row)
        s.commit()

    other = next(c for c in fragmentation["components"] if c["fragment_index"] != fragmentation["selected_fragment_index"])
    row = api.select_fragment(1, other["fragment_index"])

    assert row.stored_payload_id == salted and row.current_payload_id != salted
    assert row.canonical_smiles == other["smiles"] == "CC(=O)O"
    assert row.extra_data["fragmentation"]["selected_fragment_index"] == other["fragment_index"]
    with db.get_session() as s:
        assert len(s.exec(select(LigandActivity)).all()) == 1  # measured on the imported compound
