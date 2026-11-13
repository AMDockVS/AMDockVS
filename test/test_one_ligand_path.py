"""Every ligand format filters and splits its molecules the same way.

Worth a test because each format used to carry its own copy of these steps, and the copies
drifted: a MOL/PDB/PDBQT ligand skipped the filters entirely (Max heavy atoms = 1 kept a 30-atom
ligand), a split-off fragment skipped the streaming filter (naphthol stored under Include SMARTS
= N), and neither a MOL row nor the multithreaded SDF reader split at all.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

Chem = pytest.importorskip("rdkit.Chem")

from amdockvs.io.transformers.materializers import materialize_import_batch, materialize_multithreaded_sdf_file

PAIR = "c1ccccc1C(=O)Nc1ccccc1.c1ccc2ccccc2c1O"  # benzanilide + naphthol: both drug-sized
PROPANOL_PDBQT = """ROOT
ATOM      1  C   UNL     1       0.000   0.000   0.000  1.00  0.00     0.000 C 
ATOM      2  C   UNL     1       1.520   0.000   0.000  1.00  0.00     0.000 C 
ATOM      3  C   UNL     1       2.040   1.430   0.000  1.00  0.00     0.000 C 
ATOM      4  O   UNL     1       3.460   1.430   0.000  1.00  0.00     0.000 OA
ENDROOT
TORSDOF 1
"""


def _with_coordinates(smiles, name):
    from rdkit.Chem import AllChem

    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    mol = Chem.RemoveHs(mol)
    mol.SetProp("_Name", name)
    return mol


def _write(tmp_path, suffix, smiles="CCO", name="ethanol"):
    path = tmp_path / f"{name}{suffix}"
    if suffix == ".pdbqt":
        path.write_text(PROPANOL_PDBQT, encoding="utf-8")
    elif suffix == ".pdb":
        path.write_text(Chem.MolToPDBBlock(_with_coordinates(smiles, name)), encoding="utf-8")
    else:
        path.write_text(Chem.MolToMolBlock(_with_coordinates(smiles, name)), encoding="utf-8")
    return path


def _rows(path, prefilter, input_format=None, entries=None):
    payload = {
        "kind": "ligand",
        "file_path": str(path),
        "storage_dir": str(path.parent / "data"),
        "input_format": input_format or path.suffix[1:],
        "primary_role": "ligand",
        "molecule_kind": "small_molecule",
        "prefilter": {"target_molecule_kinds": ["small_molecule"], **prefilter},
        "entries": entries or [{"source_index": 0, "source_file": str(path)}],
    }
    return materialize_import_batch(payload)


@pytest.mark.parametrize("suffix", [".mol", ".mdl", ".pdb", ".pdbqt"])
@pytest.mark.parametrize("prefilter", [{"max_heavy_atoms": 1}, {"include_smarts": ["N"]}],
                         ids=["legacy-criteria", "streaming-filter"])
def test_a_structure_format_ligand_obeys_the_filters(tmp_path, suffix, prefilter):
    path = _write(tmp_path, suffix)
    assert _rows(path, prefilter) == []
    assert len(_rows(path, {"max_heavy_atoms": 50})) == 1  # and passes one it meets


def test_a_split_fragment_obeys_the_streaming_filter(tmp_path):
    smi = tmp_path / "pair.smi"
    smi.write_text(f"{PAIR} pair\n", encoding="utf-8")
    entries = [{"source_index": 0, "raw": f"{PAIR} pair"}]
    keep_n = _rows(smi, {"split_fragments": True, "include_smarts": ["N"]}, "smiles", entries)
    assert [row["name"] for row in keep_n] == ["pair"]  # naphthol has no N


def test_each_molecule_of_a_record_is_judged_on_its_own(tmp_path):
    smi = tmp_path / "pair.smi"
    smi.write_text(f"{PAIR} pair\n", encoding="utf-8")
    entries = [{"source_index": 0, "raw": f"{PAIR} pair"}]
    drop_n = _rows(smi, {"split_fragments": True, "exclude_smarts": ["N"]}, "smiles", entries)
    assert [row["name"] for row in drop_n] == ["pair [frag02]"]  # the anilide goes, naphthol stays


def test_a_mol_row_splits_its_fragments(tmp_path):
    path = _write(tmp_path, ".mol", smiles=PAIR, name="pair")
    assert [row["name"] for row in _rows(path, {"split_fragments": True})] == ["pair", "pair [frag02]"]
    assert [row["name"] for row in _rows(path, {})] == ["pair"]


def test_the_multithreaded_sdf_reader_splits_and_keeps_every_row(tmp_path):
    sdf = tmp_path / "library.sdf"
    writer = Chem.SDWriter(str(sdf))
    writer.write(_with_coordinates(PAIR, "pair"))
    writer.write(_with_coordinates("CCO", "ethanol"))
    writer.close()
    payload = {
        "kind": "ligand",
        "file_path": str(sdf),
        "storage_dir": str(tmp_path / "data"),
        "num_threads": 2,
        "primary_role": "ligand",
        "molecule_kind": "small_molecule",
        "prefilter": {"target_molecule_kinds": ["small_molecule"], "split_fragments": True},
    }
    rows = materialize_multithreaded_sdf_file(payload)
    assert [row["name"] for row in rows] == ["pair", "pair [frag02]", "ethanol"]
