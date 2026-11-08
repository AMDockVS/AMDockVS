"""Waters, cofactors and metals from import through to the receptor PDBQT.

Import keeps what its options say to keep; the preparation step decides what of that actually
reaches Meeko. Both halves used to be overridden by a blanket polymer+metal whitelist.
"""
import pytest

from amdockvs.docking.preparation.entities import receptor_excluded_resnames
from amdockvs.docking.preparation.meeko import _without_resnames

_BLOCK = "\n".join(
    [
        "ATOM      1  N   ASP A 285      -3.256  -3.631  -1.685  1.00  0.00           N",
        "HETATM    2 ZN    ZN A 900     -37.000 -35.000 -12.000  1.00 20.00          ZN",
        "HETATM    3  O   HOH A2001     -31.464 -37.112 -11.121  1.00 34.40           O",
        "HETATM    4  FE  HEM A 500     -30.000 -30.000 -10.000  1.00 20.00          FE",
        "END",
    ]
)


def _resnames(block: str) -> list[str]:
    return [line[17:20].strip() for line in block.splitlines() if line.startswith(("ATOM", "HETATM"))]


def test_dry_receptor_is_the_default_and_keeps_the_metal():
    excluded = receptor_excluded_resnames(keep_waters=False, keep_cofactors=False)

    assert _resnames(_without_resnames(_BLOCK, excluded)) == ["ASP", "ZN"]
    assert "ZN" not in excluded  # metals are part of the site, never optional


def test_waters_and_cofactors_ride_along_when_asked():
    both = receptor_excluded_resnames(keep_waters=True, keep_cofactors=True)
    waters_only = receptor_excluded_resnames(keep_waters=True, keep_cofactors=False)

    assert _resnames(_without_resnames(_BLOCK, both)) == ["ASP", "ZN", "HOH", "HEM"]
    assert _resnames(_without_resnames(_BLOCK, waters_only)) == ["ASP", "ZN", "HOH"]


def test_a_metal_meeko_cannot_type_is_left_out_even_when_keeping_everything():
    excluded = receptor_excluded_resnames(keep_waters=True, keep_cofactors=True)
    block = _BLOCK.replace("END", "HETATM    5 CD    CD A 701     -20.000 -20.000 -10.000  1.00 20.00          CD\nEND")

    assert _resnames(_without_resnames(block, excluded)) == ["ASP", "ZN", "HOH", "HEM"]


def test_a_receptor_meeko_refuses_names_the_missing_prody_extra(monkeypatch):
    """ProDy is an optional extra: without it the fallback says what to install."""
    import sys

    from meeko import Polymer

    from amdockvs.docking.preparation import meeko as meeko_prep

    def refuse(*_args, **_kwargs):
        raise ValueError("bad residue")

    monkeypatch.setattr(Polymer, "from_pdb_string", refuse)
    monkeypatch.setitem(sys.modules, "prody", None)
    with pytest.raises(RuntimeError, match=r"amdockvs\[prody\]"):
        meeko_prep._polymer_from_pdb("END\n", None, None)
