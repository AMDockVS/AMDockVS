import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "//src")

# pydantic BEFORE PySide6: shiboken's feature loader trips pydantic's lazy migration shim
# otherwise, and the module fails to import (see test_ui_job_feedback).
from amdockvs.runtime import AMDockVSRuntime

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow, QToolButton, QWidget

from amdockvs.ui.catalog import LIGANDS_VIEW_ID, MOLECULES_VIEW_ID, RECEPTOR_VIEW_ID
from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.projects import ApplicationWidget, ProjectsWidget
from amdockvs.ui.workspace import ComplexWidget, LigandActivityWidget
from amdockvs.ui.catalog.domain_views import COMPLEXES_VIEW_ID
from amdockvs.ui.tools.docking import (
    DOCKING_VIEW_ID,
    PREP_STATUS_VIEW_ID,
    DockingStudioWidget,
    EngineStatePrepView,
)
from amdockvs.ui.tools.docking.offtarget import OFFTARGET_VIEW_ID
from amdockvs.ui.tools.docking.redocking import REDOCKING_VIEW_ID
from amdockvs.ui.tools.molecules.pocket_detection import (
    P2RANK_SCORE_COLORS,
    _score_palette_indices,
)
from amdockvs.ui.catalog.molecules import MoleculeWidget
from amdockvs.ui.catalog.ligands import LigandWidget, _create_ligand_set


def _patch_fake_home(monkeypatch, fake_home: Path):
    monkeypatch.setenv("HOME", str(fake_home))


def test_p2rank_score_palette_is_ordered_and_distinct_for_ten_sites():
    scores = [9.8, 1.2, 7.4, 4.1, 8.6, 2.3, 6.2, 3.5, 5.0, 0.4]
    indices = _score_palette_indices(scores)

    assert len(P2RANK_SCORE_COLORS) == 10
    assert len(set(indices)) == 10
    assert [
        indices[index]
        for index in sorted(range(len(scores)), key=lambda item: scores[item])
    ] == list(range(10))


def test_2d_diagram_and_pymol_are_split_evenly_when_both_are_visible():
    app = QApplication.instance() or QApplication(["amdockvs-dock-sizing-test"])
    window = QMainWindow()
    window.resize(900, 800)
    window.setCentralWidget(QWidget())
    pymol = QDockWidget("PyMOL", window)
    diagram = QDockWidget("2D Interactions", window)
    pymol.setWidget(QWidget())
    diagram.setWidget(QWidget())
    window.addDockWidget(Qt.RightDockWidgetArea, pymol)
    window.splitDockWidget(pymol, diagram, Qt.Vertical)
    window.show()
    app.processEvents()
    window.resizeDocks([pymol, diagram], [3, 1], Qt.Vertical)
    app.processEvents()

    window.pymol_dock = pymol
    window.diagram_dock = diagram
    AMDockVSMainWindow._split_viewer_docks_evenly(window)
    app.processEvents()

    assert abs(pymol.height() - diagram.height()) <= 2
    window.close()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_ui_smoke_instantiates_without_project(tmp_path, monkeypatch):
    _patch_fake_home(monkeypatch, tmp_path)

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    window = None
    runtime = AMDockVSRuntime()
    try:
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()
        assert window.windowTitle() == "AMDockVS"
        assert window.monitor_dock is not None
        assert window.monitor_dock.summary_widget is not None
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


