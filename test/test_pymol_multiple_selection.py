import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pydantic import AliasChoices, BaseModel, Field  # noqa: F401
import pymol2
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QTableWidget, QTableWidgetItem, QToolButton, QWidget

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
    control_bar = window.pymol_dock.control_bar
    assert "pymol.protein_interface" not in {action.data() for action in control_bar.preset_menu.actions()}
    assert control_bar.toolbar.actions()[-1] is control_bar.export_action
    assert control_bar.toolbar.widgetForAction(control_bar.export_action).popupMode() == QToolButton.InstantPopup
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
    # One receptor payload, one receptor object: the poses share it, each keeps its ligand.
    assert len(objects) == 3
    assert viewer._pose_objects(hits[0])[0] == viewer._pose_objects(hits[1])[0]
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
        _showing_together=lambda: False, isVisibleTo=lambda _window: True,
        _load_selected_hit_in_pymol=lambda: shown.append(selected[-1]))
    DockingResultsWidget._apply_selected_hit(page, "latest", (2, epoch))
    DockingResultsWidget._apply_selected_hit(page, "earlier_shift_click", (1, epoch))
    assert selected == ["latest"]
    assert shown == ["latest", "earlier_shift_click"]
    viewer.select_table_row(object(), additive=True)
    DockingResultsWidget._apply_selected_hit(page, "stale", (2, epoch))
    assert shown == ["latest", "earlier_shift_click"]


def test_a_results_tab_behind_another_leaves_pymol_alone(viewer_scene):
    # Complexes fills itself at start-up behind Receptors: its first pose must not take PyMOL.
    from amdockvs.ui.catalog.results import DockingResultsWidget

    _viewer, window, _cmd = viewer_scene
    selected, shown = [], []
    page = SimpleNamespace(
        _hit_token=1, window=lambda: window, _show_detail=selected.append,
        _showing_together=lambda: False, isVisibleTo=lambda _window: False,
        _load_selected_hit_in_pymol=lambda: shown.append(selected[-1]))
    DockingResultsWidget._apply_selected_hit(page, "first", (1, getattr(window.pymol_dock, "_amdock_scene_epoch", 0)))
    assert selected == ["first"]
    assert shown == []


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


def test_a_repeat_click_on_the_shown_pose_reloads_nothing(viewer_scene, monkeypatch):
    """Clicking the selected row again used to clear PyMOL and load it back: the camera and
    styles jumped. Now it is a no-op, unless Shift had added more to the scene."""
    from amdockvs.models import MoleculeRecord
    from amdockvs.ui.catalog.common import project_table
    from ms_components.ms_table import ColumnDef, SmartTableView, TableConfig

    viewer, window, cmd = viewer_scene
    monkeypatch.setattr(SmartTableView, "refresh", lambda self: None)
    reads, read = [], viewer._read
    viewer._read = lambda wanted, then: (reads.append(dict(wanted)), read(wanted, then))
    hits = [SimpleNamespace(run_kind="screening", receptor_id=7, receptor_payload_id=7,
                            pose_payload_id=p, ligand_payload_id=None) for p in (11, 12)]
    table = project_table(
        SimpleNamespace(workspace=SimpleNamespace(project_db=object())),
        TableConfig(model_class=MoleculeRecord, columns=[ColumnDef("id")], show_row_numbers=False),
        window)
    table._model.load_data([{"id": row, "__raw__": SimpleNamespace(id=row)} for row in range(2)], 2, 1)
    show = lambda obj: viewer.shows_hit(hits[obj.id]) or viewer.load_hit(hits[obj.id])  # as the window does
    table.row_clicked.connect(show)
    table.row_reselected.connect(show)
    QHBoxLayout(window).addWidget(table)
    window.show()
    QApplication.processEvents()

    def click(row, modifiers=Qt.NoModifier):
        QTest.mouseClick(table.view.viewport(), Qt.LeftButton, modifiers,
                         table.view.visualRect(table._model.index(row, 0)).center())

    click(0)
    assert len(reads) == 1
    click(0)
    assert len(reads) == 1 and set(cmd.get_names("objects")) == set(viewer._pose_objects(hits[0]))
    click(1, Qt.ShiftModifier)
    assert len(cmd.get_names("objects")) == 3  # both poses share receptor 7's one object
    click(1)  # the scene holds two poses, the click asks for one
    assert set(cmd.get_names("objects")) == set(viewer._pose_objects(hits[1]))


