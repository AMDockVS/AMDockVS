"""A biological assembly keeps every copy of the chains the user selected.

The chain and component choices name the file's own chains, but gemmi renames the copies it
builds (the translated copy of A comes out as "B"), so they have to be applied before the
assembly is built, not filtered by name afterwards.
"""
from __future__ import annotations

import gemmi
import pytest

from amdockvs.io.receptor_preview import (
    ReceptorImportOptions,
    scan_receptor_structure,
    write_processed_receptor,
)

_ASSEMBLY = """\
REMARK 350 BIOMOLECULE: 1
REMARK 350 APPLY THE FOLLOWING TO CHAINS: A
REMARK 350   BIOMT1   1  1.000000  0.000000  0.000000        0.00000
REMARK 350   BIOMT2   1  0.000000  1.000000  0.000000        0.00000
REMARK 350   BIOMT3   1  0.000000  0.000000  1.000000        0.00000
REMARK 350   BIOMT1   2  1.000000  0.000000  0.000000       80.00000
REMARK 350   BIOMT2   2  0.000000  1.000000  0.000000        0.00000
REMARK 350   BIOMT3   2  0.000000  0.000000  1.000000        0.00000"""


def _atom(serial, name, resname, chain, resseq, x, y, element, record="ATOM"):
    return (f"{record:<6}{serial:>5} {name:<4} {resname:>3} {chain}{resseq:>4}    "
            f"{x:>8.3f}{y:>8.3f}{0.0:>8.3f}  1.00 20.00          {element:>2}")


def _receptor(path):
    """Gly chains A and B, ZN 301 on A; assembly 1 is chain A plus a copy 80 Å along x."""
    lines, serial = [_ASSEMBLY], 1
    for chain, y in (("A", 0.0), ("B", 20.0)):
        for resseq in range(1, 4):
            for name, element, dx in (("N", "N", 0.0), ("CA", "C", 1.5), ("C", "C", 3.0), ("O", "O", 4.0)):
                lines.append(_atom(serial, name, "GLY", chain, resseq, 4 * resseq + dx, y, element))
                serial += 1
    lines.append(_atom(serial, "ZN", "ZN", "A", 301, 6.0, 3.0, "ZN", "HETATM"))
    path.write_text("\n".join(lines + ["END", ""]))
    return path


def _write(tmp_path, **options):
    source = _receptor(tmp_path / "rec.pdb")
    out = tmp_path / "out.pdb"
    write_processed_receptor(source, out, scan=scan_receptor_structure(source), options=ReceptorImportOptions(**options))
    return gemmi.read_structure(str(out))[0]


def _first_x(chain):
    return chain[0][0].pos.x


@pytest.mark.parametrize("chains", [("A",), ()])
def test_assembly_keeps_the_copy_of_the_selected_chain(tmp_path, chains):
    model = _write(tmp_path, selected_assembly="1", selected_chain_ids=chains)

    assert len(model) == 2
    assert [sum(1 for res in chain if res.name == "GLY") for chain in model] == [3, 3]
    assert _first_x(model[1]) - _first_x(model[0]) == pytest.approx(80.0)


def test_assembly_copy_drops_a_metal_taken_off_the_source_chain(tmp_path):
    model = _write(tmp_path, selected_assembly="1", selected_chain_ids=("A",), selected_metals=())

    assert len(model) == 2
    assert not [res for chain in model for res in chain if res.name == "ZN"]


def test_assembly_copy_keeps_a_kept_metal(tmp_path):
    model = _write(tmp_path, selected_assembly="1", selected_chain_ids=("A",))

    assert [sum(1 for res in chain if res.name == "ZN") for chain in model] == [1, 1]


def test_asymmetric_unit_chain_selection_is_unchanged(tmp_path):
    model = _write(tmp_path, use_biological_assembly=False, selected_chain_ids=("B",))

    assert [chain.name for chain in model] == ["B"]
    assert _first_x(model[0]) == pytest.approx(4.0)


def test_assembly_without_any_selected_chain_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="none of the selected chains"):
        _write(tmp_path, selected_assembly="1", selected_chain_ids=("B",))


def test_dimer_assembly_with_one_chain_deselected_builds_from_the_rest(tmp_path):
    source = _receptor(tmp_path / "rec.pdb")
    source.write_text(source.read_text().replace("CHAINS: A", "CHAINS: A, B"))
    out = tmp_path / "out.pdb"
    options = ReceptorImportOptions(selected_assembly="1", selected_chain_ids=("A",))
    write_processed_receptor(source, out, scan=scan_receptor_structure(source), options=options)
    model = gemmi.read_structure(str(out))[0]

    assert len(model) == 2  # A and its translated copy, nothing from B
    assert {round(chain[0][0].pos.y, 1) for chain in model} == {0.0}


def test_mmcif_assembly_keeps_both_copies_of_the_selected_chain(tmp_path):
    from test_cif_assembly_chains import CIF

    two_copies = CIF.replace("1 1 A,B,C", "1 '(1,2)' A,B,C").replace(
        "1 'identity operation' 1 0 0 0 0 1 0 0 0 0 1 0",
        "1 'identity operation' 1 0 0 0 0 1 0 0 0 0 1 0\n"
        "2 'translation' 1 0 0 80 0 1 0 0 0 0 1 0",
    )
    source = tmp_path / "t.cif"
    source.write_text(two_copies)
    out = tmp_path / "out.cif"
    options = ReceptorImportOptions(selected_assembly="1", selected_chain_ids=("A",))
    write_processed_receptor(source, out, scan=scan_receptor_structure(source), options=options)
    model = gemmi.read_structure(str(out))[0]

    ala = [residue[0].pos.x for chain in model for residue in chain if residue.name == "ALA"]
    assert sorted(round(x - min(ala), 1) for x in ala) == [0.0, 80.0]
    assert not [residue for chain in model for residue in chain if residue.name == "JXM"]  # ligand removed in both
