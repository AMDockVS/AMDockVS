"""T1: a warning says which structure it is about, not just how many there are."""
from types import SimpleNamespace

import pytest
from pydantic import AliasChoices, BaseModel, Field  # noqa: F401 - preloaded before amdockvs (import cycle)

import amdockvs.ui.catalog  # noqa: F401 - imported first, it is what breaks the ui import cycle
from amdockvs.docking.readiness import EntityReadiness
from amdockvs.ui.common.naming import about, name_list
from amdockvs.ui.tools.docking.run_panel import RunPanel


@pytest.mark.parametrize("names,total,expected", [
    (["4OYS"], None, "4OYS"),
    (["4OYS", "4UWF"], None, "4OYS and 4UWF"),
    (["4OYS", "4UWF", "4UWG"], None, "4OYS, 4UWF and 4UWG"),
    (["4OYS", "4UWF", "4UWG"], 5, "4OYS, 4UWF, 4UWG and 2 more"),
    ([], 3, ""),  # nothing to name: the caller falls back to the count alone
    (["", "  "], None, ""),
])
def test_name_list_names_the_first_few_and_counts_the_rest(names, total, expected):
    assert name_list(names, total) == expected


def test_about_names_one_structure_and_counts_several():
    assert about(1, ["4OYS"], "receptor", "has no binding site") == "4OYS has no binding site."
    assert about(3, ["4OYS", "4UWF"], "receptor", "has no binding site") == (
        "3 receptors have no binding site: 4OYS, 4UWF and 1 more.")
    # No name to give (an older call site, or a scope that could not be read): the count alone.
    assert about(2, [], "receptor", "has no binding site") == "2 receptors have no binding site."
    # A fragment to join into a longer note.
    assert about(1, ["4OYS"], "receptor", "has no binding site", end="") == "4OYS has no binding site"
    # A dialog has the room to put the names on their own line.
    assert about(2, ["4OYS", "4UWF"], "receptor", "has no binding site", sep=":\n") == (
        "2 receptors have no binding site:\n4OYS and 4UWF.")


def test_readiness_carries_the_receptors_that_have_no_binding_site():
    readiness = EntityReadiness(total=4, ready=3, no_site=1, no_site_ids=(7,))
    assert readiness.as_mapping()["no_site_ids"] == [7]
    assert EntityReadiness().as_mapping()["no_site_ids"] == []


def test_the_run_preview_names_the_receptor_it_leaves_out():
    """The one left out can be the target itself, which "1 receptor(s)" never showed."""
    panel = RunPanel()
    panel._check_inflight = True
    panel.runtime = SimpleNamespace(molecules=SimpleNamespace(
        get=lambda molecule_id: SimpleNamespace(name={1: "4OYS", 2: "4UWF"}[int(molecule_id)])))
    panel.req_ligands_count = SimpleNamespace(setText=lambda _text: None)
    panel.req_receptors_count = SimpleNamespace(setText=lambda _text: None)
    panel.req_tasks = SimpleNamespace(setText=lambda _text: None)
    panel._set_offtarget_receptors = lambda _receptors: None
    panel._set_sharded_run_mode = lambda *_args, **_kwargs: None
    panel.step_run = SimpleNamespace(set_done=lambda _value: None)
    panel._pairs_text = lambda _text: None
    statuses = []
    panel.check_status_label = SimpleNamespace(setText=statuses.append)

    def check(no_site, no_site_ids):
        panel._apply_check({
            "mode": "docking", "ready": True, "offtarget_reference_id": None,
            "ligands": {"ready": 1, "total": 1},
            "receptors": {"ready": 1, "total": 2, "no_site": no_site, "no_site_ids": no_site_ids},
            "pairs": {"total": 1, "already": 0, "to_run": 1},
        })
        return statuses[-1]

    assert check(1, [1]) == "4OYS has no binding site and will be left out."
    assert check(2, [1, 2]) == "2 receptors have no binding site and will be left out: 4OYS and 4UWF."
    assert check(0, []) == "Ready to run."
