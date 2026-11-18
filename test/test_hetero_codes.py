"""The hetero code lists have to be true to be useful, so the rules are pinned here.

A code in these lists is a claim about a residue name, and a wrong claim is silent: it
misclassifies a component at import, and nothing downstream says so. Two were wrong for a long
time -- HEME, which is not a CCD component and is too long to match anything, and I, which the
CCD calls INOSINIC ACID rather than iodide. These tests are what makes that class of mistake
loud. The CCD check is opt-in because it needs the network; the rest run always.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from amdockvs.io.receptor_preview import hetero_codes

CODES_FILE = Path(__file__).resolve().parents[1] / "src" / "amdockvs" / "data" / "hetero_codes.json"
RAW = json.loads(CODES_FILE.read_text())
CLASSES = [key for key in RAW if not key.startswith("_")]


def test_the_classes_are_the_ones_the_code_asks_for():
    # receptor_preview and docking.preparation.entities both index these by name.
    # "cofactor" is not in the file: it is ms_adtools' template list (see the last test).
    assert set(CLASSES) == {"water", "coordination_metal", "ion", "additive"}


@pytest.mark.parametrize("group", CLASSES)
def test_every_code_can_actually_match_a_residue_name(group):
    """PDB columns 18-20 hold 3 characters, so a longer code is unreachable by construction."""
    too_long = [code for code in RAW[group] if len(code) > 3]
    assert not too_long, f"{group}: {too_long} can never match a residue name"


@pytest.mark.parametrize("group", CLASSES)
def test_codes_are_upper_case_and_unique(group):
    codes = RAW[group]
    assert codes == [code.upper() for code in codes]
    assert len(codes) == len(set(codes)), f"{group} repeats a code"


def test_no_code_is_claimed_by_two_classes():
    """A residue has one class; _classify_component returns on the first list that matches, so an
    overlap would make the order of those checks the real answer."""
    from ms_adtools.templates import COFACTORS

    seen = dict.fromkeys(COFACTORS, "cofactor")
    for group in CLASSES:
        for code in RAW[group]:
            assert code not in seen, f"{code} is in both {seen.get(code)} and {group}"
            seen[code] = group


def test_the_corrected_codes_stay_out():
    """Both were plausible-looking and both were wrong; _corrected says why. Keep them gone."""
    for code, reason in RAW["_corrected"].items():
        assert reason, f"{code} needs its reason recorded"
        for group in CLASSES:
            assert code not in RAW[group], f"{code} was removed on purpose: {reason}"


def test_the_standard_amino_acids_are_not_in_any_list():
    # A polymer residue classified as a hetero component would be stripped from the receptor.
    amino = {"ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE", "LEU",
             "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL", "MSE"}
    for group in CLASSES:
        assert not (set(RAW[group]) & amino), f"{group} claims a standard residue"


def test_hetero_codes_skips_the_notes():
    """The _-prefixed keys are documentation; the loader must not offer them as a class."""
    assert hetero_codes("_comment") == frozenset()
    assert hetero_codes("_corrected") == frozenset()
    assert hetero_codes("cofactor")  # and the real ones still load


@pytest.mark.skipif(not os.environ.get("AMDOCK_VERIFY_CCD"),
                    reason="set AMDOCK_VERIFY_CCD=1 to check every code against the RCSB")
@pytest.mark.parametrize("group", CLASSES)
def test_every_code_resolves_to_the_component_it_claims(group):
    """Opt-in: the CCD is the authority on what a code means, so ask it.

    Anything in _not_ccd is exempt and says why it is there.
    """
    pytest.importorskip("ms_adtools")
    from ms_adtools.ccd import component_file

    exempt = set(RAW.get("_not_ccd") or {})
    unresolved = [code for code in RAW[group]
                  if code not in exempt and component_file(code) is None]
    assert not unresolved, f"{group}: {unresolved} are not CCD components and are not in _not_ccd"


def test_cofactors_are_the_ones_preparation_can_type():
    """One list: import keeps as a cofactor exactly what ms_adtools has a template for, so
    "Include cofactors" never keeps a residue it would type by geometry alone."""
    from ms_adtools.templates import COFACTORS

    from amdockvs.io.receptor_preview import _classify_component

    assert hetero_codes("cofactor") == COFACTORS
    assert {_classify_component(code, ["C", "N", "O"]) for code in COFACTORS} == {"cofactor"}
    assert _classify_component("PLP", ["C", "N", "O"]) == "ligand"  # no template yet: see TODO.md
