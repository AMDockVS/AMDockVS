"""A receptor with no polymer in it is all receptor.

Worth a test because the failure is silent: the import succeeds and stores an empty model
(every HETATM residue taken for a co-crystal ligand and stripped).
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

gemmi = pytest.importorskip("gemmi")
pytest.importorskip("rdkit")

from amdockvs.io.receptor_preview import (
    ReceptorImportOptions,
    build_receptor_import_preview,
    scan_receptor_structure,
    write_processed_receptor,
)
from amdockvs.io.transformers.materializers import materialize_import_batch

ETHANOL_MOL = """ethanol
     RDKit          3D

  3  2  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.2990    0.7500    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.5980    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  2  3  1  0
M  END
"""

PROTEIN_WITH_LIGAND = """ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00  0.00           C
ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00  0.00           C
ATOM      4  O   ALA A   1       1.251   2.390   0.000  1.00  0.00           O
ATOM      5  CB  ALA A   1       1.988  -0.773  -1.199  1.00  0.00           C
HETATM    6  C1  LIG A 101       4.000   4.000   4.000  1.00  0.00           C
HETATM    7  O1  LIG A 101       5.200   4.000   4.000  1.00  0.00           O
END
"""

ALTLOC_PROTEIN = """ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00  0.00           C
ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00  0.00           C
ATOM      4  O   ALA A   1       1.251   2.390   0.000  1.00  0.00           O
ATOM      5  CB AALA A   1       1.988  -0.773  -1.199  0.60  0.00           C
ATOM      6  CB BALA A   1       1.900  -0.700  -1.300  0.40  0.00           C
END
"""

WATERS_ONLY = """HETATM    1  O   HOH A 201       0.000   0.000   0.000  1.00  0.00           O
HETATM    2  O   HOH A 202       5.000   0.000   0.000  1.00  0.00           O
END
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _receptor_rows(path):
    payload = {
        "kind": "receptor",
        "file_path": str(path),
        "storage_dir": str(path.parent / "data"),
        "input_format": path.suffix[1:],
        "primary_role": "receptor",
        "molecule_kind": "protein",
        "entries": [{"source_index": 0, "source_file": str(path)}],
    }
    return materialize_import_batch(payload)


def _stored_atoms(row):
    document = gemmi.cif.read_string(bytes(row["current_payload_id"].data).decode())
    return gemmi.make_structure_from_block(document.sole_block())[0].count_atom_sites()


def _processed_atoms(tmp_path, name, text, options=ReceptorImportOptions()):
    source = _write(tmp_path, name, text)
    output = tmp_path / f"{source.stem}_out.cif"
    write_processed_receptor(source, output, scan=scan_receptor_structure(source), options=options)
    return gemmi.read_structure(str(output))[0]


@pytest.mark.parametrize("name,text,atoms", [("ethanol.mol", ETHANOL_MOL, 3)])
def test_a_small_molecule_receptor_keeps_every_atom(tmp_path, name, text, atoms):
    rows = _receptor_rows(_write(tmp_path, name, text))
    assert [row["primary_role"] for row in rows] == ["receptor"]
    assert rows[0]["n_atoms"] == atoms
    assert _stored_atoms(rows[0]) == atoms


def test_a_receptor_with_no_polymer_offers_nothing_of_itself_as_a_ligand(tmp_path):
    source = _write(tmp_path, "host.pdb", PROTEIN_WITH_LIGAND.replace("ATOM  ", "HETATM").replace("ALA", "CYD"))
    preview = build_receptor_import_preview(scan_receptor_structure(source), ReceptorImportOptions())
    assert preview["workflow"]["reference_ligands"] == []
    assert preview["binding_site_specs"] == []
    assert preview["structure"]["components"]["ligands"] == []


def test_a_protein_still_loses_its_cocrystal_ligand(tmp_path):
    model = _processed_atoms(tmp_path, "complex.pdb", PROTEIN_WITH_LIGAND)
    assert [residue.name for chain in model for residue in chain] == ["ALA"]
    assert model.count_atom_sites() == 5


def test_a_receptor_left_with_no_atoms_is_refused(tmp_path):
    with pytest.raises(ValueError, match="nothing is left of the receptor"):
        _processed_atoms(tmp_path, "waters.pdb", WATERS_ONLY)
