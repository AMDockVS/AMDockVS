import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Import the runtime before PySide6; shiboken's feature loader otherwise trips over
# pydantic's lazy migration shim while ms_flow is being imported.
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QGroupBox, QPushButton

from amdockvs.ui.catalog.molecules import MoleculeWidget
from amdockvs.ui.tools.molecules.build import MoleculeBuildWidget
from amdockvs.core.vocab import MoleculeType
from ms_components.ms_monitor.models import JobMonitorState
from ms_components.tool_panel import ToolSection


class _FakeMolecules:
    def __init__(self):
        self.selections = []
        self.shape = (False, 0)  # (library is sharded, ligand rows)

    def library_shape(self):
        return self.shape

    def select(self, *, molecule_type):
        selection = SimpleNamespace(molecule_type=molecule_type, filters={})
        self.selections.append(selection)
        return selection

    @staticmethod
    def filter(scope, *, filters):
        return SimpleNamespace(
            molecule_type=scope.molecule_type,
            filters={**scope.filters, **filters},
        )


class _FakeChemistry:
    def __init__(self):
        self.calls = []

    def protonate_ligands(self, **config):
        self.calls.append(("protonate_ligands", config))
        return "small-job"

    def run_ligand_pipeline(self, **config):
        self.calls.append(("run_ligand_pipeline", config))
        return "pipeline-job"

    def protonate_receptors(self, **config):
        self.calls.append(("protonate_receptors", config))
        return "protein-job"

    def fix_receptors(self, **config):
        self.calls.append(("fix_receptors", config))
        return "fix-job"


@pytest.fixture
def build_widget():
    app = QApplication.instance() or QApplication(["amdockvs-build-test"])
    runtime = SimpleNamespace(molecules=_FakeMolecules(), chemistry=_FakeChemistry())
    widget = MoleculeBuildWidget(runtime=runtime)
    yield widget
    widget.close()
    app.processEvents()


def _group_titles(page):
    return [box.title() for box in page.findChildren(QGroupBox)]


def test_build_uses_molecule_type_tabs_and_requested_groups(build_widget):
    assert build_widget.tabs.count() == 2
    assert [build_widget.tabs.tabText(index) for index in range(2)] == [
        "Small molecules",
        "Proteins",
    ]
    assert [section.title() for section in build_widget.small_molecules_tab.findChildren(ToolSection)] == [
        "Protonation",
        "3D generation",
        "Minimization",
        "Conformer ensemble",
        "Advanced",
    ]
    assert [section.title() for section in build_widget.proteins_tab.findChildren(ToolSection)] == [
        "3D generation",
        "Fix structure",
        "Protonation",
        "Minimization",
        "Advanced",
    ]
    assert not _group_titles(build_widget.proteins_tab)
    # 3D generation (ESMFold API) is wired, first in the chain and off by default
    assert build_widget.receptor_predict_section.isEnabled()
    assert not build_widget.receptor_predict_section.isChecked()

    button_texts = [button.text() for button in build_widget.findChildren(QPushButton)]
    assert "Refresh" not in button_texts
    assert "Open Jobs" not in button_texts
    assert not any("Ligand" in text or "Receptor" in text for text in button_texts)
    assert [
        build_widget.ligand_protonation_method.itemText(index)
        for index in range(build_widget.ligand_protonation_method.count())
    ] == ["Dimorphite-DL", "OpenBabel", "pKasso", "Explicit Hs (RDKit)"]
    assert build_widget._active_protonation_method() == "dimorphite"


def test_small_molecule_protonation_method_switches_compact_form(build_widget):
    build_widget.ligand_protonation_method.setCurrentIndex(3)

    assert build_widget._active_protonation_method() == "explicit_hs"
    assert dict(build_widget._build_steps())["protonate"]["method"] == "explicit_hs"
    explicit_hs_note, = build_widget.ligand_protonation_rows["explicit_hs"]
    assert not explicit_hs_note.isHidden()
    assert build_widget.pkasso_model.isHidden()
    # One pH serves every pH-aware method; Explicit Hs predicts no states.
    assert not build_widget.ligand_protonation_ph.isEnabled()

    build_widget.ligand_protonation_method.setCurrentIndex(2)
    assert not build_widget.pkasso_model.isHidden() and explicit_hs_note.isHidden()
    assert build_widget.ligand_protonation_ph.isEnabled()