def test_selected_ligands_share_the_scene_and_leave_it_when_deselected(viewer_scene, monkeypatch):
    from PySide6.QtCore import QItemSelectionModel
    from amdockvs.ui.catalog import results
    from ms_components.ms_table import SmartTableView

    viewer, window, cmd = viewer_scene
    monkeypatch.setattr(SmartTableView, "refresh", lambda self: None)
    monkeypatch.setattr(results, "run_async", lambda work, done, on_error=None, **_: done(work()))
    hits = {rid: SimpleNamespace(run_kind="screening", receptor_id=7, receptor_payload_id=7, result_id=rid,
                                 pose_payload_id=100 + rid, ligand_payload_id=None, pose_rank=1)
            for rid in (1, 2, 3)}
    page = results.DockingResultsWidget(runtime=SimpleNamespace(
        workspace=SimpleNamespace(project_db=object()),
        docking=SimpleNamespace(result_protocols=lambda **_kwargs: [], hit=lambda result_id: hits[result_id]),
    ), load_hit_in_pymol=viewer.load_hit, parent=window)
    page.pose_table.set_filters = lambda filters: None
    page._show_detail = lambda hit: None
    page.ligand_table._model.load_data([
        {"id": rid, "__raw__": SimpleNamespace(id=rid, ligand_molecule_id=50 + rid, docking_run_id=1, score=-7.0)}
        for rid in hits], 3, 1)
    selection = page.ligand_table._table.selectionModel()

    def pick(row, command):
        viewer.select_table_row(page.ligand_table._table, additive=command != QItemSelectionModel.ClearAndSelect)
        selection.select(page.ligand_table._model.index(row, 0), command | QItemSelectionModel.Rows)

    pick(0, QItemSelectionModel.ClearAndSelect)
    pick(1, QItemSelectionModel.Select)
    pick(2, QItemSelectionModel.Select)
    poses = {obj for obj in cmd.get_names("objects") if obj.startswith("amdock_result_pose")}
    assert poses == {viewer._pose_objects(hits[rid])[1] for rid in hits}
    pick(1, QItemSelectionModel.Deselect)
    poses = {obj for obj in cmd.get_names("objects") if obj.startswith("amdock_result_pose")}
    assert poses == {viewer._pose_objects(hits[rid])[1] for rid in (1, 3)}
    # Another program: the scene starts over on the first ligand of its list.
    page._populate_protocol_combo([("q", "QuickVina")])
    page.protocol_combo.setCurrentIndex(0)  # All programs
    assert not {obj for obj in cmd.get_names("objects") if obj.startswith("amdock_result_pose")}
    viewer.load_hit(hits[1], 1)  # what the first row's pose loads, once the list is back
    assert {obj for obj in cmd.get_names("objects") if obj.startswith("amdock_result_pose")} == {
        "amdock_result_pose"}


def test_a_press_on_a_non_table_item_view_is_ignored(monkeypatch):
    """The grid box site list stores its color under the role SmartTable uses for its ColumnDef."""
    from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem, QWidget
    from amdockvs.ui.tools.binding_sites.box import SITE_COLOR_ROLE

    QApplication.instance() or QApplication([])
    window = QWidget()
    window.pymol_dock, window.grid_dock = None, None
    viewer = molecular_viewer.MolecularViewerController(window)
    selected = []
    monkeypatch.setattr(viewer, "select_table_row", lambda view, additive: selected.append(view))
    tree = QTreeWidget(window)
    tree.setColumnCount(3)
    item = QTreeWidgetItem(["", "7A5 site"])
    item.setData(1, SITE_COLOR_ROLE, "#f4a0b0")
    tree.addTopLevelItem(item)
    viewer._on_table_pressed(tree, tree.model().index(0, 1), Qt.NoModifier)
    assert selected == []


