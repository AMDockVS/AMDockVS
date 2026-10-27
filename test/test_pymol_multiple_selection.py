import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pydantic import AliasChoices, BaseModel, Field  # noqa: F401
import pymol2
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QTableWidget, QTableWidgetItem, QWidget

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
        window = QWidget()
        window.pymol_dock, window.grid_dock = dock, None
        viewer = window.viewer = molecular_viewer.MolecularViewerController(window)
        monkeypatch.setattr(molecular_viewer, "molecule_payload_id", lambda m: m.id)

        def read(wanted, then):
            then({name: ((f"HETATM    1  C1  LIG A   1    {number:8.3f}   0.000   0.000"
                          "  1.00  0.00           C\nEND\n").encode(), "pdb")
                  for name, number in wanted.items() if number})

        viewer._read = read
        yield viewer, window, cmd
        window.close()
        window.deleteLater()
        bar.close()
        app.processEvents()


def test_shift_click_accumulates_only_within_the_current_table(viewer_scene, monkeypatch):
    from amdockvs.models import MoleculeRecord
    from amdockvs.ui.catalog.common import project_table
    from ms_components.ms_table import ColumnDef, SmartTableView, TableConfig

    viewer, window, cmd = viewer_scene
    monkeypatch.setattr(SmartTableView, "refresh", lambda self: None)
    install_pymol_toolbar(window)
    assert not any("multiple selection" in a.text() for a in window._pymol_overflow_menu.actions())
    host = window
    layout = QHBoxLayout(host)
    tables = []
    for offset in (1, 10):
        table = project_table(
            SimpleNamespace(workspace=SimpleNamespace(project_db=object())),
            TableConfig(model_class=MoleculeRecord, columns=[ColumnDef("id")], show_row_numbers=False),
            host)
        table._model.load_data([
            {"id": offset + row, "__raw__": SimpleNamespace(
                id=offset + row, is_receptor=False, is_ligand=True)} for row in range(4)
        ], 4, 1)
        table.row_clicked.connect(lambda obj: viewer.show_molecule(obj, "current"))
        layout.addWidget(table)
        tables.append(table)
    host.show()
    QApplication.processEvents()

    def click(table, row, *, shift=False):
        QTest.mouseClick(table.view.viewport(), Qt.LeftButton,
                         Qt.ShiftModifier if shift else Qt.NoModifier,
                         table.view.visualRect(table._model.index(row, 0)).center())

    try:
        click(tables[0], 0)
        click(tables[0], 1, shift=True)
        click(tables[0], 1, shift=True)
        assert set(cmd.get_names("objects")) == {"molecule_1", "molecule_2"}
        assert cmd.count_states("molecule_2") == 1
        click(tables[1], 0, shift=True)
        assert cmd.get_names("objects") == ["molecule_10"]
        click(tables[1], 1, shift=True)
        assert set(cmd.get_names("objects")) == {"molecule_10", "molecule_11"}
        click(tables[1], 1)
        assert cmd.get_names("objects") == ["molecule_11"]
        click(tables[0], 2)
        assert cmd.get_names("objects") == ["molecule_3"]
    finally:
        host.close()


def test_multiple_pose_selections_keep_their_own_structures_and_contact_targets(viewer_scene, monkeypatch):
    from amdockvs.ui.viewers import interactions_3d

    viewer, window, cmd = viewer_scene
    table = object()
    hits = [SimpleNamespace(run_kind="screening", receptor_id=7, receptor_payload_id=7,
                            pose_payload_id=p, ligand_payload_id=None) for p in (11, 12)]
    for hit in hits:
        viewer.select_table_row(table, additive=True)
        viewer.load_hit(hit)
    objects = {obj for hit in hits for obj in viewer._pose_objects(hit)}
    assert set(cmd.get_names("objects")) == objects
    drawn = []
    monkeypatch.setattr(interactions_3d, "show_interaction", lambda cmd, **kwargs: drawn.append(kwargs))
    viewer.show_interaction(hits[0], object())
    assert (drawn[0]["receptor_obj"], drawn[0]["ligand_obj"]) == viewer._pose_objects(hits[0])
    viewer.select_table_row(table)
    viewer.load_hit(hits[0])
    assert set(cmd.get_names("objects")) == set(viewer._pose_objects(hits[0]))