def test_build_actions_are_right_aligned_and_tabs_fix_operation_scope(build_widget):
    assert build_widget.small_molecules_tab.action_bar.isAncestorOf(build_widget.run_build_ligands_button)
    assert build_widget.proteins_tab.action_bar.isAncestorOf(build_widget.run_proteins_button)
    for section in build_widget._protein_sections():
        section.setChecked(False)
    assert not build_widget.run_proteins_button.isEnabled()

    build_widget.tabs.setCurrentWidget(build_widget.proteins_tab)
    assert build_widget._selected_molecule_type() == MoleculeType.PROTEIN
    assert build_widget._cfg_build_ligands()["ligands"].molecule_type == MoleculeType.SMALL_MOLECULE
    assert build_widget._cfg_protonate_receptors()["receptors"].molecule_type == MoleculeType.PROTEIN


def test_build_ops_run_on_whatever_the_molecules_table_shows(build_widget, monkeypatch):
    # No scope selector here: the tool acts on the rows the catalog table shows.
    catalog = SimpleNamespace(scope_ids=lambda: None)
    monkeypatch.setattr(build_widget, "_catalog_molecules_widget", lambda: catalog)
    assert build_widget._scope(MoleculeType.SMALL_MOLECULE).filters == {}

    catalog.scope_ids = lambda: [12, 31]
    assert build_widget._scope(MoleculeType.SMALL_MOLECULE).filters == {"id__in": [12, 31]}
    assert build_widget._scope(MoleculeType.PROTEIN).filters == {"id__in": [12, 31]}


def test_only_fix_structure_can_go_back_to_the_imported_structure(build_widget):
    # Inside a tool the Molecules table shows the working structure; only the op that
    # explicitly needs the untouched import says otherwise.
    assert build_widget.receptor_fix_structure.currentData() == "current"
    build_widget.receptor_fix_structure.setCurrentIndex(1)
    assert build_widget._cfg_fix_receptors()["structure_source"] == "original"

    for cfg in (
        build_widget._cfg_build_ligands(),
        build_widget._cfg_protonate_receptors(),
        build_widget._cfg_minimize_receptors(),
    ):
        assert "structure_source" not in cfg


def test_build_run_buttons_use_chemistry_api(build_widget, monkeypatch):
    monkeypatch.setattr(build_widget, "_require_executable", lambda **_kwargs: True)
    monkeypatch.setattr(build_widget, "_require_optional_module", lambda **_kwargs: True)

    build_widget._protonation_tool_ready["dimorphite"] = True
    build_widget._sync_build_button()

    build_widget.run_build_ligands_button.click()
    build_widget.run_proteins_button.click()

    ligand_call, fix_call, protein_call = build_widget.runtime.chemistry.calls
    assert ligand_call[0] == "run_ligand_pipeline"
    # Minimization is not ticked by default.
    assert [name for name, _ in ligand_call[1]["steps"]] == ["protonate", "generate_3d"]
    assert ligand_call[1]["ligands"].molecule_type == MoleculeType.SMALL_MOLECULE
    # Fix and protonation are ticked by default; minimization is not. The chain is explicit.
    assert fix_call[0] == "fix_receptors" and fix_call[1]["depends_on"] is None
    assert protein_call[0] == "protonate_receptors"
    assert protein_call[1]["receptors"].molecule_type == MoleculeType.PROTEIN
    assert protein_call[1]["method"] == "reduce"
    assert protein_call[1]["depends_on"] == ["fix-job"]


def test_protein_run_follows_the_whole_chain(build_widget, monkeypatch):
    monkeypatch.setattr(build_widget, "_require_executable", lambda **_kwargs: True)
    monkeypatch.setattr(build_widget, "_require_optional_module", lambda **_kwargs: True)
    cancelled = []
    build_widget.runtime.cancel_job = cancelled.append
    bar = build_widget.proteins_tab.action_bar
    jobs = build_widget.protein_jobs

    build_widget.run_proteins_button.click()
    assert bar.is_running and (bar.headline.text(), bar.detail.text()) == ("Queued", "step 1 of 2")

    jobs.on_upserted("fix-job", JobMonitorState(job_id="fix-job", status="running", chunks_total=4, chunks_done=1))
    assert bar.headline.text() == "Fixing — 1 / 4 batches"
    assert "step 1 of 2" in bar.detail.text()

    jobs.on_finished("fix-job", "completed")
    assert bar.is_running and bar.detail.text() == "step 2 of 2"
    bar.cancel_button.click()
    assert cancelled == ["protein-job"]  # the finished job is left alone
    jobs.on_finished("protein-job", "canceled")
    assert not bar.is_running and bar.headline.text() == "Protein build cancelled"


