"""The Include/Exclude SMARTS fields: a comma separates patterns only outside an atom.

Worth a test because ``[N,O]`` (N or O) is a valid atom list; split on its comma it becomes
``[N`` and ``O]`` and the whole import job fails on an invalid pattern.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("PySide6")
Chem = pytest.importorskip("rdkit.Chem")

from amdockvs.ui.tools.importing.ligand_forms import _smarts_list


@pytest.mark.parametrize(
    "text,patterns",
    [
        ("[N,O]", ["[N,O]"]),
        ("[N,O], c1ccccc1", ["[N,O]", "c1ccccc1"]),
        ("C(=O)[O,N],[#7,#8;!H0]", ["C(=O)[O,N]", "[#7,#8;!H0]"]),
        ("[$(C=O),$(S=O)],Cl", ["[$(C=O),$(S=O)]", "Cl"]),
        ("c1ccccc1, ,CCO,", ["c1ccccc1", "CCO"]),
        ("", []),
    ],
)
def test_commas_inside_an_atom_stay_in_the_pattern(text, patterns):
    assert _smarts_list(text) == patterns
    assert all(Chem.MolFromSmarts(pattern) is not None for pattern in patterns)


def test_a_malformed_pattern_still_reaches_validation_whole():
    # Unbalanced: nothing to split on, so the job's own check reports the pattern as typed.
    assert _smarts_list("[N,O") == ["[N,O"]
