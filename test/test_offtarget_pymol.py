import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pydantic import AliasChoices, BaseModel, Field
import numpy
import pymol2
import pytest
from PySide6.QtWidgets import QApplication

from amdockvs.ui.tools.docking import offtarget
from amdockvs.ui.viewers.molecular_viewer import MolecularViewerController
from ms_components.ms_pymol import PymolControlBar


@pytest.fixture
def grid_scene(monkeypatch):
    app = QApplication.instance() or QApplication(["amdockvs-offtarget-pymol-test"])
    monkeypatch.setattr(offtarget.OffTargetResultsWidget, "refresh", lambda self: None)
    monkeypatch.setattr(offtarget, "payload_store", lambda runtime: object())
    monkeypatch.setattr(offtarget, "fetch_structures", lambda store, wanted: {
        name: ((f"HETATM    1  C1  LIG A   1    {payload_id:8.3f}   0.000   0.000"
                "  1.00  0.00           C\nEND\n").encode(), "pdb")
        for name, payload_id in wanted.items()
    })
    pending = []
    monkeypatch.setattr(offtarget, "run_async", lambda work, done, error: pending.append((work, done)))
    with pymol2.PyMOL() as session:
        cmd = session.cmd
        bar = PymolControlBar(cmd)

        def set_scene_context(*args, **kwargs):
            bar.set_scene_context(*args, **kwargs)
            dock.scene_context = bar.scene_context

        def replace_comparison_object(*args, **kwargs):
            result = bar.replace_comparison_object(*args, **kwargs)
            dock.scene_context = bar.scene_context
            return result

        dock = SimpleNamespace(cmd=cmd, show=lambda: None, set_scene_context=set_scene_context,
                               scene_context=bar.scene_context, set_comparison_layout=bar.set_comparison_layout,
                               replace_comparison_object=replace_comparison_object)
        widget = offtarget.OffTargetResultsWidget(runtime=object())
        monkeypatch.setattr(widget, "_pymol", lambda: (cmd, dock))
        widget._columns = [(1, "4UWF"), (2, "4UWH"), (3, "4UWG")]
        widget._names[741] = "4543"
        widget._poses[741] = {
            rid: [{
                "pose_rank": rank,
                "score": -10.0,
                "receptor_payload_id": rid,
                "pose_payload_id": rid * 10 + rank,
            } for rank in (1, 2)]
            for rid, name in widget._columns
        }
        widget._overrides[(741, 2)] = 2
        yield widget, cmd, dock, pending
        widget.close()
        bar.close()
        app.processEvents()


def _label(cmd, slot):
    labels = []
    cmd.iterate(f"panel_text{slot}", "labels.append(label)", space={"labels": labels})
    return labels


def test_multiple_ligand_grids_share_the_scene_and_keep_pose_replacement_local(grid_scene):
    widget, cmd, dock, pending = grid_scene
    viewer = MolecularViewerController(SimpleNamespace(pymol_dock=dock))
    viewer.set_multiple_selection_enabled(True)
    widget._poses[742] = widget._poses[741]
    widget._show_ligand_grid(741)
    widget._show_ligand_grid(742)
    for work, done in reversed(pending[:]):
        done(work())
    pending.clear()
    for ligand_id in (741, 742):
        for rid in (1, 2, 3):
            rec, lig = widget._grid_objects(dock, ligand_id, rid)
            assert rec in cmd.get_names("objects") and lig in cmd.get_names("objects")
            assert cmd.get_setting_int("grid_slot", rec) == cmd.get_setting_int("grid_slot", lig)
    assert set(dock.scene_context.grid_labels) == set(range(1, 7))
    widget._select_pose(741, 1, 2)
    work, done = pending.pop()
    done(work())
    assert cmd.get_coords("lig_1_ligand_741")[0][0] == 12
    assert cmd.get_coords("lig_1_ligand_742")[0][0] == 11

    widget._show_ligand_grid(741)
    work, done = pending.pop()
    viewer.clear_scene()
    done(work())
    assert cmd.get_names("objects") == []