def test_build_runs_only_ticked_steps_and_targets_shards_in_a_sharded_project(build_widget):
    build_widget._protonation_tool_ready["dimorphite"] = True
    build_widget.ligand_3d_section.setChecked(False)
    assert [name for name, _ in build_widget._build_steps()] == ["protonate"]
    build_widget.ligand_protonation_section.setChecked(False)
    build_widget.ligand_min_section.setChecked(False)
    assert not build_widget.run_build_ligands_button.isEnabled()

    # No shards: no target choice, rows with a batch size.
    assert build_widget.small_molecules_tab.top_bar.isHidden()
    assert build_widget._cfg_build_ligands()["ligands"] is not None

    build_widget.runtime.molecules.shape = (True, 0)
    build_widget._sync_ligand_target()
    assert not build_widget.small_molecules_tab.top_bar.isHidden()
    assert build_widget._cfg_build_ligands()["ligands"] is None  # -> run_shard_pipeline
    assert build_widget.batch_size_ligands.isHidden()  # one shard per task, fixed
    assert not build_widget.ligand_conformers_section.isEnabled()
    build_widget.ligand_conformers_section.setChecked(True)
    assert "conformers" not in dict(build_widget._build_steps())  # shards refuse ensembles
    # No reference rows yet: the rows choice is there but cannot be taken.
    rows_index = build_widget.ligand_target.findData("rows")
    assert not build_widget.ligand_target.model().item(rows_index).isEnabled()

    build_widget.runtime.molecules.shape = (True, 3)
    build_widget._sync_ligand_target()
    build_widget.ligand_target.setCurrentIndex(rows_index)
    assert build_widget._cfg_build_ligands()["ligands"] is not None
    assert not build_widget.batch_size_ligands.isHidden()


def test_conformers_run_before_minimize_with_the_shared_seed(build_widget):
    build_widget.ligand_random_seed.setValue(42)
    build_widget.ligand_conformers_section.setChecked(True)
    build_widget.ligand_min_section.setChecked(True)

    steps = build_widget._build_steps()
    assert [name for name, _ in steps] == ["protonate", "generate_3d", "conformers", "minimize"]
    params = dict(steps)
    assert params["generate_3d"]["random_seed"] == params["conformers"]["random_seed"] == 42
    assert params["minimize"]["prune_rms_thresh"] == params["conformers"]["prune_rms_thresh"] > 0


def test_build_run_follows_its_job_in_the_action_bar(build_widget):
    cancelled = []
    build_widget.runtime.cancel_job = cancelled.append
    build_widget._protonation_tool_ready["dimorphite"] = True
    build_widget._sync_build_button()
    bar = build_widget.small_molecules_tab.action_bar

    build_widget.run_build_ligands_button.click()
    assert bar.is_running and bar.headline.text() == "Queued"
    assert not build_widget.ligand_protonation_section.isEnabled()  # settings locked mid-run

    jobs = build_widget.ligand_jobs
    jobs.on_upserted("other-job", JobMonitorState(job_id="other-job", status="running"))
    assert bar.headline.text() == "Queued"
    running = JobMonitorState(
        job_id="pipeline-job", status="running", chunks_total=10, chunks_done=4, chunks_failed=1, chunks_running=3
    )
    jobs.on_upserted("pipeline-job", running)
    assert bar.headline.text() == "Building — 4 / 10 batches"
    assert bar.detail.text().startswith("Estimating time… · 3 running · 1 failed")

    bar.cancel_button.click()
    assert cancelled == ["pipeline-job"]

    jobs.on_finished("pipeline-job", "canceled")
    assert not bar.is_running and bar.headline.text() == "Build cancelled"
    assert not build_widget.run_build_ligands_button.isHidden()
    jobs.on_finished("pipeline-job", "completed")  # already settled: ignored
    assert bar.headline.text() == "Build cancelled"


def test_pdb2pqr_enables_its_specific_protein_options(build_widget):
    assert not build_widget.receptor_protonation_ph.isEnabled()
    assert not build_widget.receptor_protonation_forcefield.isEnabled()

    build_widget.receptor_protonation_method.setCurrentIndex(1)

    assert build_widget.receptor_protonation_ph.isEnabled()
    assert build_widget.receptor_protonation_forcefield.isEnabled()


