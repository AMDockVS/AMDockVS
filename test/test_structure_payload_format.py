"""A structure stored as a non-small-molecule ligand holds the format its payload declares.

Worth a test because the bytes and the label parted ways silently: a polymer from a .cif was
stored as its PDB rendering in a payload labelled CIF, and only reading it back (`data_` header
missing) showed it. The row itself looked fine.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

gemmi = pytest.importorskip("gemmi")
Chem = pytest.importorskip("rdkit.Chem")

from amdockvs.io.transformers.materializers import materialize_import_batch


@pytest.fixture(scope="module")
def peptide_pdb(tmp_path_factory):
    from rdkit.Chem import AllChem

    mol = Chem.AddHs(Chem.MolFromSequence("GAS"))
    AllChem.EmbedMolecule(mol, randomSeed=11)
    path = tmp_path_factory.mktemp("peptide") / "gas.pdb"
    path.write_text(Chem.MolToPDBBlock(Chem.RemoveHs(mol)), encoding="utf-8")
    return path


def _as(source: Path, suffix: str, folder: Path) -> Path:
    target = folder / f"gas{suffix}"
    if suffix == ".pdb":
        target.write_text(source.read_text())
    elif suffix in {".cif", ".mmcif"}:
        structure = gemmi.read_structure(str(source))
        structure.setup_entities()
        structure.make_mmcif_document().write_file(str(target))
    else:  # .mol2 is not written by RDKit; a molfile stands in for the RDKit-read formats
        target.write_text(Chem.MolToMolBlock(Chem.MolFromPDBFile(str(source))))
    return target


def _stored(path: Path, molecule_kind: str):
    payload = {
        "kind": "ligand",
        "file_path": str(path),
        "storage_dir": str(path.parent / "data"),
        "input_format": path.suffix[1:],
        "primary_role": "ligand",
        "molecule_kind": molecule_kind,
        "entries": [{"source_index": 0, "source_file": str(path)}],
    }
    return materialize_import_batch(payload)


def _atoms_as_declared(payload) -> int:
    text = bytes(payload.data).decode()
    if payload.kind == "cif":
        return gemmi.make_structure_from_block(gemmi.cif.read_string(text).sole_block())[0].count_atom_sites()
    assert payload.kind == "pdb", payload.kind
    return gemmi.read_pdb_string(text)[0].count_atom_sites()


@pytest.mark.parametrize("suffix", [".cif", ".mmcif", ".pdb", ".mol"])
@pytest.mark.parametrize("molecule_kind", ["polymer", "peptide"])
def test_the_payload_parses_as_the_format_it_declares(tmp_path, peptide_pdb, suffix, molecule_kind):
    [row] = _stored(_as(peptide_pdb, suffix, tmp_path), molecule_kind)
    current = row["current_payload_id"] or row["stored_payload_id"]
    assert current.kind == "cif"  # mmCIF: the canonical form of anything not a small molecule
    assert _atoms_as_declared(current) == row["n_atoms"] > 0


def test_a_cif_source_is_kept_as_it_came(tmp_path, peptide_pdb):
    [row] = _stored(_as(peptide_pdb, ".cif", tmp_path), "polymer")
    assert row["current_payload_id"] is None  # current is the stored bytes, not a rewrite