def test_multiple_selection_keeps_a_grid_when_switching_to_a_nonstructural_tab(grid_scene):
    widget, cmd, dock, pending = grid_scene
    viewer = MolecularViewerController(SimpleNamespace(
        pymol_dock=dock, central_widget=SimpleNamespace(open_view=lambda _: None)))
    viewer.set_multiple_selection_enabled(True)
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())
    objects = cmd.get_names("objects")
    viewer.sync_to_active_view("other")
    assert cmd.get_names("objects") == objects
    assert cmd.get_setting_int("grid_mode") == 1


def test_offtarget_passes_receptor_ligand_and_selected_pose_names_to_pymol(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())

    assert _label(cmd, 1) == ["4UWF-4543[P1]"]
    assert _label(cmd, 2) == ["4UWH-4543[P2]"]
    assert _label(cmd, 3) == ["4UWG-4543[P1]"]
    assert cmd.get_setting_int("grid_mode") == 1


@pytest.mark.parametrize("missing", ["rec_2", "lig_2"])
def test_offtarget_label_slots_follow_only_the_complexes_actually_loaded(grid_scene, missing):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    found = work()
    del found[missing]
    done(found)

    assert _label(cmd, 1) == ["4UWF-4543[P1]"]
    assert _label(cmd, 2) == ["4UWG-4543[P1]"]
    assert "panel_text3" not in cmd.get_names("objects")
    assert "rec_2" not in cmd.get_names("objects")


def test_offtarget_names_keep_the_pose_snapshot_used_to_read_the_structures(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    widget._names[741] = "2895"
    widget._overrides.pop((741, 2))
    widget._columns.reverse()
    done(work())

    assert _label(cmd, 1) == ["4UWF-4543[P1]"]
    assert _label(cmd, 2) == ["4UWH-4543[P2]"]
    assert _label(cmd, 3) == ["4UWG-4543[P1]"]


def test_leaving_offtarget_for_a_nonstructural_view_notifies_pymol(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())
    viewer = MolecularViewerController(SimpleNamespace(
        pymol_dock=dock,
        central_widget=SimpleNamespace(open_view=lambda view_id: None),
    ))
    viewer.sync_to_active_view("other")

    assert cmd.get_setting_int("grid_mode") == 0
    assert cmd.get_names("objects") == []


@pytest.mark.parametrize("receptor_id", [1, 2, 3])
@pytest.mark.parametrize("layout", ["grid", "overlay"])
def test_changing_pose_replaces_only_its_ligand_and_keeps_the_grid_camera(grid_scene, monkeypatch, receptor_id, layout):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())
    dock.set_comparison_layout(layout)
    cmd.turn("y", 15)
    cmd.move("z", -5)
    cmd.set("auto_zoom", 1)
    view = cmd.get_view()
    objects = set(cmd.get_names("objects"))
    positions = {obj: cmd.get_coords(obj).copy() for obj in objects}
    deleted, loaded = [], []
    original_delete, original_load = cmd.delete, cmd.load_raw
    monkeypatch.setattr(cmd, "delete", lambda obj: (deleted.append(obj), original_delete(obj))[1])
    monkeypatch.setattr(cmd, "load_raw",
                        lambda data, fmt, name: (loaded.append(name), original_load(data, fmt, name))[1])

    index = 0 if receptor_id == 2 else 1
    widget._select_pose(741, receptor_id, index + 1)
    work, done = pending.pop()
    found = work()
    assert set(found) == {f"lig_{receptor_id}"}
    done(found)

    assert deleted == [f"lig_{receptor_id}"]
    assert len(loaded) == 1
    assert cmd.get_setting_int("grid_mode") == int(layout == "grid")
    assert cmd.get_setting_int("grid_slot", f"lig_{receptor_id}") == receptor_id
    assert cmd.get_setting_int("auto_zoom") == 1
    assert cmd.get_view() == pytest.approx(view)
    assert set(cmd.get_names("objects")) == objects
    for obj in objects - {f"lig_{receptor_id}"}:
        assert cmd.get_coords(obj) == pytest.approx(positions[obj])
    matrix = numpy.asarray(cmd.get_object_matrix(f"rec_{receptor_id}")).reshape(4, 4)
    expected = matrix @ [receptor_id * 10 + index + 1, 0, 0, 1]
    assert cmd.get_coords(f"lig_{receptor_id}")[0] == pytest.approx(expected[:3])
    receptor_name = dict(widget._columns)[receptor_id]
    assert _label(cmd, receptor_id) == [f"{receptor_name}-4543[P{index + 1}]"]
    assert cmd.count_atoms(f"panel_text{receptor_id}") == 1