@pytest.mark.parametrize("reset", ["clear", "other_table", "other_view"])
def test_multiple_reads_survive_each_other_but_not_a_scene_change(viewer_scene, monkeypatch, reset):
    viewer, window, cmd = viewer_scene
    pending, shown = [], []
    monkeypatch.setattr(molecular_viewer, "run_async", lambda work, done, error: pending.append((work, done)))
    table = object()
    viewer.select_table_row(table)
    viewer._later(lambda: "one", shown.append)
    viewer.select_table_row(table, additive=True)
    viewer._later(lambda: "two", shown.append)
    for work, done in reversed(pending):
        done(work())
    assert shown == ["two", "one"]
    viewer._later(lambda: "stale", shown.append)
    if reset == "clear":
        viewer.clear_scene()
    elif reset == "other_table":
        viewer.select_table_row(object(), additive=True)
    else:
        window.central_widget = SimpleNamespace(open_view=lambda _: None)
        viewer.sync_to_active_view("other")
    work, done = pending[-1]
    done(work())
    assert shown == ["two", "one"]
    assert cmd.get_names("objects") == []
    assert window.pymol_dock.scene_context.kind == "generic"

    pending.clear()
    viewer.select_table_row(table)
    viewer._later(lambda: "old", shown.append)
    viewer.select_table_row(table)
    viewer._later(lambda: "latest", shown.append)
    for work, done in reversed(pending):
        done(work())
    assert shown == ["two", "one", "latest"]


def test_pending_result_hits_accumulate_but_do_not_reappear_after_a_table_change(viewer_scene):
    from amdockvs.ui.catalog.results import DockingResultsWidget

    viewer, window, cmd = viewer_scene
    table = object()
    viewer.select_table_row(table)
    viewer.select_table_row(table, additive=True)
    epoch = window.pymol_dock._amdock_scene_epoch
    selected, shown = [], []
    page = SimpleNamespace(
        _hit_token=2, window=lambda: window, _show_detail=selected.append,
        _pose_rank=lambda hit: 1, _load_hit_in_pymol=lambda hit, rank: shown.append(hit),
        _load_selected_hit_in_pymol=lambda: shown.append(selected[-1]))
    DockingResultsWidget._apply_selected_hit(page, "latest", (2, epoch))
    DockingResultsWidget._apply_selected_hit(page, "earlier_shift_click", (1, epoch))
    assert selected == ["latest"]
    assert shown == ["latest", "earlier_shift_click"]
    viewer.select_table_row(object(), additive=True)
    DockingResultsWidget._apply_selected_hit(page, "stale", (2, epoch))
    assert shown == ["latest", "earlier_shift_click"]


def test_redocking_clicks_replace_or_accumulate_even_when_reselecting_a_row(viewer_scene, monkeypatch):
    from amdockvs.ui.tools.docking.redocking import RedockingWidget

    viewer, window, cmd = viewer_scene
    monkeypatch.setattr(RedockingWidget, "refresh", lambda self: None)
    monkeypatch.setattr(RedockingWidget, "_refresh_results", lambda self: None)
    host = window
    layout = QHBoxLayout(host)
    page = RedockingWidget(runtime=object(), load_hit_in_pymol=viewer.load_hit, parent=host)
    layout.addWidget(page)
    page._pose_rank = lambda hit: 1
    page._rows = [SimpleNamespace(
        run_kind="redocking", pose_payload_id=pose, reference_ligand_payload_id=8,
        ligand_payload_id=None, reference_receptor_payload_id=7, receptor_payload_id=7)
        for pose in (11, 12)]
    page.results_table.setRowCount(2)
    for row in range(2):
        item = QTableWidgetItem(str(row))
        item.setData(Qt.UserRole, row)
        page.results_table.setItem(row, 0, item)
    host.show()
    QApplication.processEvents()

    def click(row, modifiers=Qt.NoModifier):
        QTest.mouseClick(page.results_table.viewport(), Qt.LeftButton, modifiers,
                         page.results_table.visualItemRect(page.results_table.item(row, 0)).center())

    try:
        click(0)
        click(1, Qt.ShiftModifier)
        assert {"redock_docked_pose_11", "redock_docked_pose_12"} <= set(cmd.get_names("objects"))
        click(1)
        assert "redock_docked_pose_11" not in cmd.get_names("objects")
        assert "redock_docked_pose_12" in cmd.get_names("objects")
    finally:
        host.close()


def test_multiple_redocking_results_keep_names_and_details_replace_the_scene(viewer_scene):
    viewer, window, cmd = viewer_scene
    table = object()
    for pose in (11, 12):
        viewer.select_table_row(table, additive=True)
        viewer.load_hit(SimpleNamespace(
            run_kind="redocking", pose_payload_id=pose, reference_ligand_payload_id=8,
            ligand_payload_id=None, reference_receptor_payload_id=7, receptor_payload_id=7))
    for pose in (11, 12):
        assert {f"redock_reference_receptor_{pose}", f"redock_reference_ligand_{pose}",
                f"redock_docked_pose_{pose}"} <= set(cmd.get_names("objects"))
    viewer.show_payload(20, "selected_file")
    viewer.show_payload(21, "selected_file")
    assert cmd.get_names("objects") == ["selected_file"]


def test_reselecting_a_structure_keeps_its_existing_comparison_panel(viewer_scene):
    viewer, window, cmd = viewer_scene
    table = object()

    def show(number):
        viewer.select_table_row(table, additive=True)
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
