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
from PySide6.QtWidgets import QApplication, QGroupBox, QLabel, QPushButton, QWidget

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

    def excluded_ids(self):
        return set()

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
        "Ligands from",  # shown in a sharded project only
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
    assert build_widget._cfg_build_ligands()[0]["ligands"].molecule_type == MoleculeType.SMALL_MOLECULE
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
        *build_widget._cfg_build_ligands(),
        build_widget._cfg_protonate_receptors(),
        build_widget._cfg_minimize_receptors(),
    ):
        assert "structure_source" not in cfg


def test_build_run_buttons_use_chemistry_api(build_widget, monkeypatch):
    monkeypatch.setattr(build_widget, "_require_executable", lambda **_kwargs: True)

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
    cancelled = []
    build_widget.runtime.cancel_job = cancelled.append
    bar = build_widget.proteins_tab.action_bar
    jobs = build_widget.protein_jobs

    build_widget.run_proteins_button.click()
    assert bar.is_running and (bar.headline.text(), bar.detail.text()) == ("Queued", "step 1 of 2")

    jobs.on_upserted("fix-job", JobMonitorState(job_id="fix-job", status="running", chunks_total=4, chunks_done=1))
    assert bar.headline.text() == "Fixing - 1 / 4 batches"
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

    # No shards: no store choice, rows with a batch size.
    assert build_widget.ligand_source_section.isHidden()
    assert [cfg["ligands"] is None for cfg in build_widget._cfg_build_ligands()] == [False]

    build_widget.runtime.molecules.shape = (True, 0)
    build_widget._sync_ligand_target()
    assert not build_widget.ligand_source_section.isHidden()
    # No rows yet: the table box is there but cannot be ticked.
    rows_box = build_widget.ligand_sources.boxes["rows"]
    assert not rows_box.isEnabled() and not rows_box.isChecked()
    assert [cfg["ligands"] is None for cfg in build_widget._cfg_build_ligands()] == [True]  # -> run_shard_pipeline
    assert build_widget.batch_size_ligands.isHidden()  # one shard per task, fixed
    assert not build_widget.ligand_conformers_section.isEnabled()
    build_widget.ligand_conformers_section.setChecked(True)
    assert "conformers" not in dict(build_widget._build_steps())  # shards refuse ensembles

    # Rows arrive: the table is ticked again, both stores run, ensembles only for the rows.
    build_widget.runtime.molecules.shape = (True, 3)
    build_widget._sync_ligand_target()
    assert rows_box.isEnabled() and rows_box.isChecked()
    rows, shards = build_widget._cfg_build_ligands()
    assert rows["ligands"] is not None and shards["ligands"] is None
    assert "conformers" in dict(rows["steps"]) and "conformers" not in dict(shards["steps"])
    assert not build_widget.batch_size_ligands.isHidden()
    build_widget.ligand_sources.boxes["shards"].setChecked(False)
    assert [cfg["ligands"] is None for cfg in build_widget._cfg_build_ligands()] == [False]
    build_widget.ligand_sources.boxes["rows"].setChecked(False)
    assert not build_widget.run_build_ligands_button.isEnabled()


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
    assert bar.headline.text() == "Building - 4 / 10 batches"
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
    build_widget.runtime.molecules.excluded_ids = lambda: {3}
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

    assert asked == [{3}]  # the ids excluded before the run
    tree = build_widget.failures_dialog.findChild(QTreeWidget)
    groups = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    assert len(groups) == 2  # four failures, two causes: atom numbers don't split a cause
    assert groups[0].childCount() == groups[1].childCount() == 2
    first = groups[0].child(0) if "3D" in groups[0].text(0) else groups[1].child(0)
    assert [first.text(c) for c in range(3)] == ["ZINC7", "7", "generate_3d: RDKit could not generate a 3D conformer."]

@pytest.mark.parametrize(("usable", "message"), [
    (False, "GPU: Example GPU (compute 6.1) can't be used by pKasso's PyTorch, "
            "built for compute 7.5 to 12.0; Uni-pKa runs on the CPU."),
    (False, "No GPU is visible to pKasso; Uni-pKa runs on the CPU."),
    (True, "GPU: Example GPU is ready."),
], ids=["unusable-card", "no-gpu", "usable"])
def test_pkasso_offers_the_gpu_only_when_its_pytorch_can_run_on_it(build_widget, monkeypatch, usable, message):
    """pKasso said it was ready whatever the GPU, so the box offered a card its PyTorch had no
    kernels for, and every molecule then failed with "no kernel image is available". Build now says
    what the GPU can do, and the box opens only when it can."""
    from amdockvs.chemistry.protonation import GpuSupport
    from amdockvs.ui.tools.molecules import build as build_module

    chemistry = build_widget.runtime.chemistry
    chemistry.small_molecule_protonation_tool_status = (
        lambda name: SimpleNamespace(installed=True, message=f"{name} is ready.")
    )
    chemistry.pkasso_gpu_support = lambda: GpuSupport(usable, message)
    monkeypatch.setattr(build_module, "run_async", lambda job, done, **_kw: done(job()))
    build_widget.pkasso_model.setCurrentIndex(build_widget.pkasso_model.findData("mixed"))
    assert not build_widget.pkasso_gpu.isEnabled()  # not before the GPU has been asked about

    build_widget._refresh_protonation_tools()

    assert build_widget.pkasso_status.text() == f"pkasso is ready. {message}"
    assert build_widget.pkasso_gpu.isEnabled() is usable
    assert build_widget.pkasso_gpu.toolTip() == message
    build_widget.pkasso_gpu.setChecked(True)
    build_widget.pkasso_model.setCurrentIndex(build_widget.pkasso_model.findData("molgpka"))
    assert not build_widget.pkasso_gpu.isEnabled() and not build_widget.pkasso_gpu.isChecked()  # MolGpKa: no GPU


