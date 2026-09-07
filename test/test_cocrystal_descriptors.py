"""Guards PDB_SAFE_DESCRIPTORS: the descriptor subset a cocrystal ligand row may be filled with.

A ligand extracted from a deposited PDB has no CONECT records, so RDKit perceives connectivity by
proximity and every bond comes out single. This pins which descriptors survive that and — just as
important — which do not, so nobody widens the list on the assumption that RDKit "figures it out".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.io.transformers.structures import PDB_SAFE_DESCRIPTORS, cocrystal_ligand_descriptors

# Drug-like, with the aromatics and carbonyls that bond-order loss destroys.
SMILES = [
    "CC(=O)Oc1ccccc1C(=O)O",
    "CN1CCN(CC1)c1ccc2nc3ccccc3nc2c1",
    "Cc1ccc(cc1)S(=O)(=O)Nc1ccc(Cl)cc1C(=O)O",
    "OC(=O)c1cc2ccccc2n1Cc1ccc(F)cc1",
    "CC(C)Cc1ccc(cc1)C(C)C(=O)O",
    "Clc1ccccc1C1=NCc2nnc(C)n2-c2ccc(Cl)cc12",
]
# Measured, not assumed: these match 0/6 once bond orders are gone.
UNSAFE = ("mw", "exact_mw", "logp", "tpsa", "hbd", "aromatic_ring_count", "fraction_csp3")


def _as_deposited_pdb(smiles: str, path: Path):
    """3D molecule → PDB without CONECT, i.e. what extract_component_to_pdb leaves behind."""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    assert AllChem.EmbedMolecule(mol, randomSeed=0xF00D) == 0
    mol = Chem.RemoveHs(mol)
    block = Chem.MolToPDBBlock(mol)
    path.write_text("\n".join(l for l in block.splitlines() if not l.startswith("CONECT")))
    return mol


def test_safe_descriptors_survive_a_conectless_pdb(tmp_path):
    from amdockvs.chemistry.descriptors import calculate_basic_descriptors

    for index, smiles in enumerate(SMILES):
        path = tmp_path / f"lig{index}.pdb"
        truth = calculate_basic_descriptors(_as_deposited_pdb(smiles, path))
        recovered = cocrystal_ligand_descriptors(path)

        assert set(recovered) == set(PDB_SAFE_DESCRIPTORS)
        for key in PDB_SAFE_DESCRIPTORS:
            assert recovered[key] == pytest.approx(truth[key]), f"{smiles}: {key}"


def test_unsafe_descriptors_are_not_emitted(tmp_path):
    """If RDKit ever learns to recover these, this fails and the list can be widened on purpose."""
    from rdkit import Chem
    from rdkit.Chem import Descriptors

    from amdockvs.chemistry.descriptors import calculate_basic_descriptors

    for key in UNSAFE:
        assert key not in PDB_SAFE_DESCRIPTORS

    wrong = 0
    for index, smiles in enumerate(SMILES):
        path = tmp_path / f"lig{index}.pdb"
        truth = calculate_basic_descriptors(_as_deposited_pdb(smiles, path))
        from_pdb = calculate_basic_descriptors(
            Chem.MolFromPDBFile(str(path), sanitize=True, removeHs=False)
        )
        wrong += any(from_pdb[key] != pytest.approx(truth[key]) for key in UNSAFE)
    assert wrong == len(SMILES), "bond-order-less PDB now recovers descriptors it used to lose"
    assert Descriptors is not None


def test_missing_or_unparsable_file_yields_no_descriptors(tmp_path):
    assert cocrystal_ligand_descriptors(tmp_path / "absent.pdb") == {}

def test_cif_source_recovers_the_descriptors_a_pdb_loses(monkeypatch, tmp_path):
    """The whole point of the mmCIF route: real MW and aromatic rings, not perceived ones."""
    from rdkit import Chem

    from amdockvs.io.ccd_bonds import ligand_from_cif

    monkeypatch.setenv("AMDOCK_OFFLINE", "1")
    cif = Path(__file__).parent / "data" / "ccd_ligand.cif"
    pdb = Path(__file__).parent / "data" / "ccd_ligand.pdb"
    if not cif.exists():  # fixture is a trimmed RCSB entry, see test/data/README
        pytest.skip("mmCIF fixture not present")

    mol = ligand_from_cif(cif, pdb, "JXM")
    assert mol is not None
    assert Chem.MolToSmiles(Chem.RemoveHs(mol)) == (
        "O=c1cc(N2CCOCC2)nc2n1CCC(C(F)(F)F)N2CC(O)c1ccccc1"
    )

    rich = cocrystal_ligand_descriptors(pdb, source_file=cif, resname="JXM")
    poor = cocrystal_ligand_descriptors(pdb)
    assert rich["mw"] == pytest.approx(424.42, abs=0.01)
    assert rich["aromatic_ring_count"] == 2
    assert set(poor) == set(PDB_SAFE_DESCRIPTORS)  # same file, no CIF: the honest five


def test_cif_without_a_matching_component_falls_back(monkeypatch, tmp_path):
    from amdockvs.io.ccd_bonds import ligand_from_cif

    monkeypatch.setenv("AMDOCK_OFFLINE", "1")  # no CCD lookup: this pins the PDB-only fallback

    cif = Path(__file__).parent / "data" / "ccd_ligand.cif"
    pdb = Path(__file__).parent / "data" / "ccd_ligand.pdb"
    if not cif.exists():
        pytest.skip("mmCIF fixture not present")
    assert ligand_from_cif(cif, pdb, "NOPE") is None
    assert set(cocrystal_ligand_descriptors(pdb, source_file=cif, resname="NOPE")) == set(
        PDB_SAFE_DESCRIPTORS
    )

def test_pdbqt_round_trips_through_meeko(tmp_path):
    """Every PDBQT we write (prepared ligands, docking poses) carries meeko's REMARK SMILES."""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    from amdockvs.io.transformers.structures import _load_small_molecule_from_path

    meeko = pytest.importorskip("meeko")
    mol = Chem.AddHs(Chem.MolFromSmiles(SMILES[1]))
    assert AllChem.EmbedMolecule(mol, randomSeed=0xF00D) == 0
    setups = meeko.MoleculePreparation()(mol)
    text, ok, err = meeko.PDBQTWriterLegacy.write_string(setups[0])
    assert ok, err
    path = tmp_path / "lig.pdbqt"
    path.write_text(text)

    back = _load_small_molecule_from_path(path)
    assert back is not None
    assert Chem.MolToSmiles(Chem.RemoveHs(back)) == Chem.MolToSmiles(Chem.RemoveHs(mol))


def test_ccd_lookup_is_offline_safe(monkeypatch, tmp_path):
    """AMDOCK_OFFLINE and an unknown code must not reach the network or raise."""
    from amdockvs.io.ccd_bonds import ccd_component_file

    monkeypatch.setenv("AMDOCK_CCD_CACHE", str(tmp_path))
    monkeypatch.setenv("AMDOCK_OFFLINE", "1")
    assert ccd_component_file("JXM") is None
    assert ccd_component_file("") is None
    assert ccd_component_file("../etc/passwd") is None

    (tmp_path / "JXM.cif").write_text("data_JXM\n")  # a cached copy is used without fetching
    assert ccd_component_file("JXM") == tmp_path / "JXM.cif"
