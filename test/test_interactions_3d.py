"""A residue picked in the diagram is drawn in PyMOL: one distance per contact, at the 3D length."""
import pytest

pymol2 = pytest.importorskip("pymol2")
from rdkit import Chem  # noqa: E402
from rdkit.Chem import AllChem  # noqa: E402

from ms_contactmap.model import Diagram, Interaction, Residue, ResidueRef  # noqa: E402

from amdockvs.ui.viewers.interactions_3d import GROUP, show_interaction, show_residue_interactions  # noqa: E402

_SER = """\
ATOM      1  N   SER A  10       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  SER A  10       1.450   0.000   0.000  1.00  0.00           C
ATOM      3  CB  SER A  10       2.000   1.400   0.000  1.00  0.00           C
ATOM      4  OG  SER A  10       3.400   1.400   0.000  1.00  0.00           O
END
"""


def test_contacts_of_the_picked_residue_are_drawn_at_their_3d_length(tmp_path):
    ligand = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(ligand, randomSeed=7)
    conf = ligand.GetConformer()
    shift = [6.2 - conf.GetAtomPosition(2).x, 1.4 - conf.GetAtomPosition(2).y, -conf.GetAtomPosition(2).z]
    for i in range(ligand.GetNumAtoms()):  # the hydroxyl O lands 2.8 Å from OG
        p = conf.GetAtomPosition(i)
        conf.SetAtomPosition(i, (p.x + shift[0], p.y + shift[1], p.z + shift[2]))
    (tmp_path / "rec.pdb").write_text(_SER)
    Chem.MolToMolFile(ligand, str(tmp_path / "pose.sdf"))  # with H: PyMOL must skip them

    key = "A:10:SER"
    diagram = Diagram("t", "LIG", Chem.RemoveHs(ligand), [(0.0, 0.0)] * 3,
                      residues=[Residue(ResidueRef("A", 10, "SER"), has_interactions=True)],
                      interactions=[Interaction("hbond", key, (2,), 2.8, protein_atom="OG"),
                                    Interaction("hydrophobic", key, (0, 1), 4.0),
                                    Interaction("hbond", "A:99:ASP", (2,), 3.0)])
    session = pymol2.PyMOL()
    session.start()
    cmd = session.cmd
    try:
        cmd.load(str(tmp_path / "rec.pdb"), "receptor_1")
        cmd.load(str(tmp_path / "pose.sdf"), "pose")
        for _picked_again in range(2):  # the previous drawing is replaced, not added to
            assert show_residue_interactions(cmd, receptor_obj="receptor_1", ligand_obj="pose",
                                             key=key, diagram=diagram) == 2
        points = [n for n in cmd.get_names("objects") if n.startswith(f"{GROUP}_p")]
        assert round(cmd.get_distance(points[0], points[1]), 1) == 2.8
        assert GROUP in cmd.get_names("all")

        # A contact picked in the interaction table: just that one, in its type's colour.
        from amdockvs.ui.catalog.results import _table_contact

        row = {"interaction_type": "hydrogen_bond", "residue": "SER10:A", "distance": 2.8,
               "geometry": {"provider_interaction": "hbond",
                            "provider_payload": {"ligand_atoms": [2], "protein_atom": "OG"}}}
        contact = _table_contact(row)
        assert contact.residue_key == key  # parsed from the residue: this row has no key saved
        assert show_interaction(cmd, receptor_obj="receptor_1", ligand_obj="pose", contact=contact) == 1
        (dash,) = [n for n in cmd.get_names("objects") if n.startswith(f"{GROUP}_d")]
        assert cmd.get("dash_color", dash) != cmd.get("dash_color")  # its own colour, not the default
        points = [n for n in cmd.get_names("objects") if n.startswith(f"{GROUP}_p")]
        assert round(cmd.get_distance(points[0], points[1]), 1) == 2.8
    finally:
        session.stop()
