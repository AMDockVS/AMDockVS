"""Docking Studio reopens on what the project's Run clicks committed to (docking/studio_setup.py)."""
import time

import pytest

from amdockvs.docking.engines.programs import GNINA_PROGRAM, VINA_PROGRAM, get_docking_program
from amdockvs.docking.protocols import PROTOCOL_SCHEMA, protocol_hash
from amdockvs.docking.studio_setup import setup_from_project, setup_from_submissions, submitted_docking


@pytest.fixture
def studio_factory(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    from PySide6.QtWidgets import QApplication

    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.ui.tools.docking import preparation_panel, protocol_editor
    from amdockvs.ui.tools.docking.studio import DockingStudioWidget

    app = QApplication.instance() or QApplication([])  # noqa: F841  (kept alive for the widgets)
    monkeypatch.setattr(protocol_editor, "run_async", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(preparation_panel, "run_async", lambda *_args, **_kwargs: None)
    runtime = AMDockVSRuntime()
    runtime.create_project(name="p", folder=tmp_path / "p", description="")
    studios = []

    def make():
        studios.append(DockingStudioWidget(runtime=runtime))
        studios[-1]._warn = lambda title, message: pytest.fail(f"{title}: {message}")  # never a modal box
        return studios[-1]

    make.runtime = runtime
    make.notices = []
    # The restore runs while the studio is built, so the notice is caught on the class.
    monkeypatch.setattr(protocol_editor.ProtocolEditorWidget, "_setup_notice",
                        lambda _self, problem: make.notices.append(problem))
    yield make
    for studio in studios:
        studio.deleteLater()
    runtime.close_project()


def _submit(runtime, run_id, program, kind="docking", **config):
    """A docking job submitted through MolSuite, as a Run click submits it. It fails (the project
    has no molecules), which does not matter: what was submitted is on record either way."""
    from amdockvs.docking.jobs import DockingJobParams, RedockingJobParams, docking_job, redocking_job

    spec = get_docking_program(program)
    config = spec.validate_config(config)
    protocol = {"schema": PROTOCOL_SCHEMA, "program": program, "label": f"{program} {config}",
                "config": config, "rescoring": [], "hash": protocol_hash(program=program, config=config)}
    job, model = (redocking_job, RedockingJobParams) if kind == "redocking" else (docking_job, DockingJobParams)
    root = runtime.active_context.path
    before = len(submitted_docking(root))
    runtime.submit_job(job, params=model(run_id=run_id, engine=spec.docking_engine, protocol_metadata=protocol,
                                         engine_config=config, output_dir=str(root / "out")).model_dump(mode="python"))
    deadline = time.monotonic() + 10
    while len(submitted_docking(root)) == before:  # the journal writes every few tens of ms
        assert time.monotonic() < deadline, "the submission never reached the journal"
        time.sleep(0.02)


def _set_kind(studio, kind):
    studio.experiment_kind_combo.setCurrentIndex(studio.experiment_kind_combo.findData(kind))


def test_a_project_with_no_runs_starts_from_the_programs_own_defaults(studio_factory):
    from amdockvs.core.configuration import app_config

    settings = app_config(studio_factory.runtime).docking
    for name in ("exhaustiveness", "num_modes", "cpu_per_task"):  # not Settings' to say
        assert not hasattr(settings.vina, name) and not hasattr(settings.gnina, name)
    assert setup_from_project(studio_factory.runtime.active_context.path) == {}
    studio = studio_factory()
    for program in (VINA_PROGRAM, GNINA_PROGRAM):
        defaults = program.validate_config({})
        shown = studio._protocol_config_from_widgets(program.key)
        assert {k: shown[k] for k in ("exhaustiveness", "num_modes", "vina_cpu")} == \
            {k: defaults[k] for k in ("exhaustiveness", "num_modes", "vina_cpu")}
    assert studio_factory.notices == []


def test_it_reopens_on_the_last_run_not_on_an_edit_that_never_ran(studio_factory):
    runtime = studio_factory.runtime
    _submit(runtime, "a", "vina", exhaustiveness=4, scoring_function="vinardo", seed=7)
    _submit(runtime, "a", "adgpu", nrun=7)
    studio = studio_factory()
    studio.exhaustiveness.setValue(14)  # typed after the run, never run

    again = studio_factory()
    assert again.exhaustiveness.value() == 4 and again.scoring_combo.currentText() == "vinardo"
    assert again.vina_seed.value() == 7
    assert again._program_config_widgets["adgpu"]["nrun"].value() == 7
    assert [key for key, check in again._program_checks.items() if check.isChecked()] == ["vina", "adgpu"]
    assert again._run_kind() == "docking" and studio_factory.notices == []


def test_the_last_launch_ticks_and_each_program_keeps_its_own_last_run(studio_factory):
    runtime = studio_factory.runtime
    _submit(runtime, "a", "qvina", seed=5)
    _submit(runtime, "a", "vina", exhaustiveness=6)
    _submit(runtime, "b", "vina", exhaustiveness=12)  # the later launch: Vina alone
    studio = studio_factory()
    assert [key for key, check in studio._program_checks.items() if check.isChecked()] == ["vina"]
    assert studio.exhaustiveness.value() == 12
    assert studio._program_config_widgets["qvina"]["seed"].value() == 5  # its own last run


def test_what_a_submission_carries_beyond_the_page_is_left_out(studio_factory):
    runtime = studio_factory.runtime
    _submit(runtime, "a", "vina", exhaustiveness=3)
    _submit(runtime, "r", "vina", kind="redocking", exhaustiveness=5, vina_command="/opt/vina")
    studio = studio_factory()
    assert [p["config"].get("vina_command") for p in studio._protocols] == [None]  # not this machine's binary
    _set_kind(studio, "docking")
    assert studio.exhaustiveness.value() == 3


def test_jobs_that_are_not_docking_or_unknown_programs_are_ignored():
    import json

    def row(name, **params):
        return {"name": name, "job_id": name, "spec": json.dumps({"origin": {"params": params}})}

    vina = VINA_PROGRAM.validate_config({"exhaustiveness": 7})
    rows = [row("amdock_docking_job", run_id="a", protocol_metadata={"program": "vina", "config": vina}),
            row("amdock_prepare_ligands", run_id="b", protocol_metadata={"program": "vina", "config": {}}),
            row("amdock_docking_job", run_id="c", protocol_metadata={"program": "not-a-program", "config": {}}),
            {"name": "amdock_docking_job", "job_id": "x", "spec": "not json"}]
    from amdockvs.docking.studio_setup import submissions

    setup = setup_from_submissions(submissions(rows))
    assert setup["programs"] == ["vina"] and setup["configs"]["vina"]["exhaustiveness"] == 7


def test_a_project_last_run_in_redocking_shows_its_variants(studio_factory):
    runtime = studio_factory.runtime
    _submit(runtime, "a", "vina", exhaustiveness=27)
    _submit(runtime, "r", "vina", kind="redocking", exhaustiveness=8)
    _submit(runtime, "r", "vina", kind="redocking", exhaustiveness=6)
    studio = studio_factory()
    assert studio._run_kind() == "redocking"
    assert [p["config"]["exhaustiveness"] for p in studio._protocols] == [8, 6]
    assert studio.exhaustiveness.value() == 8  # the selected variant, not Docking's
    studio._replace_selected_protocol()  # "Update selected" with nothing edited changes nothing
    assert studio._protocols[0]["config"]["exhaustiveness"] == 8
    _set_kind(studio, "docking")
    assert studio.exhaustiveness.value() == 27  # Docking's last run


def test_a_variant_picked_in_redocking_does_not_become_the_docking_run(studio_factory):
    studio = studio_factory()
    studio.exhaustiveness.setValue(27)
    studio.scoring_combo.setCurrentText("vinardo")
    _set_kind(studio, "redocking")
    studio._select_program("vina")  # a variant is of the program shown
    studio.exhaustiveness.setValue(6)  # not the default variant's 8: that one is in the set already
    studio.scoring_combo.setCurrentText("vina")
    studio._add_current_protocol()
    studio.protocol_table.setCurrentCell(len(studio._protocols) - 1, 0)  # picking it in the table
    _set_kind(studio, "docking")
    assert studio.exhaustiveness.value() == 27 and studio.scoring_combo.currentText() == "vinardo"
    assert studio._protocol_for_program("vina")["config"]["exhaustiveness"] == 27  # what a run uses


def test_a_setup_that_cannot_be_put_back_opens_on_the_defaults_and_says_so(studio_factory, monkeypatch):
    from amdockvs.ui.tools.docking import protocol_editor

    def broken(_project_root):
        raise ValueError("unreadable run")

    monkeypatch.setattr(protocol_editor, "setup_from_project", broken)
    studio = studio_factory()  # builds: the project opens
    assert studio.exhaustiveness.value() == VINA_PROGRAM.validate_config({})["exhaustiveness"]
    assert studio_factory.notices == ["unreadable run."]


def test_the_latest_click_ticks_even_when_its_jobs_ran_in_another_order():
    """Submission order is the journal's: a job of an older click that starts late is still older."""
    def sub(run_id, program, kind="docking", **config):
        return {"kind": kind, "run_id": run_id, "job": f"{run_id}-{program}", "program": program,
                "config": get_docking_program(program).validate_config(config), "label": None, "rescoring": []}

    setup = setup_from_submissions([sub("A", "vina", exhaustiveness=2), sub("A", "qvina", seed=412),
                                    sub("B", "gnina", exhaustiveness=3)])
    assert setup["programs"] == ["gnina"] and setup["configs"]["qvina"]["seed"] == 412


def test_a_sharded_click_of_several_programs_restores_all_its_ticks(studio_factory):
    """Over sharded libraries each program's job gets a run_id of its own (`program_run_id`)."""
    from uuid import uuid4

    from amdockvs.docking.planning import program_run_id

    runtime, click = studio_factory.runtime, uuid4().hex
    for index, (program, config) in enumerate((("vina", {"exhaustiveness": 5}), ("qvina", {"seed": 9}))):
        spec = get_docking_program(program)
        run_id = program_run_id(click, protocol_hash(program=program, config=spec.validate_config(config)), index, 2)
        assert run_id != click
        _submit(runtime, run_id, program, **config)
    studio = studio_factory()
    assert [key for key, check in studio._program_checks.items() if check.isChecked()] == ["vina", "qvina"]
    assert studio.exhaustiveness.value() == 5 and studio._program_config_widgets["qvina"]["seed"].value() == 9


def test_click_id_undoes_only_the_shape_program_run_id_makes():
    from uuid import uuid4

    from amdockvs.docking.planning import click_id, program_run_id

    click = uuid4().hex
    assert click_id(program_run_id(click, "abcdef0123456789", 0, 2)) == click
    assert click_id(program_run_id(click, "", 1, 2)) == click
    assert program_run_id(click, "abcdef0123456789", 0, 1) == click
    assert click_id("exp-2") == "exp-2" and click_id("") == "" and click_id(None) == ""  # API-chosen ids stay


def test_a_zinc_redocking_visit_leaves_the_docking_ticks_as_they_were(studio_factory):
    from amdockvs.docking.engines.autogrid import AUTOGRID_UNAVAILABLE

    if AUTOGRID_UNAVAILABLE:
        pytest.skip("Zinc needs autogrid4")
    studio = studio_factory()
    for key, check in studio._program_checks.items():
        check.setChecked(key == "unidock")
    _set_kind(studio, "redocking")
    studio.method_combo.setCurrentIndex(studio.method_combo.findData("zinc"))  # unticks Uni-Dock, ticks Vina
    _set_kind(studio, "docking")
    assert studio._method() == "standard"
    assert [key for key, check in studio._program_checks.items() if check.isChecked()] == ["unidock"]


def test_re_entering_redocking_shows_the_selected_variant(studio_factory):
    runtime = studio_factory.runtime
    _submit(runtime, "d", "qvina", seed=216)
    _submit(runtime, "r", "qvina", kind="redocking", seed=713)
    studio = studio_factory()  # reopens in Redocking, on the variant
    _set_kind(studio, "docking")
    assert studio._program_config_widgets["qvina"]["seed"].value() == 216
    _set_kind(studio, "redocking")
    assert studio._program_config_widgets["qvina"]["seed"].value() == 713  # the variant, not Docking's
    studio._replace_selected_protocol()  # "Update selected" with nothing edited changes nothing
    assert studio._protocols[0]["config"]["seed"] == 713
    _set_kind(studio, "docking")
    assert studio._program_config_widgets["qvina"]["seed"].value() == 216


def test_a_submission_this_build_refuses_is_skipped_and_the_rest_restored(studio_factory):
    from amdockvs.docking.jobs import RedockingJobParams, redocking_job

    runtime = studio_factory.runtime
    _submit(runtime, "d", "vina", exhaustiveness=12)
    root = runtime.active_context.path
    bad = {**VINA_PROGRAM.validate_config({}), "seed": "unreadable"}
    runtime.submit_job(redocking_job, params=RedockingJobParams(
        run_id="r", protocol_metadata={"program": "vina", "config": bad, "hash": "h"}, engine_config={},
        output_dir=str(root / "out")).model_dump(mode="python"))
    from ms_flow.engine.journal import recorded

    deadline = time.monotonic() + 10
    while len(recorded(root / "executor.db")) < 2:  # the journal writes every few tens of ms
        assert time.monotonic() < deadline, "the submission never reached the journal"
        time.sleep(0.02)
    assert [item["kind"] for item in submitted_docking(root)] == ["docking"]  # the bad one is not read
    studio = studio_factory()
    assert studio._run_kind() == "docking" and studio.exhaustiveness.value() == 12
    assert studio_factory.notices == []


def test_a_setup_that_fails_halfway_leaves_the_defaults_not_half_of_it(studio_factory, monkeypatch):
    from amdockvs.ui.tools.docking import protocol_editor

    runtime = studio_factory.runtime
    _submit(runtime, "d", "qvina", seed=216)
    _submit(runtime, "r", "vina", kind="redocking", exhaustiveness=6, seed=5)
    applied = protocol_editor.ProtocolEditorWidget._apply_docking_setup

    def halfway(self, setup):
        applied(self, setup)  # all of it went in, Redocking and the variant included
        raise ValueError("a value this build cannot put back")

    monkeypatch.setattr(protocol_editor.ProtocolEditorWidget, "_apply_docking_setup", halfway)
    studio = studio_factory()
    assert studio_factory.notices == ["a value this build cannot put back."]
    assert studio._run_kind() == "docking" and studio._method() == "standard"
    defaults = {spec.key: spec.validate_config({}) for spec in protocol_editor.list_docking_programs()}
    assert studio._program_config_widgets["qvina"]["seed"].value() == defaults["qvina"]["seed"]
    assert studio.exhaustiveness.value() == defaults["vina"]["exhaustiveness"]
    assert [p["program"] for p in studio._protocols] == ["vina"]
    assert studio._protocols[0]["config"]["seed"] == defaults["vina"]["seed"]
    assert [k for k, c in studio._program_checks.items() if c.isChecked()] == [protocol_editor.DEFAULT_PROGRAMS[0]]


def test_a_malformed_rescoring_entry_is_skipped_and_cannot_break_the_reset(studio_factory, monkeypatch):
    from amdockvs.docking.jobs import RedockingJobParams, redocking_job
    from amdockvs.docking.studio_setup import submissions
    from amdockvs.ui.tools.docking import protocol_editor
    import json

    vina = VINA_PROGRAM.validate_config({})
    rows = [{"name": "amdock_redocking_job", "job_id": "r", "spec": json.dumps({"origin": {"params": {
        "run_id": "r", "protocol_metadata": {"program": "vina", "config": vina, "rescoring": [None]}}}})}]
    assert submissions(rows) == []  # a rescoring step is a mapping

    def halfway(self, setup):  # a malformed variant got in, then something failed
        self._protocols = [{"program": "vina", "config": vina, "rescoring": [None], "label": "x", "id": "x"}]
        self.experiment_kind_combo.setCurrentIndex(self.experiment_kind_combo.findData("redocking"))
        raise ValueError("halfway")

    runtime = studio_factory.runtime
    _submit(runtime, "d", "vina", exhaustiveness=12)
    monkeypatch.setattr(protocol_editor.ProtocolEditorWidget, "_apply_docking_setup", halfway)
    studio = studio_factory()  # opens: the reset clears the variants before anything redraws them
    assert studio_factory.notices == ["halfway."]
    assert studio._run_kind() == "docking" and [p["rescoring"] for p in studio._protocols] == [[]]


def test_an_emptied_docking_selection_stays_empty_across_redocking(studio_factory):
    studio = studio_factory()
    for check in studio._program_checks.values():
        check.setChecked(False)
    _set_kind(studio, "redocking")
    _set_kind(studio, "docking")
    assert [key for key, check in studio._program_checks.items() if check.isChecked()] == []


def test_a_method_that_cannot_run_the_ticked_program_still_ticks_the_default(studio_factory):
    from amdockvs.docking.engines.autogrid import AUTOGRID_UNAVAILABLE

    if AUTOGRID_UNAVAILABLE:
        pytest.skip("Zinc needs autogrid4")
    studio = studio_factory()
    for key, check in studio._program_checks.items():
        check.setChecked(key == "qvina")
    studio.method_combo.setCurrentIndex(studio.method_combo.findData("zinc"))  # cannot run QuickVina
    assert [key for key, check in studio._program_checks.items() if check.isChecked()] == ["vina"]


def test_a_recorded_value_the_page_does_not_take_shows_the_programs_default(studio_factory):
    runtime = studio_factory.runtime
    assert VINA_PROGRAM.validate_config({"exhaustiveness": 999})["exhaustiveness"] == 999  # the model takes it
    _submit(runtime, "a", "vina", exhaustiveness=999, num_modes=4, scoring_function="vinardo")
    studio = studio_factory()
    defaults = VINA_PROGRAM.validate_config({})
    assert studio.exhaustiveness.maximum() < 999
    assert studio.exhaustiveness.value() == defaults["exhaustiveness"]  # not the box's 256
    assert studio.num_modes.value() == 4 and studio.scoring_combo.currentText() == "vinardo"