def test_a_pose_table_swaps_the_pose_and_keeps_the_receptor_and_camera(viewer_scene):
    viewer, window, cmd = viewer_scene
    reads, read = [], viewer._read
    viewer._read = lambda wanted, then: (reads.append(set(wanted)), read(wanted, then))
    table = QWidget()
    table.setProperty(molecular_viewer.SWAPS_POSE, True)
    hits = [SimpleNamespace(run_kind="screening", receptor_id=7, receptor_payload_id=7,
                            pose_payload_id=p, ligand_payload_id=None) for p in (11, 12, 13)]
    viewer.select_table_row(table)
    viewer.load_hit(hits[0])
    cmd.turn("y", 45)
    camera = cmd.get_view()
    for hit in hits[1:]:  # arrows down: one pose at a time, receptor read once
        viewer.select_table_row(table)
        viewer.load_hit(hit)
    assert reads == [{"pose", "receptor"}, {"pose"}, {"pose"}]
    assert set(cmd.get_names("objects")) == set(viewer._pose_objects(hits[2]))
    assert cmd.get_view() == camera
    viewer.select_table_row(table, additive=True)  # Shift adds the pose next to the shown one
    viewer.load_hit(hits[0])
    assert len(cmd.get_names("objects")) == 2 + 2
    viewer.select_table_row(table)  # a plain row after Shift starts over with one pose
    viewer.load_hit(hits[1])
    assert set(cmd.get_names("objects")) == set(viewer._pose_objects(hits[1]))


def _atom(x: float, *, resn: str = "LIG", name: str = "C1", record: str = "HETATM") -> str:
    return f"{record:<6}    1  {name:<3} {resn} A   1    {x:8.3f}   0.000   0.000  1.00  0.00           C\n"


def _shared_receptor_hit(pose: int, *, receptor: int = 7, payload: int | None = 7):
    return SimpleNamespace(run_kind="screening", receptor_id=receptor, receptor_payload_id=payload,
                           pose_payload_id=pose, ligand_payload_id=None)


def test_poses_read_together_share_one_receptor_whatever_order_their_reads_finish(viewer_scene):
    """Each pose asks for the receptor while none is loaded yet; the first read to finish loads
    it and the others only add their ligand: one copy, never reloaded under the others."""
    viewer, window, cmd = viewer_scene
    pending = []
    viewer._read = lambda wanted, then: pending.append((dict(wanted), then))
    hits = [_shared_receptor_hit(p) for p in (11, 12, 13)]
    viewer.select_table_row(object(), additive=True, restart=True)
    for hit in hits:
        viewer.load_hit(hit)
    assert all("receptor" in wanted for wanted, _ in pending)
    for wanted, then in reversed(pending):
        then({name: ((_atom(float(number)) + "END\n").encode(), "pdb") for name, number in wanted.items()})
    receptor = viewer._pose_objects(hits[0])[0]
    ligands = {viewer._pose_objects(hit)[1] for hit in hits}
    assert set(cmd.get_names("objects")) == {receptor, *ligands}
    assert cmd.count_atoms(receptor) == 1  # loaded once, not one copy per pose stacked in it
    assert all(viewer.shows_hit(hit) for hit in hits)
    assert window.pymol_dock.scene_context.selections["ligand"].count("amdock_result_pose") == 3


def test_poses_keep_one_receptor_object_per_receptor_payload(viewer_scene):
    """Two receptors (the Complexes view lists both), or two preparations of one receptor, are
    different structures: each keeps its own object. A receptor without a payload id has nothing
    to load: only its name falls back to the plain one, and its pose still loads."""
    viewer, window, cmd = viewer_scene
    hits = [_shared_receptor_hit(11), _shared_receptor_hit(12), _shared_receptor_hit(21, receptor=8, payload=8),
            _shared_receptor_hit(31, payload=70), _shared_receptor_hit(41, receptor=9, payload=None)]
    viewer.select_table_row(object(), additive=True, restart=True)
    for hit in hits:
        viewer.load_hit(hit)
    receptors = {viewer._pose_objects(hit)[0] for hit in hits}
    assert receptors == {"receptor_7_payload_7", "receptor_8_payload_8", "receptor_7_payload_70", "receptor_9"}
    assert set(cmd.get_names("objects")) == (receptors - {"receptor_9"}) | {
        viewer._pose_objects(hit)[1] for hit in hits}


