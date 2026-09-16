"""A PDB whose CONECT topology is fine but whose atoms sit close enough for RDKit's proximity
bonding to invent a bond (rdkit#9581) must still be read."""
from pathlib import Path

from rdkit import Chem

from amdockvs.io.formats import _read_pdb

# Sulfate S 2.33 A from a phosphate P (anhydride), as in CCD ADX; resname UNL -> no CCD lookup.
PDB = """\
HETATM    1  S   UNL A   1       0.000   0.000   0.000  1.00 20.00           S
HETATM    2  O1  UNL A   1       0.000   1.420   0.000  1.00 20.00           O
HETATM    3  O2  UNL A   1       1.230  -0.710   0.000  1.00 20.00           O
HETATM    4  O3  UNL A   1      -0.900  -0.500   1.100  1.00 20.00           O
HETATM    5  O4  UNL A   1      -0.900  -0.500  -1.100  1.00 20.00           O
HETATM    6  P   UNL A   1      -2.300   0.000  -0.400  1.00 20.00           P
HETATM    7  O5  UNL A   1      -2.900   1.300  -0.600  1.00 20.00           O
HETATM    8  O6  UNL A   1      -3.100  -1.000   0.500  1.00 20.00           O
HETATM    9  O7  UNL A   1      -2.500  -0.600  -1.900  1.00 20.00           O
CONECT    1    2    2    3    3    4    5
CONECT    5    6
CONECT    6    7    7    8    9
END
"""


def test_conect_topology_survives_proximity_bonding(tmp_path: Path) -> None:
    path = tmp_path / "unl.pdb"
    path.write_text(PDB)
    assert Chem.MolFromPDBFile(str(path)) is None  # the RDKit default trips on the S...P contact
    mol = _read_pdb(path, sanitize=True, remove_hs=False, index=0, resname="UNL")
    assert mol is not None
    assert mol.GetNumBonds() == 8
