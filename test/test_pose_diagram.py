"""Pose diagram plumbing: file naming, the saved document, and the interaction vocabulary."""
from pathlib import Path

from amdockvs.docking.results.diagram import diagram_path_for, load_pose_diagram, save_pose_diagram
from amdockvs.docking.results.interactions import _interaction_type


def test_diagram_path_convention() -> None:
    assert diagram_path_for("/x/pose_1.pdbqt", 2).name == "pose_1.pdbqt.rank2.diagram.png"
    assert diagram_path_for("/x/p.sdf", suffix=".svg").name == "p.sdf.rank1.diagram.svg"


def test_saved_document_roundtrip(tmp_path: Path) -> None:
    # A minimal but real Diagram/LayoutResult, so the test fails if ms_contactmap's schema and
    # what the viewer reads back drift apart.
    from rdkit import Chem
    from rdkit.Chem import AllChem

    from ms_contactmap import Diagram, LayoutResult

    mol = Chem.MolFromSmiles("CCO")
    AllChem.Compute2DCoords(mol)
    xy = [(p.x, p.y) for p in (mol.GetConformer().GetAtomPosition(i) for i in range(3))]
    diagram = Diagram(name="d", ligand_name="LIG", mol=mol, coords_2d=xy)
    layout = LayoutResult({}, xy, 0.0, False, 0.0, {}, 0)
    pose = tmp_path / "pose.sdf"
    assert save_pose_diagram(pose, 2, diagram, layout).name == "pose.sdf.rank2.diagram.json"
    loaded, loaded_layout = load_pose_diagram(pose, 2)
    assert loaded.ligand_name == "LIG" and loaded.mol.GetNumAtoms() == 3
    assert loaded_layout.ligand_coords == [tuple(p) for p in xy]
    assert load_pose_diagram(pose, 7) is None  # never built


def test_interaction_vocabulary() -> None:
    # ms_contactmap's vocabulary reaches the table intact; only these two are renamed.
    assert _interaction_type("pi_stacking") == "pi_stacking"
    assert _interaction_type("hbond") == "hydrogen_bond"
    assert _interaction_type("metal_coordination") == "metal_complex"