def test_a_contact_picked_on_a_shared_receptor_replaces_the_previous_poses_pick(viewer_scene):
    """Contacts draw in their own pose's group, but the receptor object is shared: picking a
    contact for pose B takes pose A's drawing away, and reloading a pose drops only its own."""
    from amdockvs.ui.viewers.interactions_3d import interaction_group

    viewer, window, cmd = viewer_scene
    hits = [_shared_receptor_hit(p) for p in (11, 12)]
    viewer.select_table_row(object(), additive=True, restart=True)
    for hit in hits:
        viewer.load_hit(hit)
    contact = SimpleNamespace(kind="hydrophobic", residue_key="A:1:LIG", ligand_atoms=[0],
                              protein_atom="C1", protein_atoms=(), via_water=None)
    groups = [interaction_group(*viewer._pose_objects(hit)) for hit in hits]
    assert groups[0] != groups[1]
    viewer.show_interaction(hits[0], contact)
    assert groups[0] in cmd.get_names("objects")
    viewer.show_interaction(hits[1], contact)
    names = set(cmd.get_names("objects"))
    assert groups[1] in names and groups[0] not in names
    window.pymol_dock.cmd.delete(viewer._pose_objects(hits[1])[1])  # pose 12 leaves, then comes back
    viewer.load_hit(hits[1])
    assert groups[1] not in cmd.get_names("objects")


def test_flexible_poses_on_a_shared_receptor_keep_their_own_side_chains(viewer_scene):
    viewer, window, cmd = viewer_scene

    def read(wanted, then):
        found = {}
        for name, number in wanted.items():
            text = _atom(float(number)) + "END\n"
            if name == "pose":  # the side chains docked with this pose ride in its SD property
                text += "> <flexres>\n" + _atom(float(number), resn="TYR", name="OH", record="ATOM") + "\n"
            found[name] = (text.encode(), "pdb")
        then(found)

    viewer._read = read
    hits = [_shared_receptor_hit(p) for p in (11, 12)]
    viewer.select_table_row(object(), additive=True, restart=True)
    for hit in hits:
        viewer.load_hit(hit)
    flexres = {f"{viewer._pose_objects(hit)[1]}_flexres" for hit in hits}
    assert flexres <= set(cmd.get_names("objects"))
    assert len({viewer._pose_objects(hit)[0] for hit in hits}) == 1


def test_changing_the_pose_takes_the_previous_poses_picked_contact_away(viewer_scene):
    """A contact picked for pose 1 draws its residue as sticks with a label; arrowing to pose 2
    in the pose table (the receptor stays) must not leave that residue drawn for a pose it is
    no longer a contact of."""
    viewer, window, cmd = viewer_scene
    table = QWidget()
    table.setProperty(molecular_viewer.SWAPS_POSE, True)
    hits = [_shared_receptor_hit(p) for p in (11, 12)]
    viewer.select_table_row(table)
    viewer.load_hit(hits[0])
    receptor, _ligand = viewer._pose_objects(hits[0])
    cmd.hide("everything", receptor)
    contact = SimpleNamespace(kind="hydrophobic", residue_key="A:1:LIG", ligand_atoms=[0],
                              protein_atom="C1", protein_atoms=(), via_water=None)
    viewer.show_interaction(hits[0], contact)

    def labels():
        shown = []
        cmd.iterate(receptor, "shown.append(label)", space={"shown": shown})
        return [text for text in shown if text]

    assert cmd.count_atoms(f"{receptor} and rep sticks") == 1
    viewer.select_table_row(table)
    viewer.load_hit(hits[1])
    assert viewer._pose_objects(hits[1])[0] == receptor  # swapped, receptor kept
    assert cmd.count_atoms(f"{receptor} and rep sticks") == 0 and not labels()
    assert not any(name.startswith("amdock_interactions") for name in cmd.get_names("objects"))


