"""CCD mmCIF ligands: a component definition imports as its molecule, and a CIF that holds no
atoms is refused instead of being stored as a 0-atom "SDF" that is really a bare PDB header."""
from __future__ import annotations

import gzip
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    from amdockvs.runtime import AMDockVSRuntime

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("AMDOCK_CCD_CACHE", str(tmp_path / "ccd"))
    monkeypatch.setenv("AMDOCK_OFFLINE", "1")
    rt = AMDockVSRuntime()
    rt.create_project(name="ccd", folder=tmp_path / "project")
    yield rt
    rt.shutdown()


def _import(runtime, path):
    from sqlmodel import select

    from amdockvs.core.payloads import molecule_payload_id, read_structure
    from amdockvs.models import MoleculeRecord

    runtime.wait_for_jobs(list(runtime.loader.load_ligands([path], executor_name="thread")), timeout_s=60)
    with runtime.workspace.project_db.get_session() as session:
        rows = session.exec(select(MoleculeRecord)).all()
    return [(row, *read_structure(runtime.workspace.project_db, molecule_payload_id(row))) for row in rows]


def _gz(source, tmp_path):
    target = tmp_path / (source.name + ".gz")
    target.write_bytes(gzip.compress(source.read_bytes()))
    return target


@pytest.mark.parametrize("name,heavy_atoms,smiles", [
    ("ccd_component_7EP.cif", 26, "COc1cccc(OC)c1C(=O)NC(C=O)C1NC(C(=O)O)C(C)(C)S1"),
    # Its pyrazole NH only kekulizes with the hydrogens the component lists.
    ("ccd_component_LIG.cif", 18, "c1ccc2c(c1)Cc1c-2n[nH]c1-c1ccncc1"),
])
def test_read_mol_builds_a_ccd_component(tmp_path, name, heavy_atoms, smiles):
    pytest.importorskip("rdkit")
    from rdkit import Chem

    from amdockvs.io.formats import read_mol

    for path in (DATA / name, _gz(DATA / name, tmp_path)):
        mol = read_mol(path)
        assert mol is not None, path
        assert mol.GetNumHeavyAtoms() == heavy_atoms
        assert Chem.MolToSmiles(mol) == Chem.CanonSmiles(smiles)
        assert mol.GetConformer().Is3D()


def test_a_cif_with_no_atoms_does_not_render_as_pdb(tmp_path):
    from amdockvs.io.formats import to_pdb

    assert not to_pdb(DATA / "ccd_ligand.cif", tmp_path / "out.pdb")  # bonds only, no coordinates


@pytest.mark.parametrize("compressed", [False, True])
def test_ccd_component_imports_as_a_readable_sdf(runtime, tmp_path, compressed):
    from rdkit import Chem

    source = DATA / "ccd_component_7EP.cif"
    rows = _import(runtime, _gz(source, tmp_path) if compressed else source)

    assert len(rows) == 1
    row, text, kind = rows[0]
    assert kind == "sdf" and row.n_atoms == 26
    mol = Chem.MolFromMolBlock(text)
    assert mol is not None and mol.GetNumHeavyAtoms() == 26


@pytest.mark.parametrize("compressed", [False, True])
def test_a_cif_with_no_atoms_is_not_imported_as_a_ligand(runtime, tmp_path, compressed):
    source = DATA / "ccd_ligand.cif"
    assert _import(runtime, _gz(source, tmp_path) if compressed else source) == []
