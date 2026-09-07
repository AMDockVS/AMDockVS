"""Reading every accepted molecule file, and what a PDBQT import turns into.

The PDBQT fixture is written by hand rather than by meeko so the AutoDock-type inversion is
exercised without the ``REMARK SMILES`` shortcut: aromatic carbons are typed ``A`` and the
carbonyl oxygen ``OA``, which is all the bond-order information a PDBQT carries.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from rdkit import Chem

from amdockvs.io.formats import (
    as_pdb,
    canonical_suffix,
    normalized_suffix,
    read_mol,
    to_canonical,
)
from amdockvs.io.payloads import ImportBatchPayload
from amdockvs.io.transformers.materializers import build_import_graph_payload
from amdockvs.io.transformers.structures import _materialize_structure_rows
from amdockvs.models.molecules import MoleculeType

# Benzoic acid: a ring that must come back aromatic and a C=O that must come back double.
_PDBQT = """REMARK  Name = benzoic acid
ROOT
ATOM      1  C1  UNL     1       1.396   0.000   0.000  1.00  0.00     0.010 A
ATOM      2  C2  UNL     1       0.698   1.209   0.000  1.00  0.00     0.010 A
ATOM      3  C3  UNL     1      -0.698   1.209   0.000  1.00  0.00     0.010 A
ATOM      4  C4  UNL     1      -1.396   0.000   0.000  1.00  0.00     0.010 A
ATOM      5  C5  UNL     1      -0.698  -1.209   0.000  1.00  0.00     0.010 A
ATOM      6  C6  UNL     1       0.698  -1.209   0.000  1.00  0.00     0.010 A
ATOM      7  C7  UNL     1       2.896   0.000   0.000  1.00  0.00     0.200 C
ATOM      8  O1  UNL     1       3.520   1.060   0.000  1.00  0.00    -0.250 OA
ATOM      9  O2  UNL     1       3.520  -1.060   0.000  1.00  0.00    -0.350 OA
ATOM     10  H1  UNL     1       4.480  -1.060   0.000  1.00  0.00     0.210 HD
ENDROOT
TORSDOF 1
"""


# Two residues of a real chain: receptor import keeps standard residues and drops the rest, so a
# lone HETATM ligand would legitimately come back empty.
_RECEPTOR_PDBQT = """REMARK  receptor
ATOM      1  N   SER A 438      11.317  66.182  33.926  1.00  0.00    -0.350 N
ATOM      2  HN  SER A 438      10.900  65.300  33.700  1.00  0.00     0.160 HD
ATOM      3  CA  SER A 438      12.317  66.182  34.926  1.00  0.00     0.180 C
ATOM      4  C   SER A 438      13.317  67.182  34.926  1.00  0.00     0.240 C
ATOM      5  O   SER A 438      13.717  67.582  33.826  1.00  0.00    -0.270 OA
ATOM      6  N   ALA A 439      13.717  67.582  36.126  1.00  0.00    -0.350 N
ATOM      7  CA  ALA A 439      14.717  68.582  36.326  1.00  0.00     0.170 C
ATOM      8  C   ALA A 439      15.717  68.182  37.426  1.00  0.00     0.240 C
ATOM      9  O   ALA A 439      16.117  68.982  38.226  1.00  0.00    -0.270 OA
"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_ent_and_sd_are_read_as_pdb_and_sdf():
    assert normalized_suffix("/x/1abc.ent") == ".pdb"
    assert normalized_suffix("/x/lib.SD") == ".sdf"
    assert normalized_suffix("/x/1abc.mmcif") == ".cif"


def test_canonical_format_follows_the_molecule_kind():
    assert canonical_suffix(MoleculeType.SMALL_MOLECULE) == ".sdf"
    assert canonical_suffix(MoleculeType.PROTEIN) == ".cif"


def test_autodock_types_restore_the_aromatic_ring_and_the_carbonyl():
    with tempfile.TemporaryDirectory() as tmp:
        mol = read_mol(_write(Path(tmp), "lig.pdbqt", _PDBQT))
    assert mol is not None
    smiles = Chem.MolToSmiles(Chem.RemoveHs(mol))
    assert smiles == "O=C(O)c1ccccc1", smiles


def test_a_pdbqt_becomes_an_sdf_and_a_prepared_ad4_molecule():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _write(root, "lig.pdbqt", _PDBQT)
        storage = root / "molecules"
        storage.mkdir()
        batch = ImportBatchPayload(
            kind="ligand",
            file_path=source,
            storage_dir=storage,
            input_format="pdbqt",
            primary_role="ligand",
            molecule_kind=MoleculeType.SMALL_MOLECULE,
        )
        rows = list(_materialize_structure_rows(batch=batch, entries=[]))
        assert len(rows) == 1
        row = rows[0]
        # Stored as SDF; the PDBQT survives untouched as the engine artifact.
        assert Path(row["current_path"]).suffix == ".sdf"
        assert Path(row["stored_path"]).suffix == ".pdbqt"
        states = build_import_graph_payload(rows)["engine_states"]
        assert [(s["engine"], s["role_type"], s["is_ready"]) for s in states] == [
            ("ad4", "ligand", True)
        ]
        assert Path(states[0]["files"]["prepared"]).read_text().startswith("REMARK")


def test_a_receptor_pdbqt_is_stored_as_mmcif():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _write(root, "rec.pdbqt", _RECEPTOR_PDBQT)
        storage = root / "molecules"
        storage.mkdir()
        batch = ImportBatchPayload(
            kind="receptor",
            file_path=source,
            storage_dir=storage,
            input_format="pdbqt",
            primary_role="receptor",
            molecule_kind=MoleculeType.PROTEIN,
        )
        row = list(_materialize_structure_rows(batch=batch, entries=[]))[0]
        current = root / row["current_path"]  # rows carry project-relative paths
        assert current.suffix == ".cif"
        # PDB is what the preparation tools read, so the archive must render back to one whose
        # element column holds elements and whose chain column is a single character.
        with as_pdb(current) as rendered:
            atoms = [l for l in rendered.read_text().splitlines() if l.startswith(("ATOM", "HETATM"))]
        assert {line[76:78].strip() for line in atoms} == {"C", "N", "O", "H"}
        assert {line[21] for line in atoms} == {"A"}


def test_to_canonical_converts_between_the_stored_formats():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _write(root, "lig.pdbqt", _PDBQT)
        assert to_canonical(source, root / "lig.sdf", molecule_kind=MoleculeType.SMALL_MOLECULE)
        assert Chem.MolFromMolFile(str(root / "lig.sdf")) is not None
