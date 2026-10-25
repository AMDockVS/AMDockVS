import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pydantic import AliasChoices, BaseModel, Field  # noqa: F401
import pymol2
import pytest
from PySide6.QtWidgets import QApplication

from amdockvs.ui.tools.pymol_ribbon import install_pymol_toolbar
from amdockvs.ui.viewers import molecular_viewer
from ms_components.ms_pymol import PymolControlBar


@pytest.fixture
def viewer_scene(monkeypatch):
    app = QApplication.instance() or QApplication(["amdockvs-multiple-selection-test"])
    with pymol2.PyMOL() as session:
        cmd = session.cmd
        bar = PymolControlBar(cmd)

        def context(*args, **kwargs):
            bar.set_scene_context(*args, **kwargs)
            dock.scene_context = bar.scene_context

        dock = SimpleNamespace(cmd=cmd, control_bar=bar, show=lambda: None,
                               scene_context=bar.scene_context, set_scene_context=context,
                               register_preset=bar.register_preset)
        window = SimpleNamespace(pymol_dock=dock, grid_dock=None)
        viewer = window.viewer = molecular_viewer.MolecularViewerController(window)
        monkeypatch.setattr(molecular_viewer, "molecule_payload_id", lambda m: m.id)

        def read(wanted, then):
            then({name: ((f"HETATM    1  C1  LIG A   1    {number:8.3f}   0.000   0.000"
                          "  1.00  0.00           C\nEND\n").encode(), "pdb")
                  for name, number in wanted.items() if number})

        viewer._read = read
        yield viewer, window, cmd
        bar.close()
        app.processEvents()


def test_settings_allow_multiple_structures_and_restore_single_selection(viewer_scene):
    viewer, window, cmd = viewer_scene
    install_pymol_toolbar(window)
    action = next(a for a in window._pymol_overflow_menu.actions() if "multiple selection" in a.text())
    assert action.isCheckable() and not action.isChecked()
    action.trigger()
    for number in (1, 2, 3, 3):
        viewer.show_molecule(SimpleNamespace(id=number, is_receptor=number == 1,
                                            is_ligand=number != 1), "current")
    assert set(cmd.get_names("objects")) == {"molecule_1", "molecule_2", "molecule_3"}
    assert cmd.count_states("molecule_3") == 1
    assert window.pymol_dock.scene_context.kind == "complex"
    window.pymol_dock.control_bar.apply_preset("amdockvs.complex")
    assert cmd.count_atoms("molecule_1 and rep cartoon") == 1
    assert cmd.count_atoms("(molecule_2 or molecule_3) and rep sticks") == 2
    assert window.pymol_dock.control_bar._presets["pymol.ligand_cartoon"].per_grid_slot

    action.trigger()
    viewer.show_molecule(SimpleNamespace(id=4, is_receptor=False, is_ligand=True), "current")
    assert cmd.get_names("objects") == ["molecule_4"]


def test_multiple_pose_selections_keep_their_own_structures_and_contact_targets(viewer_scene, monkeypatch):
    from amdockvs.ui.viewers import interactions_3d

    viewer, window, cmd = viewer_scene
    viewer.set_multiple_selection_enabled(True)
    hits = [SimpleNamespace(run_kind="screening", receptor_id=7, receptor_payload_id=7,
                            pose_payload_id=p, ligand_payload_id=None) for p in (11, 12)]
    for hit in hits:
        viewer.load_hit(hit)
    objects = {obj for hit in hits for obj in viewer._pose_objects(hit)}
    assert set(cmd.get_names("objects")) == objects
    drawn = []
    monkeypatch.setattr(interactions_3d, "show_interaction", lambda cmd, **kwargs: drawn.append(kwargs))
    viewer.show_interaction(hits[0], object())
    assert (drawn[0]["receptor_obj"], drawn[0]["ligand_obj"]) == viewer._pose_objects(hits[0])
    viewer.set_multiple_selection_enabled(False)
    viewer.load_hit(hits[0])
    assert set(cmd.get_names("objects")) == {"receptor_7", "amdock_result_pose"}


def test_multiple_reads_survive_each_other_but_not_clear_scene(viewer_scene, monkeypatch):
    viewer, window, cmd = viewer_scene
    pending, shown = [], []
    monkeypatch.setattr(molecular_viewer, "run_async", lambda work, done, error: pending.append((work, done)))
    viewer.set_multiple_selection_enabled(True)
    viewer._later(lambda: "one", shown.append)
    viewer._later(lambda: "two", shown.append)
    for work, done in reversed(pending):
        done(work())
    assert shown == ["two", "one"]
    viewer._later(lambda: "stale", shown.append)
    viewer.clear_scene()
    work, done = pending[-1]
    done(work())
    assert shown == ["two", "one"]
    assert cmd.get_names("objects") == []
    assert window.pymol_dock.scene_context.kind == "generic"

    viewer.set_multiple_selection_enabled(False)
    pending.clear()
    viewer._later(lambda: "old", shown.append)
    viewer._later(lambda: "latest", shown.append)
    for work, done in reversed(pending):
        done(work())
    assert shown == ["two", "one", "latest"]


def test_multiple_redocking_results_and_payloads_do_not_reuse_object_names(viewer_scene):
    viewer, window, cmd = viewer_scene
    viewer.set_multiple_selection_enabled(True)
    for pose in (11, 12):
        viewer.load_hit(SimpleNamespace(
            run_kind="redocking", pose_payload_id=pose, reference_ligand_payload_id=8,
            ligand_payload_id=None, reference_receptor_payload_id=7, receptor_payload_id=7))
    for pose in (11, 12):
        assert {f"redock_reference_receptor_{pose}", f"redock_reference_ligand_{pose}",
                f"redock_docked_pose_{pose}"} <= set(cmd.get_names("objects"))
    viewer.show_payload(20, "selected_file")
    viewer.show_payload(21, "selected_file")
    assert {"selected_file_20", "selected_file_21"} <= set(cmd.get_names("objects"))


def test_reselecting_a_structure_keeps_its_existing_comparison_panel(viewer_scene):
    viewer, window, cmd = viewer_scene
    viewer.set_multiple_selection_enabled(True)

    def show(number):
        viewer.show_molecule(SimpleNamespace(id=number, is_ligand=True, is_receptor=False), "current")

    show(1)
    show(2)
    cmd.set("grid_slot", 1, "molecule_1")
    cmd.set("grid_slot", 2, "molecule_2")
    window.pymol_dock.set_scene_context(
        "comparison", selections={"ligand": "molecule_1 or molecule_2"}, grid_labels={1: "One", 2: "Two"})
    show(3)
    show(4)
    show(3)
    assert cmd.get_setting_int("grid_slot", "molecule_3") == 3
    assert cmd.get_setting_int("grid_slot", "molecule_4") == 4
    assert set(window.pymol_dock.scene_context.grid_labels) == {1, 2, 3, 4}
    assert "panel_text5" not in cmd.get_names("objects")
