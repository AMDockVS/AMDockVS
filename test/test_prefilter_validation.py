"""An import filter that cannot mean what it says is refused before any job is queued.

Worth a test because each of these used to run to "completed": an unknown descriptor or a NaN
bound rejects every molecule, a reversed range keeps none, and an unknown activity transform or
unit loads no activity at all. Nothing told the user why the library came out empty.
"""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("rdkit")

from amdockvs.io.api import LoaderAPI
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


class _Loader(LoaderAPI):
    """Everything up to the job submission is real; the submission is recorded instead."""

    def __init__(self):
        super().__init__(SimpleNamespace(
            _require_active_project=lambda: None,
            workspace=SimpleNamespace(project_db=None),
            get_project_resource_path=lambda _name: None,
        ))
        self.submitted = []

    def _import_generation_id(self):
        return 1

    def _batches(self, files, **_):
        return {}

    def _temporary(self, files, temporary_files):
        return {}

    def _submit_job(self, job_def, *, params, **_):
        self.submitted.append(params)
        return "job"


@pytest.fixture
def sdf(tmp_path):
    path = tmp_path / "one.sdf"
    path.write_text("ethanol\n\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n$$$$\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("route", ["load_ligands", "load_molecules", "shard_ligands"])
@pytest.mark.parametrize("policy,message", BAD[:1] + BAD[3:5])
def test_the_api_refuses_it_before_queuing_a_job(sdf, route, policy, message):
    loader = _Loader()
    with pytest.raises(ValueError, match=message):
        getattr(loader, route)([sdf], prefilter=policy)
    assert loader.submitted == []


@pytest.mark.parametrize("route", ["load_ligands", "shard_ligands"])
def test_the_api_still_queues_a_sound_filter(sdf, route):
    loader = _Loader()
    getattr(loader, route)([sdf], prefilter=GOOD[0])
    assert loader.submitted and loader.submitted[0]["prefilter"] == GOOD[0]


def test_the_dialog_form_reports_a_reversed_weight_range(qapp):
    from amdockvs.ui.tools.importing.ligand_forms import ImportFilterCriteriaForm, finalize_import_prefilter_policy

    form = ImportFilterCriteriaForm()
    form.mw_min.setValue(300)
    form.mw_max.setValue(100)
    policy = {"target_molecule_kinds": ["small_molecule"]}
    form.contribute(policy)
    with pytest.raises(ValueError, match="minimum 300 is above the maximum 100"):
        finalize_import_prefilter_policy(policy)


@pytest.fixture
def qapp():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