def test_a_gpu_check_that_raises_still_answers(build_widget, monkeypatch):
    """Its error was dropped, so pKasso's status said "Checking the GPU…" -- and the box's tooltip
    "Checking…" -- for as long as Build stayed open."""
    from amdockvs.ui.tools.molecules import build as build_module

    chemistry = build_widget.runtime.chemistry
    chemistry.small_molecule_protonation_tool_status = (
        lambda name: SimpleNamespace(installed=True, message=f"{name} is ready.")
    )

    def probe():
        raise OSError("the environment went away")

    chemistry.pkasso_gpu_support = probe

    def run_async(job, done, on_error=None, **_kw):
        try:
            result = job()
        except Exception as exc:  # noqa: BLE001 - as run_async hands a job's error over
            on_error(exc)
            return
        done(result)

    monkeypatch.setattr(build_module, "run_async", run_async)
    build_widget.pkasso_model.setCurrentIndex(build_widget.pkasso_model.findData("mixed"))

    build_widget._refresh_protonation_tools()

    said = "GPU check failed (OSError); Uni-pKa runs on the CPU."
    assert build_widget.pkasso_status.text() == f"pkasso is ready. {said}"
    assert build_widget.pkasso_gpu.toolTip() == said
    assert not build_widget.pkasso_gpu.isEnabled()


@pytest.mark.parametrize("width", [360, 480, 560, 640, 760, 900])  # where text wraps varies with the font
@pytest.mark.parametrize("installed", [True, False], ids=["installed", "not-installed"])
@pytest.mark.parametrize("method", ["dimorphite", "openbabel", "pkasso", "explicit_hs"])
def test_every_protonation_row_shows_whole_at_any_width(build_widget, monkeypatch, method, installed, width):
    """The form laid a wrapping note out taller than it counted it when sizing the section, and the
    difference came off the section's bottom: the last row lost its last line. pKasso's status,
    two lines once it says what the GPU can do, showed a line and a half."""
    from PySide6.QtCore import QPoint

    from amdockvs.chemistry.protonation import GpuSupport
    from amdockvs.ui.tools.molecules import build as build_module

    chemistry = build_widget.runtime.chemistry
    chemistry.small_molecule_protonation_tool_status = lambda name: SimpleNamespace(
        installed=installed, message=f"{name} {'is ready' if installed else 'is not installed'}.")
    chemistry.pkasso_gpu_support = lambda: GpuSupport(False, (
        "GPU: Example GPU (compute 6.1) can't be used by pKasso's PyTorch, built for compute 7.5 to "
        "12.0; Uni-pKa runs on the CPU."))

    later = []

    def run_async(job, done, **_kw):
        if job is chemistry.pkasso_gpu_support:  # it takes seconds: its answer comes later
            later.append((job, done))
            return
        try:
            result = job()
        except Exception:  # noqa: BLE001 - the fake runtime answers only what this test is about
            return
        done(result)

    monkeypatch.setattr(build_module, "run_async", run_async)
    # As the tools are warmed under the splash: sized and laid out hidden (grab), its statuses
    # arriving before it is ever shown -- when a label is still 100 px, a widget's first width.
    build_widget.resize(width, 900)
    build_widget.grab()
    build_widget.ligand_protonation_method.setCurrentIndex(build_widget.ligand_protonation_method.findData(method))
    build_widget.ligand_protonation_section.setChecked(True)
    build_widget._refresh_protonation_tools()
    build_widget.show()
    QApplication.processEvents()  # shown with the tool's status alone, as the GPU probe runs
    for job, done in later:  # ... and then the GPU's answer, often a second line, arrives
        done(job())
    for _ in range(5):
        QApplication.processEvents()

    def lines(label: QLabel) -> int:
        """What its text needs at its width, asked of a label with no height bounds: a label's own
        heightForWidth never answers less than its minimum height, so it cannot see one too tall."""
        clone = QLabel(label.text())
        clone.setFont(label.font())
        clone.setWordWrap(True)
        return clone.heightForWidth(label.width())

    # What is seen, wherever it is nested: a row's layout can fit while a label inside it runs over.
    section = build_widget.ligand_protonation_section
    shown = [widget for widget in section.body.findChildren(QWidget) if widget.isVisible() and widget.height()]
    where = f"{method}, {'installed' if installed else 'not installed'}, {width}px"
    for widget in shown:
        bottom = widget.mapTo(section, QPoint(0, widget.height())).y()
        what = type(widget).__name__ + (f" {widget.text()[:40]!r}" if isinstance(widget, QLabel) else "")
        assert bottom <= section.height(), (
            f"{where}: {what} ends at y={bottom}, below its section ({section.height()} tall)")
        if isinstance(widget, QLabel) and widget.wordWrap():
            # Not shorter, which cuts its last line; not taller, which leaves a gap around it.
            assert widget.height() == lines(widget), (
                f"{where}: {what} is {widget.height()} px tall, its lines need {lines(widget)}")
    assert any(isinstance(widget, QLabel) and widget.wordWrap() for widget in shown)


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
    statuses = lambda: [SimpleNamespace(tool_id=t, installed=t in installed, available=True) for t in ("openbabel", "pkasso")]
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
