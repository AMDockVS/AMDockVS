"""A bare metal ion carries its oxidation state, and says what that does not buy.

Gasteiger has no parameters for a metal, so it reached the grid at 0 and no ligand could feel
it. The charge is the cheaper of two wrongs; the expensive part is that only zinc has a
coordination model behind it, so every other metal has to say so.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from amdockvs.docking.preparation.cofactors import bundled_components
from amdockvs.docking.preparation.metals import apply_formal_charges, formal_charge


def row(resname: str, atom: str, element: str, resseq: int, charge: float = 0.0,
        chain: str = "A"):
    """One PDBQT atom line, by the columns the parser actually reads."""
    line = [" "] * 79
    line[0:6] = list("HETATM")
    line[12:16] = list(f"{atom:<4}")
    line[17:20] = list(f"{resname:>3}")
    line[21] = chain
    line[22:26] = list(f"{resseq:>4}")
    line[30:54] = list(f"{1.0:>8.3f}{2.0:>8.3f}{3.0:>8.3f}")
    line[70:76] = list(f"{charge:>6.3f}")
    line[77:79] = list(f"{element:>2}")
    return "".join(line)


def charges_of(text: str, element: str) -> list[float]:
    return [float(l[70:76]) for l in text.splitlines()
            if l.startswith(("ATOM", "HETATM")) and l[77:79].strip() == element]


@pytest.mark.parametrize("code,expected", [("ZN", 2), ("MG", 2), ("CA", 2), ("MN", 2),
                                           ("FE", 3), ("FE2", 2), ("CU", 2), ("CU1", 1)])
def test_the_ccd_decides_the_oxidation_state(code, expected):
    """The entry's own code picks the state: FE is Fe(III) where FE2 is Fe(II)."""
    pytest.importorskip("ms_adtools")
    assert code in bundled_components(), f"{code} must ship, or an offline run cannot charge it"
    assert formal_charge(code) == pytest.approx(expected)


def test_a_bare_ion_is_given_its_charge(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    pytest.importorskip("ms_adtools")
    from amdockvs.docking.preparation.cofactors import seed_component_cache
    seed_component_cache()

    text = "\n".join([row("GLY", "CA", "C", 1, -0.1), row("ZN", "ZN", "Zn", 101, 0.0)])
    out, applied, warnings = apply_formal_charges(text)
    assert applied == {"ZN": 2.0}
    assert charges_of(out, "Zn") == [2.0]
    assert charges_of(out, "C") == [-0.1], "a protein atom is left alone"
    assert not warnings, "zinc has AutoDock4Zn, so it is not the one to warn about"


def test_every_metal_but_zinc_says_it_has_no_coordination_model(monkeypatch, tmp_path):
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    pytest.importorskip("ms_adtools")
    from amdockvs.docking.preparation.cofactors import seed_component_cache
    seed_component_cache()

    text = "\n".join([row("MG", "MG", "Mg", 201), row("CA", "CA", "Ca", 202)])
    _, applied, warnings = apply_formal_charges(text)
    assert applied == {"MG": 2.0, "CA": 2.0}
    [warning] = warnings
    assert "AutoDock4Zn" in warning
    assert "MG" in warning and "CA" in warning


def test_a_metal_inside_a_component_is_not_touched_here(monkeypatch, tmp_path):
    """Heme's iron is the CCD typing path's business; charging it twice would be +4."""
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    pytest.importorskip("ms_adtools")

    # one HEM residue holding its iron and a porphyrin nitrogen: more than one atom, so the
    # residue is a component, not a bare ion
    text = "\n".join([row("HEM", "FE", "Fe", 154, 2.0), row("HEM", "NA", "NA", 154, -0.3)])
    out, applied, warnings = apply_formal_charges(text)
    assert applied == {}
    assert out == text
    assert not warnings


def test_two_ions_of_one_element_are_each_a_residue(monkeypatch, tmp_path):
    """Counting atoms by residue name instead of by residue would make a pair look molecular."""
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_OFFLINE", "1")
    pytest.importorskip("ms_adtools")
    from amdockvs.docking.preparation.cofactors import seed_component_cache
    seed_component_cache()

    text = "\n".join([row("ZN", "ZN", "Zn", 301), row("ZN", "ZN", "Zn", 302),
                      row("ZN", "ZN", "Zn", 303, chain="B")])
    out, applied, _ = apply_formal_charges(text)
    assert applied == {"ZN": 2.0}
    assert charges_of(out, "Zn") == [2.0, 2.0, 2.0]


def test_a_receptor_with_no_metal_is_returned_unchanged():
    text = "\n".join([row("GLY", "CA", "C", 1, -0.1), row("GLY", "N", "N", 1, -0.3)])
    out, applied, warnings = apply_formal_charges(text)
    assert (out, applied, warnings) == (text, {}, [])