def test_projects_widget_registers_amdock_manifest_outside_workspace(tmp_path, monkeypatch):
    _patch_fake_home(monkeypatch, tmp_path)
    monkeypatch.setenv("MS_FLOW_WORKSPACE_ROOT", str(tmp_path))

    app = QApplication.instance() or QApplication(["amdockvs-projects-test"])
    runtime = AMDockVSRuntime()
    widget = ProjectsWidget(runtime=runtime)
    try:
        assert [manifest.app_id for manifest in widget._owned_backend.list_apps()] == ["amdockvs"]
    finally:
        widget.close()
        app.processEvents()
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_ui_opens_molecule_view_for_active_project(tmp_path, monkeypatch):
    _patch_fake_home(monkeypatch, tmp_path)

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    window = None
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_project", folder=tmp_path / "project", description="ui test")
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        if window._app_widget is not None:
            assert isinstance(window._app_widget, ApplicationWidget)
            assert window._app_widget.get_total_projects() >= 1

        snapshot = window.monitor_dock.refresh_now()
        view = window.central_widget.open_or_focus_view("workspace.molecules")
        app.processEvents()
        assert view is not None
        assert isinstance(view, MoleculeWidget)
        assert window.windowTitle() == "AMDockVS - ui_project"
        assert snapshot is not None
        assert snapshot.has_project is True
        assert snapshot.project_name == "ui_project"

        jobs_view = window.open_jobs_monitor()
        app.processEvents()
        assert jobs_view is not None
        assert not window.monitor_dock.isVisible()

        jobs_index = window.central_widget.main_content_tabs.currentIndex()
        window.central_widget.on_tab_close(jobs_index)
        app.processEvents()
        assert window.monitor_dock.isVisible()
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_ui_jobs_indicator_shows_when_leaving_jobs_tab(tmp_path, monkeypatch):
    _patch_fake_home(monkeypatch, tmp_path)

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    window = None
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_project", folder=tmp_path / "project", description="ui test")
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        window.central_widget.open_or_focus_view("workspace.molecules")
        window.open_jobs_monitor()  # hides the dock, jobs view becomes current
        app.processEvents()
        # On the Jobs tab the full view is the surface, so no status-bar indicator.
        assert not window.monitor_dock.isVisible()
        assert not window._status_bar.jobs_indicator.isVisible()

        # Leave the Jobs tab WITHOUT closing it (jobs view stays as a background tab).
        window.central_widget.open_or_focus_view("workspace.molecules")
        app.processEvents()
        # Dock is still hidden and Jobs isn't the current tab -> the status-bar
        # indicator must appear so a monitor indicator is always visible.
        assert not window.monitor_dock.isVisible()
        assert window._status_bar.jobs_indicator.isVisible()
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_ui_toolbar_actions_track_open_views(tmp_path, monkeypatch):
    _patch_fake_home(monkeypatch, tmp_path)

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    window = None
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_catalog_sync", folder=tmp_path / "project", description="ui test")
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        # Data views are checkable actions in the top toolbar (checked == tab open);
        # tools are checkable buttons in the left bar (checked == mounted in the panel).
        # Ligands/Receptors open by default; Molecules and Docking Studio start closed.
        assert set(window._tool_action_buttons) >= {DOCKING_VIEW_ID}
        assert set(window._catalog_actions) >= {MOLECULES_VIEW_ID, LIGANDS_VIEW_ID}
        assert window._catalog_actions[LIGANDS_VIEW_ID].isChecked()
        assert not window._catalog_actions[MOLECULES_VIEW_ID].isChecked()

        # A catalog table opens as a central tab and checks its toolbar action.
        window.open_or_focus_view(MOLECULES_VIEW_ID)
        app.processEvents()
        assert window._catalog_actions[MOLECULES_VIEW_ID].isChecked()
        assert window._catalog_actions[LIGANDS_VIEW_ID].isChecked()

        # A tool (Docking Studio) opens in the LEFT tool panel — not a tab — and presses
        # its own button.
        tabs_before = window.central_widget.main_content_tabs.count()
        window.open_or_focus_view(DOCKING_VIEW_ID)
        app.processEvents()
        assert window._active_tool == DOCKING_VIEW_ID
        assert window.tools_dock.isVisible()
        assert window.central_widget.main_content_tabs.count() == tabs_before  # no tab added
        assert window._tool_action_buttons[DOCKING_VIEW_ID].isChecked()

        # Hiding the tool panel releases the tool's button.
        window.dock_manager.toggle("tools", False)
        app.processEvents()
        assert window._active_tool is None
        assert not window._tool_action_buttons[DOCKING_VIEW_ID].isChecked()

        molecules_index = next(
            index
            for index in range(window.central_widget.main_content_tabs.count())
            if window.central_widget.main_content_tabs.widget(index) is window.central_widget.open_view(MOLECULES_VIEW_ID)
        )
        window.central_widget.on_tab_close(molecules_index)
        app.processEvents()
        assert not window._catalog_actions[MOLECULES_VIEW_ID].isChecked()
        assert window._catalog_actions[LIGANDS_VIEW_ID].isChecked()
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_ui_opens_selected_project_in_place_when_no_active_project(tmp_path, monkeypatch):
    _patch_fake_home(monkeypatch, tmp_path)

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    runtime = AMDockVSRuntime()
    window = None
    opened: list[str] = []
    asked: list[bool] = []
    refreshed: list[bool] = []
    retitled: list[bool] = []
    try:
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        def _fake_open_project(project_id: str):
            opened.append(project_id)
            return SimpleNamespace(id=project_id, name="Selected Project")

        monkeypatch.setattr(runtime, "open_project", _fake_open_project)
        monkeypatch.setattr(
            "amdockvs.ui.main_window.QMessageBox.question",
            lambda *args, **kwargs: asked.append(True),
        )
        monkeypatch.setattr(window.monitor_bridge, "request_refresh", lambda: refreshed.append(True))
        monkeypatch.setattr(window, "_sync_window_title", lambda: retitled.append(True))
        # The faked open_project leaves no project_db behind, and the default views build
        # real tables on it. Routing is what this test is about; the views opening for a real
        # project is covered by test_amdock_ui_domain_views_show_docking_summaries.
        monkeypatch.setattr(window, "_open_default_views", lambda: None)
        window._on_application_project_requested("project-123")
        app.processEvents()

        assert opened == ["project-123"]
        assert asked == []
        assert refreshed == [True]
        assert retitled == [True]
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_ui_domain_views_show_docking_summaries(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    pytest.importorskip("meeko")
    pytest.importorskip("vina")
    _patch_fake_home(monkeypatch, tmp_path)

    from test_amdock_runtime import _make_receptor_pdb, _make_smiles_file

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    runtime = AMDockVSRuntime()
    window = None
    try:
        runtime.create_project(name="ui_domain_project", folder=tmp_path / "ui_domain_project", description="ui domain")
        ligands_file = tmp_path / "ui_domain_ligands.smi"
        receptor_file = tmp_path / "ui_domain_receptor.pdb"
        _make_smiles_file(ligands_file, count=4)
        _make_receptor_pdb(receptor_file)

        load_jobs = runtime.loader.load_ligands([ligands_file], batch_size=2, executor_name="thread")
        load_jobs.extend(runtime.loader.load_receptors([receptor_file], batch_size=2, executor_name="thread"))
        runtime.wait_for_jobs(load_jobs, timeout_s=120)
        descriptor_job = runtime.qsar.compute_descriptors(batch_size=2, executor_name="thread")
        runtime.wait_for_jobs([descriptor_job], timeout_s=120)
        receptor = next(runtime.molecules.stream(runtime.molecules.select(role="receptor", limit=1)))
        runtime.docking.set_grid(
            receptor_id=int(receptor.id or 0),
            center=(12.0, 13.0, 10.0),
            size=(20.0, 20.0, 20.0),
        )
        receptor_set = runtime.molecules.create_set(
            runtime.molecules.select(role="receptor"),
            name="ui_receptor_set",
            kind="snapshot",
        )
        gen3d_job = runtime.chemistry.generate_3d_ligands(batch_size=2, executor_name="thread")
        runtime.wait_for_jobs([gen3d_job], timeout_s=120)
        prep_jobs = [
            runtime.docking.prepare_ligands(batch_size=2, executor_name="thread"),
            runtime.docking.prepare_receptors(batch_size=1, executor_name="thread"),
        ]
        runtime.wait_for_jobs(prep_jobs, timeout_s=120)
        docking_job = runtime.docking.run(receptor_set=receptor_set, batch_size=4, executor_name="thread")
        runtime.wait_for_jobs([docking_job], timeout_s=120)

        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        results_view = window.open_complex_results()
        activity_view = window.central_widget.open_or_focus_view("workspace.ligand_activity")
        docking_view = window.central_widget.open_or_focus_view("workspace.docking")
        app.processEvents()

        # The domain views open with the right widget types; the underlying docking data
        # (4 ligand-receptor results) is verified via the runtime rather than by asserting on
        # volatile per-widget card/table internals.
        # Results is the pivot host now; the hits table is the page it opens on.
        assert isinstance(results_view.current_page(), ComplexWidget)
        assert isinstance(activity_view, LigandActivityWidget)
        assert isinstance(docking_view, DockingStudioWidget)
        # Run Scope must not be collapsible: a collapsible group box re-shows every child on
        # expand, which stuck the busy overlay's "Loading…" on screen for good.
        assert not hasattr(docking_view.req_box, "toggleCollapsed")

        # Split buttons: body = the primary action, arrow = "Save to workflow".
        for button in (docking_view.prepare_ligands_button,
                       docking_view.prepare_receptor_button,
                       docking_view.run_button):
            assert button.popupMode() == QToolButton.MenuButtonPopup
            assert [a.text() for a in button.menu().actions()] == ["Save to workflow…"]

        # Live "prepared / total": polls only while a job runs AND the Run step is up, and a
        # repeated snapshot must not restart the countdown (the bridge ticks every 500 ms).
        timer = docking_view._prep_poll_timer
        starts, real_start = [], timer.start
        timer.start = lambda *a: (starts.append(1), real_start(*a))[1]
        docking_view.stepper.set_current_index(3)
        docking_view.on_jobs_snapshot(SimpleNamespace(jobs_active=1))
        docking_view.on_jobs_snapshot(SimpleNamespace(jobs_active=2))
        assert timer.isActive() and len(starts) == 1
        docking_view.on_jobs_snapshot(SimpleNamespace(jobs_active=0))
        assert not timer.isActive()
        docking_view.on_jobs_snapshot(SimpleNamespace(jobs_active=1))
        docking_view.stepper.set_current_index(0)
        assert not timer.isActive()

        # 2D interaction diagram: its own dock under PyMOL, driven by the same pose selection.
        # It only draws the saved JSON (C6); "Build diagram" belongs to Selected Result, which
        # owns the pose and lists the interactions of that same pass.
        assert window.dockWidgetArea(window.diagram_dock) == Qt.RightDockWidgetArea
        hit = runtime.docking.top_hits(limit=1)[0]
        window.load_hit_in_pymol(hit, 1)
        assert hit.ligand_name in window.diagram_dock.status.text()
        assert not hasattr(window.diagram_dock, "render_button")
        hits_page = results_view.current_page()
        assert hits_page.build_diagram_btn.text() == "Build diagram"

        # PyMOL for a docking pose: the receptor is loaded once and only the pose is swapped,
        # but a wiped scene (switching tabs runs cmd.delete("all")) must bring it back instead
        # of trusting a remembered "already loaded" flag.
        class _Cmd:
            def __init__(self):
                self.objects: list[str] = []
            def get_names(self, _kind):
                return list(self.objects)
            def delete(self, name):
                self.objects = [] if name == "all" else [o for o in self.objects if o != name]
            def load(self, _path, name):
                self.objects.append(name)
            def create(self, name, _source, _source_state=0, _target_state=0):
                self.objects.append(name)  # one pose copied out as a single-state object
            def __getattr__(self, _name):  # zoom/show/set/color/select: don't care
                return lambda *a, **k: None

        window.pymol_dock = SimpleNamespace(cmd=_Cmd(), show=lambda: None)
        window._load_hit_in_pymol(hit, 1)
        assert window.pymol_dock.cmd.objects == [f"receptor_{hit.receptor_id}", "amdock_result_pose"]
        window.pymol_dock.cmd.delete("all")
        window._load_hit_in_pymol(hit, 1)
        assert f"receptor_{hit.receptor_id}" in window.pymol_dock.cmd.objects

        assert len(runtime.docking.list_results()) == 4
        assert runtime.docking.result_stats().completed_results == 4
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_ui_failure_message_is_summarized():
    assert AMDockVSMainWindow._summarize_failure_message(
        "prepare_ligands requires ligands with 3D conformers. Missing has_3d for 17 ligand(s). "
        "Run runtime.chemistry.generate_ligand_3d(...) before Vina preparation."
    ) == (
        "17 ligand(s) do not have 3D conformers.\n"
        "Generate 3D coordinates before running ligand preparation."
    )


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_ui_can_create_ligand_set_from_table_selection(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    from test_amdock_runtime import _make_smiles_file

    _patch_fake_home(monkeypatch, tmp_path)
    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_ligand_sets", folder=tmp_path / "ui_ligand_sets", description="ligand set ui")
        ligands_file = tmp_path / "ligand_sets.smi"
        _make_smiles_file(ligands_file, count=3)
        job_ids = runtime.loader.load_ligands([ligands_file], batch_size=2, executor_name="thread")
        runtime.wait_for_jobs(job_ids, timeout_s=120)

        widget = LigandWidget(runtime=runtime)
        widget.show()
        app.processEvents()
        assert widget.table is not None
        widget.table._table.selectAll()
        app.processEvents()

        shown_messages: list[str] = []
        monkeypatch.setattr(
            "amdockvs.ui.catalog.ligands.QInputDialog.getText",
            lambda *args, **kwargs: ("selected_ligands", True),
        )
        monkeypatch.setattr(
            "amdockvs.ui.catalog.ligands.QMessageBox.information",
            lambda *args, **kwargs: shown_messages.append(str(args[2] if len(args) > 2 else kwargs.get("text", ""))),
        )
        # The table action passes the selected rows straight in (no view-lookup context).
        _create_ligand_set(runtime, widget.table.get_selected_objects())

        assert shown_messages
        assert "Created set #" in shown_messages[-1]
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_tool_buttons_are_flat_and_contribute_contextual_data_views(tmp_path, monkeypatch):
    """Left bar = one checkable button per tool (no menus); the active tool's result
    tables appear in the top data toolbar and retire when it closes."""
    _patch_fake_home(monkeypatch, tmp_path)

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    window = None
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_project", folder=tmp_path / "project", description="ui test")
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        tool_ids = {view_id for _a, _t, view_id, _i, _o in window._TOOL_ACTIONS}
        assert tool_ids == set(window._TOOL_VIEW_IDS)  # left bar names tools, nothing else
        assert not any(b.menu() for b in window._tool_action_buttons.values())  # no popups
        assert not window.dock_manager.buttons["tools"].isVisible()  # redundant button gone

        # Result views outlive the tool that produced them: reachable with every tool closed.
        standing = {v for group in window._STANDING_DATA_VIEWS for _l, v, _i in group}
        assert COMPLEXES_VIEW_ID in standing
        # Off-target and redocking are pivots of Docking Results, not entries of their own.
        assert not {OFFTARGET_VIEW_ID, REDOCKING_VIEW_ID} & set(window._catalog_actions)
        assert standing <= set(window._catalog_actions)

        # Prep Status is not a tab any more: it is the child table of Ligands/Receptors, so
        # it occupies the auxiliary zone while Docking is open and hands it back on close.
        assert window._TOOL_AUX_VIEWS[DOCKING_VIEW_ID] == PREP_STATUS_VIEW_ID
        assert PREP_STATUS_VIEW_ID not in window._catalog_actions
        assert window._aux_occupant == window._AUX_DETAILS

        window.open_tool(DOCKING_VIEW_ID)
        app.processEvents()
        assert window._active_tool == DOCKING_VIEW_ID
        assert window._tool_action_buttons[DOCKING_VIEW_ID].isChecked()
        assert window._aux_occupant == PREP_STATUS_VIEW_ID
        assert PREP_STATUS_VIEW_ID not in window._catalog_actions  # never a tab

        window._on_tool_action(DOCKING_VIEW_ID, False)  # unchecking closes the tool
        app.processEvents()
        assert window._active_tool is None
        assert not window._tool_action_buttons[DOCKING_VIEW_ID].isChecked()
        assert window._aux_occupant == window._AUX_DETAILS  # slot handed back
        assert standing <= set(window._catalog_actions)  # results survived the tool closing
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_prep_status_role_selector_swaps_the_filter_in_place(tmp_path, monkeypatch):
    """Ligand/receptor prep status is one view over the `engines` table, not two: the
    role combo swaps the base filter instead of registering a second view."""
    _patch_fake_home(monkeypatch, tmp_path)

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="prep_roles", folder=tmp_path / "project", description="ui test")
        view = EngineStatePrepView(runtime=runtime)
        app.processEvents()

        def role() -> str:
            spec = next(f for f in view.table._builder.active_filters if f.field == "role_type")
            return str(spec.value)

        assert role() == "ligand"
        assert view._role_selector.count() == 2

        view._role_selector.setCurrentIndex(view._role_selector.findData("receptor"))
        app.processEvents()
        assert role() == "receptor"
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_ui_is_locked_until_a_project_is_open(tmp_path, monkeypatch):
    """No project -> only welcome/File/Settings; opening one turns everything on."""
    _patch_fake_home(monkeypatch, tmp_path)

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    window = None
    runtime = AMDockVSRuntime()
    try:
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        assert not window._catalog_toolbar.isEnabled()
        assert not window.monitor_dock.isEnabled()
        assert not any(action.isEnabled() for action in window._project_actions)
        assert window.central_widget.isEnabled()  # the welcome screen stays clickable

        window._set_project_ui_enabled(True)
        assert window._catalog_toolbar.isEnabled()
        assert window.monitor_dock.isEnabled()
        assert all(action.isEnabled() for action in window._project_actions)
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()