def test_a_large_selection_shows_the_first_rows_in_table_order_and_the_clicked_one(viewer_scene, monkeypatch):
    """Past MAX_SCENE_ROWS, PyMOL shows the first selected rows as the table orders them (its
    sort, best scores first by default) and always the row last clicked; the table keeps the
    whole selection, and the status bar says what was left out."""
    from PySide6.QtCore import QItemSelectionModel
    from amdockvs.ui.catalog import results
    from ms_components.ms_table import SmartTableView

    viewer, window, cmd = viewer_scene
    monkeypatch.setattr(SmartTableView, "refresh", lambda self: None)
    monkeypatch.setattr(results, "run_async", lambda work, done, on_error=None, **_: done(work()))
    monkeypatch.setattr(results, "MAX_SCENE_ROWS", 3)
    messages = []
    window.statusBar = lambda: SimpleNamespace(showMessage=lambda text, _ms: messages.append(text))
    hits = {rid: SimpleNamespace(run_kind="screening", receptor_id=7, receptor_payload_id=7, result_id=rid,
                                 pose_payload_id=100 + rid, ligand_payload_id=None, pose_rank=1)
            for rid in range(1, 7)}
    page = results.DockingResultsWidget(runtime=SimpleNamespace(
        workspace=SimpleNamespace(project_db=object()),
        docking=SimpleNamespace(result_protocols=lambda **_kwargs: [], hit=lambda result_id: hits[result_id]),
    ), load_hit_in_pymol=viewer.load_hit, parent=window)
    page.pose_table.set_filters = lambda filters: None
    page._show_detail = lambda hit: None
    page.ligand_table._model.load_data([  # table order: result 6 first, then 5, ... 1
        {"id": rid, "__raw__": SimpleNamespace(id=rid, ligand_molecule_id=50 + rid, docking_run_id=1, score=-rid)}
        for rid in sorted(hits, reverse=True)], 6, 1)
    view, model = page.ligand_table._table, page.ligand_table._model
    selection = view.selectionModel()

    def poses():
        return {obj for obj in cmd.get_names("objects") if obj.startswith("amdock_result_pose")}

    def expected(*rids):
        return {viewer._pose_objects(hits[rid])[1] for rid in rids}

    viewer.select_table_row(view)
    selection.setCurrentIndex(model.index(0, 0), QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    viewer.select_table_row(view, additive=True)
    for row in (5, 3, 1, 4, 2):  # selected out of table order: the scene still follows the table
        selection.select(model.index(row, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    assert len(page.ligand_table.get_selected_objects()) == 6  # the table keeps every row
    assert poses() == expected(6, 5, 4)
    assert messages and "first 3 of 6" in messages[-1]
    # Ctrl-click on the last row (deselect, then select it as current): it joins the scene.
    selection.select(model.index(5, 0), QItemSelectionModel.Deselect | QItemSelectionModel.Rows)
    selection.setCurrentIndex(model.index(5, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    assert poses() == expected(6, 5, 1)


def test_growing_or_shrinking_a_selection_only_loads_or_removes_the_rows_that_changed(viewer_scene, monkeypatch):
    """A drag sends one selection change per row crossed. Each row's hit is fetched once, a
    deselected row's pose leaves without the scene reloading the others, and a read that
    lands after its row was deselected does not bring the pose back."""
    from PySide6.QtCore import QItemSelectionModel
    from amdockvs.ui.catalog import results
    from ms_components.ms_table import SmartTableView

    viewer, window, cmd = viewer_scene
    monkeypatch.setattr(SmartTableView, "refresh", lambda self: None)
    pending = []
    monkeypatch.setattr(results, "run_async", lambda work, done, on_error=None, **_: pending.append((work, done)))
    fetched, loads = [], []
    hits = {rid: SimpleNamespace(run_kind="screening", receptor_id=7, receptor_payload_id=7, result_id=rid,
                                 pose_payload_id=100 + rid, ligand_payload_id=None, pose_rank=1)
            for rid in range(1, 7)}
    page = results.DockingResultsWidget(runtime=SimpleNamespace(
        workspace=SimpleNamespace(project_db=object()),
        docking=SimpleNamespace(result_protocols=lambda **_kwargs: [],
                                hit=lambda result_id: (fetched.append(result_id), hits[result_id])[1]),
    ), load_hit_in_pymol=lambda hit, rank: (loads.append(hit.result_id), viewer.load_hit(hit, rank)),
        parent=window)
    page.pose_table.set_filters = lambda filters: None
    page._show_detail = lambda hit: None
    page.ligand_table._model.load_data([
        {"id": rid, "__raw__": SimpleNamespace(id=rid, ligand_molecule_id=50 + rid, docking_run_id=1, score=-7.0)}
        for rid in hits], 6, 1)
    view, model = page.ligand_table._table, page.ligand_table._model
    selection = view.selectionModel()

    def settle():
        while pending:
            work, done = pending.pop(0)
            done(work())

    def poses():
        return {obj for obj in cmd.get_names("objects") if obj.startswith("amdock_result_pose")}

    def expected(*rids):
        return {viewer._pose_objects(hits[rid])[1] for rid in rids}

    viewer.select_table_row(view)
    selection.select(model.index(0, 0), QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
    settle()
    viewer.select_table_row(view, additive=True)
    for row in range(1, 6):  # a drag down to the last row: one change per row
        selection.select(model.index(row, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
        settle()
    assert sorted(fetched) == [1, 2, 3, 4, 5, 6]  # each row once, not 2+3+4+5+6 times
    assert poses() == expected(1, 2, 3, 4, 5, 6)
    fetched.clear(), loads.clear()
    for row in (5, 4):  # dragging back up: rows leave, the others stay as they are
        selection.select(model.index(row, 0), QItemSelectionModel.Deselect | QItemSelectionModel.Rows)
    assert fetched == [] and loads == [] and not pending
    assert poses() == expected(1, 2, 3, 4)
    selection.select(model.index(4, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    selection.select(model.index(4, 0), QItemSelectionModel.Deselect | QItemSelectionModel.Rows)
    settle()  # row 5's read lands after it left again: its pose stays out
    assert poses() == expected(1, 2, 3, 4)


def test_a_pose_removed_while_its_structure_is_read_does_not_come_back(viewer_scene):
    viewer, window, cmd = viewer_scene
    pending = []
    viewer._read = lambda wanted, then: pending.append((dict(wanted), then))
    hits = [_shared_receptor_hit(p) for p in (11, 12)]
    viewer.select_table_row(object(), additive=True, restart=True)
    for hit in hits:
        viewer.load_hit(hit)
    assert viewer.remove_hit(hits[1])  # its row left during a drag, before the read landed
    for wanted, then in pending:
        then({name: ((_atom(float(number)) + "END\n").encode(), "pdb") for name, number in wanted.items()})
    poses = {n for n in cmd.get_names("objects") if n.startswith("amdock_result_pose")}
    assert poses == {viewer._pose_objects(hits[0])[1]}


def test_removing_a_pose_keeps_the_other_poses_in_the_scene_roles(viewer_scene):
    """The roles are merged as PyMOL selections when a pose is added: a removed pose left in
    them made the selection invalid and dropped every earlier pose from the scene's roles."""
    viewer, window, cmd = viewer_scene
    hits = [_shared_receptor_hit(p) for p in (11, 12, 13, 14)]
    viewer.select_table_row(object(), additive=True, restart=True)
    for hit in hits[:3]:
        viewer.load_hit(hit)
    assert viewer.remove_hit(hits[1])
    viewer.load_hit(hits[3])
    ligands = window.pymol_dock.scene_context.selections["ligand"].split(" or ")
    assert set(ligands) == {viewer._pose_objects(hits[i])[1] for i in (0, 2, 3)}
    assert window.pymol_dock.scene_context.selections["receptor"] == viewer._pose_objects(hits[0])[0]
