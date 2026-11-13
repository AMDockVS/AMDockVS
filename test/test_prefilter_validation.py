"""An import filter that cannot mean what it says is refused.

Worth a test because each of these used to run to "completed": an unknown descriptor or a NaN
bound rejects every molecule, a reversed range keeps none, and an unknown activity transform or
unit loads no activity at all. Nothing told the user why the library came out empty.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("rdkit")

from amdockvs.io.payloads import validate_prefilter

BAD = [
    ({"property_ranges": {"mw": (300, 100)}}, "minimum 300 is above the maximum 100"),
    ({"property_ranges": {"mw": (float("nan"), 100)}}, "finite"),
    ({"property_ranges": {"mw": (100, float("inf"))}}, "finite"),
    ({"property_ranges": {"nonexistent_descriptor": (0, 1)}}, "unknown property 'nonexistent_descriptor'"),
    ({"activity_property": "IC50", "activity_transform": "nonsense"}, "not a pX label"),
    ({"activity_property": "IC50", "activity_transform": "pIC50", "activity_unit": "nanomolar"}, "cannot be converted"),
    ({"qsar_op": "nonsense", "qsar_threshold": 1.0}, "qsar_op"),
]

GOOD = [
    {"property_ranges": {"mw": (100, 200)}},
    {"property_ranges": {"mw": (150, 150), "logp": (None, 5), "tpsa": (20, None)}},
    {"activity_property": "IC50", "activity_transform": "pIC50", "activity_unit": "nM"},
    {"activity_property": "Kd", "activity_transform": "pKd", "activity_unit": "uM"},
    {"activity_property": "IC50"},  # no transform: the unit is free text
    {"qsar_op": "<=", "qsar_threshold": 1.0},
]


@pytest.mark.parametrize("policy,message", BAD)
def test_a_filter_that_cannot_mean_what_it_says_is_refused(policy, message):
    with pytest.raises(ValueError, match="Invalid import filter") as caught:
        validate_prefilter(policy)
    assert message in str(caught.value)


@pytest.mark.parametrize("policy", GOOD)
def test_a_sound_filter_is_accepted(policy):
    validate_prefilter(policy)

