"""Search-box alerts offer a fix while retaining preparation and docking skip semantics."""
from types import SimpleNamespace

import pytest
from ms_flow.query import db_pages

from amdockvs.docking.preparation.jobs import scope_spec
from amdockvs.models import BindingSite, MoleculeRecord, ReceptorPrep
from amdockvs.ui.tools.binding_sites.box import GridBoxSettingDockWidget
from test_receptor_missing_3d import _answer_dialog, _settle, receptor_studio  # noqa: F401 - shared fixture


@pytest.fixture
def box_scene(receptor_studio, monkeypatch):
    scene = receptor_studio
    store = scene.runtime.workspace.project_db
    payload = scene.runtime.molecules.get(scene.ids[0]).current_payload_id
    with store.get_session() as session:
        rows = [MoleculeRecord(name=name, molecule_type="protein", is_receptor=True,
                               has_3d=True, current_payload_id=payload)
                for name in ("no box", "inactive box", "undefined box", "outside scope")]
        session.add_all(rows)
        session.commit()
        for row in rows:
            session.refresh(row)
        scene.no_box, scene.inactive, scene.undefined, scene.outside = [row.id for row in rows]
        session.add(BindingSite(molecule_id=scene.inactive, name="Saved box", is_active=False,
                                center_x=0, center_y=0, center_z=0, size_x=20, size_y=20, size_z=20))
        session.add(BindingSite(molecule_id=scene.undefined, name="Incomplete box", is_active=True))
        session.commit()
    scene.selected = [scene.ids[0], scene.ids[1], scene.no_box, scene.inactive, scene.undefined]
    scene.window.selection.set("receptor", scene.selected)
    monkeypatch.setattr(scene.studio, "_ask_missing_receptor_3d", lambda _count: "continue")
    return scene


def _mark_prepared(scene):
    payload = scene.runtime.molecules.get(scene.ids[0]).current_payload_id
    with scene.runtime.workspace.project_db.get_session() as session:
        for molecule_id in [scene.ids[0], scene.no_box, scene.inactive, scene.undefined]:
            session.add(ReceptorPrep(receptor_molecule_id=molecule_id, family="ad4", rigid_payload_id=payload))
        session.commit()


def test_continue_counts_missing_inactive_and_undefined_boxes_but_prepares_all_3d(box_scene):
    scene = box_scene
    seen = _answer_dialog(scene, "Continue and skip", screenshot="/tmp/amdockvs-receptor-missing-box.png")
    _settle(scene, scene.studio._prepare_receptors)
    assert len(seen) == 1 and "3 receptor(s)" in seen[0][0]
    assert "skipped during docking" in seen[0][0]
    assert scene.submitted and scene.studio.stepper.current_index == 3
    for params in scene.submitted:
        assert {row["id"] for row in db_pages(scene.runtime.workspace.project_db,
                                              scope_spec(params, entity_kind="receptor"))} == {
            scene.ids[0], scene.no_box, scene.inactive, scene.undefined,
        }
    _mark_prepared(scene)
    readiness = scene.studio.readiness_service.receptors(scene.studio._selected_receptor_scope(), program="vina")
    assert readiness.ready_ids == (scene.ids[0],)
    assert scene.window.selection.get("receptor") == scene.selected
    assert all(not scene.runtime.molecules.get(i).excluded for i in scene.selected)


@pytest.mark.parametrize("existing", [False, True])
def test_fix_opens_box_controls_for_an_affected_receptor_without_activating_it(box_scene, monkeypatch, existing):
    scene = box_scene
    target = scene.inactive if existing else scene.no_box
    scene.window.selection.set("receptor", [scene.ids[0], target])
    grid = GridBoxSettingDockWidget(runtime=scene.runtime, parent=scene.window)
    monkeypatch.setattr(scene.window, "grid_dock", grid)
    opened = []
    monkeypatch.setattr(scene.window, "pymol_dock", SimpleNamespace(
        cmd=None, set_side_panel_visible=lambda visible: (opened.append(visible), grid.setVisible(visible))))
    seen = _answer_dialog(scene, "Define or activate box…")
    _settle(scene, scene.studio._prepare_receptors)
    assert len(seen) == 1 and "1 receptor(s)" in seen[0][0]
    assert not scene.submitted and not scene.followed
    assert scene.studio.stepper.current_index == 2
    assert scene.studio._focused_receptor_id == target
    assert grid._current_molecule.id == target and grid.isVisible() and opened[-1] is True
    assert scene.window.selection.get("receptor") == [scene.ids[0], target]
    assert scene.runtime.binding_sites.active_site_id(target) is None
    if existing:
        assert grid._selected_site().name == "Saved box"
        assert grid.activate_button.isEnabled()


def test_a_defined_active_box_does_not_show_an_alert(box_scene, monkeypatch):
    scene = box_scene
    scene.window.selection.set("receptor", [scene.ids[0]])
    monkeypatch.setattr(scene.studio, "_ask_missing_receptor_box",
                        lambda *a, **k: pytest.fail("An active, defined box needs no alert"))
    _settle(scene, scene.studio._prepare_receptors)
    assert scene.submitted


def test_escape_keeps_preparation_available_when_no_receptor_has_a_box(box_scene):
    scene = box_scene
    scene.window.selection.set("receptor", [scene.no_box, scene.inactive])
    seen = _answer_dialog(scene, "escape")
    _settle(scene, scene.studio._prepare_receptors)
    assert len(seen) == 1 and "2 receptor(s)" in seen[0][0] and scene.submitted
    _mark_prepared(scene)
    assert scene.studio.readiness_service.receptors(
        scene.studio._selected_receptor_scope(), program="vina").ready_ids == ()


@pytest.mark.parametrize("action,launches", [("Continue and skip", True), ("Define or activate box…", False)])
def test_run_checks_boxes_even_when_preparation_is_already_done(box_scene, monkeypatch, action, launches):
    from amdockvs.ui.tools.docking import run_panel

    scene = box_scene
    scene.studio.stepper.set_current_index(3)
    inp = {"sel_rec": scene.selected}
    params = {"run_kind": "docking", "protocols": [{"program": "vina"}]}
    monkeypatch.setattr(scene.studio, "_docking_params", lambda: (inp, params))
    monkeypatch.setattr(scene.studio, "_preparing", lambda: [])
    monkeypatch.setattr(scene.studio, "_docking_signature", lambda *_args: "test")
    monkeypatch.setattr(scene.studio, "_check_docking_conflict", lambda _sig: ("none", ""))
    submitted = []
    monkeypatch.setattr(scene.studio, "_after_conflict_check", lambda *args: submitted.append(args))
    monkeypatch.setattr(run_panel, "run_async", lambda work, done, **kwargs: done(work()))
    seen = _answer_dialog(scene, action)
    _settle(scene, scene.studio._run_docking)
    assert len(seen) == 1 and bool(submitted) is launches
    assert scene.window.selection.get("receptor") == scene.selected
    assert scene.studio.stepper.current_index == (3 if launches else 2)