def test_replacement_pose_inherits_the_displayed_receptor_alignment(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())
    cmd.translate([5, 6, 7], object="rec_2", camera=0)
    matrix = cmd.get_object_matrix("rec_2")
    widget._select_pose(741, 2, 1)
    work, done = pending.pop()
    done(work())

    assert cmd.get_object_matrix("lig_2") == pytest.approx(matrix)
    assert cmd.get_object_matrix("rec_2") == pytest.approx(matrix)


@pytest.mark.parametrize("shared_payload", [False, True])
def test_a_slow_old_pose_read_cannot_overwrite_the_latest_selection(grid_scene, shared_payload):
    widget, cmd, dock, pending = grid_scene
    if shared_payload:
        poses = widget._poses[741][1]
        poses[1]["pose_payload_id"] = poses[0]["pose_payload_id"]
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())
    widget._select_pose(741, 1, 2)
    older_work, older_done = pending.pop()
    widget._select_pose(741, 1, 1)
    latest_work, latest_done = pending.pop()
    latest_done(latest_work())
    older_done(older_work())

    assert _label(cmd, 1) == ["4UWF-4543[P1]"]
    assert cmd.get_coords("lig_1")[0][0] == 11
    assert cmd.get_setting_int("grid_mode") == 1


def test_pose_reads_for_different_panels_do_not_cancel_each_other(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())
    widget._select_pose(741, 1, 2)
    first_work, first_done = pending.pop()
    widget._select_pose(741, 2, 1)
    second_work, second_done = pending.pop()
    second_done(second_work())
    first_done(first_work())

    assert _label(cmd, 1) == ["4UWF-4543[P2]"]
    assert _label(cmd, 2) == ["4UWH-4543[P1]"]
    assert cmd.get_coords("lig_1")[0][0] == 12
    matrix = numpy.asarray(cmd.get_object_matrix("rec_2")).reshape(4, 4)
    assert cmd.get_coords("lig_2")[0] == pytest.approx((matrix @ [21, 0, 0, 1])[:3])


def test_a_pending_pose_update_does_not_reopen_the_grid_after_leaving(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())
    widget._select_pose(741, 1, 2)
    work, done = pending.pop()
    viewer = MolecularViewerController(SimpleNamespace(
        pymol_dock=dock,
        central_widget=SimpleNamespace(open_view=lambda view_id: None),
    ))
    viewer.sync_to_active_view("other")
    done(work())

    assert cmd.get_setting_int("grid_mode") == 0
    assert cmd.get_names("objects") == []


def test_a_missing_replacement_payload_keeps_the_displayed_pose(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())
    position = cmd.get_coords("lig_1").copy()
    widget._select_pose(741, 1, 2)
    work, done = pending.pop()
    done({})

    assert cmd.get_coords("lig_1") == pytest.approx(position)
    assert _label(cmd, 1) == ["4UWF-4543[P1]"]
    assert cmd.get_setting_int("grid_mode") == 1


def test_selecting_a_pose_without_a_comparison_opens_one_instead_of_a_single_pose(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget._select_pose(741, 1, 2)
    work, done = pending.pop()
    done(work())

    assert dock.scene_context.kind == "comparison"
    assert cmd.get_setting_int("grid_mode") == 1
    assert _label(cmd, 1) == ["4UWF-4543[P2]"]
    assert _label(cmd, 2) == ["4UWH-4543[P2]"]


def test_a_slow_initial_comparison_cannot_overwrite_a_newer_pose_selection(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget._show_ligand_grid(741)
    older_work, older_done = pending.pop()
    widget._select_pose(741, 1, 2)
    work, done = pending.pop()
    done(work())
    older_done(older_work())

    assert _label(cmd, 1) == ["4UWF-4543[P2]"]


def test_hiding_offtarget_cancels_pending_comparisons_and_disables_its_mode(grid_scene):
    widget, cmd, dock, pending = grid_scene
    widget.show()
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    done(work())
    widget._show_ligand_grid(741)
    work, done = pending.pop()
    widget.hide()
    done(work())

    assert not dock.scene_context.is_comparison
    assert cmd.get_setting_int("grid_mode") == 0
    assert "panel_text1" not in cmd.get_names("objects")