def test_molecules_structure_source_lives_and_dies_with_tool_scope(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication(["amdockvs-molecule-source-test"])
    runtime = AMDockVSRuntime()
    widget = None
    try:
        runtime.create_project(name="source_scope", folder=tmp_path / "source_scope")
        widget = MoleculeWidget(runtime=runtime)
        reloads = []
        monkeypatch.setattr(widget, "_reload_selected_structure", lambda: reloads.append(True))

        widget.push_scope("build", structure_source="current")
        assert widget._structure_sources == {"build": "current"}

        widget.pop_scope("build")
        assert widget._structure_sources == {}
        assert reloads == [True, True]
    finally:
        if widget is not None:
            widget.close()
        runtime.shutdown()
        app.processEvents()


def test_a_finished_build_lists_the_molecules_it_excluded(build_widget, monkeypatch):
    from PySide6.QtWidgets import QTreeWidget

    from amdockvs.ui.tools.molecules import build as build_module

    monkeypatch.setattr(build_module, "run_async", lambda fn, on_result, *a, **k: on_result(fn()))
    asked = []
    build_widget.runtime.molecules.excluded_since = lambda since: asked.append(since) or [
        (7, "ZINC7", "generate_3d: RDKit could not generate a 3D conformer."),
        (8, "ZINC8", "standardize: Explicit valence for atom # 3 N, 4, is greater than permitted"),
        (9, "ZINC9", "generate_3d: RDKit could not generate a 3D conformer."),
        (10, "ZINC10", "standardize: Explicit valence for atom # 12 N, 4, is greater than permitted"),
    ]
    build_widget._protonation_tool_ready["dimorphite"] = True
    build_widget._sync_build_button()

    build_widget.run_build_ligands_button.click()
    build_widget.ligand_jobs.on_finished("pipeline-job", "completed")

    assert asked and asked[0] <= __import__("datetime").datetime.now()
    tree = build_widget.failures_dialog.findChild(QTreeWidget)
    groups = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    assert len(groups) == 2  # four failures, two causes: atom numbers don't split a cause
    assert groups[0].childCount() == groups[1].childCount() == 2
    first = groups[0].child(0) if "3D" in groups[0].text(0) else groups[1].child(0)
    assert [first.text(c) for c in range(3)] == ["ZINC7", "7", "generate_3d: RDKit could not generate a 3D conformer."]

def test_a_protonation_tool_installed_from_settings_becomes_the_method_in_use(build_widget, monkeypatch):
    from amdockvs.ui.shell.settings_tools import ExternalToolsPage, tool_events
    from amdockvs.ui.tools.molecules import build as build_module

    installed = set()
    build_widget.runtime.chemistry.small_molecule_protonation_tool_status = (
        lambda name: SimpleNamespace(installed=name in installed, message="")
    )
    monkeypatch.setattr(build_module, "run_async", lambda job, done, **_kw: done(job()))
    build_widget._refresh_protonation_tools()
    assert build_widget.ligand_protonation_method.currentData() == "dimorphite"

    # The Settings page announces what turned up between two checks, and only that.
    page = SimpleNamespace(_rows={}, install_all_button=SimpleNamespace(setEnabled=lambda _on: None))
    announced = []
    tool_events.installed.connect(announced.append)
    statuses = lambda: [SimpleNamespace(tool_id=t, installed=t in installed) for t in ("openbabel", "pkasso")]
    ExternalToolsPage._apply(page, statuses())  # first look: nothing is "new"
    installed.add("openbabel")
    ExternalToolsPage._apply(page, statuses())
    tool_events.installed.disconnect(announced.append)

    assert announced == ["openbabel"]
    assert build_widget.ligand_protonation_method.currentData() == "openbabel"
    assert build_widget.ligand_protonation_section.isChecked()


def test_a_mass_failure_lists_a_few_molecules_per_cause_and_counts_the_rest(build_widget):
    from PySide6.QtWidgets import QTreeWidget

    from amdockvs.ui.common.failures import PER_CAUSE, show_failures
    from ms_components.tool_panel import format_count

    rows = [(i, f"Z{i}", f"generate_3d: embedding failed after {i} tries") for i in range(1, 2001)]
    dialog = show_failures(build_widget, "Build", rows)
    tree = dialog.findChild(QTreeWidget)
    [cause] = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]  # numbers don't split it
    assert cause.childCount() == PER_CAUSE + 1
    assert cause.child(PER_CAUSE).text(0) == f"… and {format_count(2000 - PER_CAUSE)} more"
    dialog.close()
