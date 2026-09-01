import pytest

from amdockvs.orchestrator import (
    STEP_NEEDS_CONFIG,
    STEP_PENDING,
    WF_COMPLETED,
    WorkflowRunner,
)
from amdockvs.workflow_steps import STEP_SPECS, build_submit, is_configured, make_step


class _Row:
    def __init__(self, job_id, status):
        self.job_id, self.status = job_id, status


class _FakeLoader:
    def __init__(self):
        self.ligand_calls = []

    def load_ligands(self, files, **kwargs):
        self.ligand_calls.append((tuple(files), kwargs))
        return "job_import"


class _FakeRuntime:
    def __init__(self):
        self.loader = _FakeLoader()
        self.statuses = {}

    def list_jobs(self, *, statuses=()):
        return [_Row(j, s) for j, s in self.statuses.items()]

    def cancel_job(self, job_id):
        pass


def test_no_arg_spec_is_configured_and_pending():
    step = make_step("generate_3d_ligands")
    assert step.status == STEP_PENDING
    assert step.category == "chemistry" and step.kind == "generate_3d_ligands"


def test_import_needs_config_until_files_given():
    bare = make_step("import_ligands")
    assert bare.status == STEP_NEEDS_CONFIG  # a preset import with no files can't launch yet
    configured = make_step("import_ligands", config={"files": ["a.sdf"]})
    assert configured.status == STEP_PENDING


def test_is_configured_matrix():
    assert is_configured("generate_3d_ligands", None) is True   # no config needed
    assert is_configured("import_ligands", None) is False
    assert is_configured("import_ligands", {"files": ["x"]}) is True


def test_build_submit_lazily_calls_runtime():
    rt = _FakeRuntime()
    submit = build_submit("import_ligands", {"files": ["a.sdf", "b.sdf"], "prefilter": None})
    submit(rt)
    # prefilter=None must NOT be forwarded (loader signature stays clean)
    assert rt.loader.ligand_calls == [(("a.sdf", "b.sdf"), {})]


def test_launch_gate_blocks_unconfigured_steps():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(make_step("import_ligands"))          # needs_config
    assert len(r.unconfigured_steps()) == 1
    with pytest.raises(ValueError):
        r.materialize()                               # gate refuses to launch


def test_configure_step_with_submit_makes_it_runnable():
    # docking-style step: no spec config, configured by handing it an explicit submit (from the
    # panel's workflow_step_payload via the config dialog).
    from amdockvs.orchestrator import STEP_NEEDS_CONFIG, WorkflowStep

    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    sid = r.add_step(WorkflowStep(name="Docking (needs config)", kind="docking",
                                  category="docking", status=STEP_NEEDS_CONFIG))
    assert r.unconfigured_steps()
    r.configure_step(sid, submit=lambda _rt: "job_dock", name="Docking (vina)")
    assert not r.unconfigured_steps()
    assert r.steps[0].status == STEP_PENDING and r.steps[0].name == "Docking (vina)"


def test_configure_step_flips_to_pending_then_runs():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    sid = r.add_step(make_step("import_ligands"))
    r.configure_step(sid, {"files": ["a.sdf"]})
    assert r.steps[0].status == STEP_PENDING and not r.unconfigured_steps()
    r.materialize()                                   # now allowed
    assert rt.loader.ligand_calls[0][0] == ("a.sdf",)
    rt.statuses["job_import"] = "completed"
    assert r.refresh_status() == WF_COMPLETED


def test_every_spec_builds_a_callable():
    for kind in STEP_SPECS:
        assert callable(build_submit(kind, {}))


def test_presets_instantiate_with_import_needing_config():
    from amdockvs.workflow_steps import PRESET_WORKFLOWS, build_preset

    for name in PRESET_WORKFLOWS:
        steps = build_preset(name)
        assert steps, f"{name} is empty"
        for s in steps:  # imports + docking come in unconfigured, everything else ready
            expected = STEP_NEEDS_CONFIG if (s.kind.startswith("import_") or s.kind == "docking") else STEP_PENDING
            assert s.status == expected


def test_vina_preset_docking_joins_both_branches():
    from amdockvs.orchestrator import WorkflowRunner
    from amdockvs.workflow_steps import build_preset

    r = WorkflowRunner(_FakeRuntime())
    for s in build_preset("Vina docking"):
        r.add_step(s)
    levels = r.explicit_levels()
    roots = [s.kind for s in r.steps if levels[s.step_id] == 0]
    assert set(roots) == {"import_ligands", "import_receptors"}  # parallel roots
    dock = next(s for s in r.steps if s.kind == "docking")
    parents = {s.kind for s in r.steps if s.step_id in dock.depends_on}
    assert parents == {"prepare_ligands", "prepare_receptors"}  # joins both branch tails


def test_build_route_is_linear_with_manual_interactive_steps():
    from amdockvs.orchestrator import STEP_PENDING
    from amdockvs.workflow_steps import build_route

    route = build_route("Vina docking")
    # strictly linear: each step depends on exactly the previous one (roots only for the first)
    for prev, step in zip(route, route[1:]):
        assert step.depends_on == [prev.step_id]
    # imports + docking are interactive (manual) and runnable (PENDING, not needs_config)
    manual = {s.kind for s in route if s.manual}
    assert manual == {"import_ligands", "import_receptors", "docking"}
    assert all(s.status == STEP_PENDING for s in route if s.manual)
    # docking is the last step and opens the Docking panel
    assert route[-1].kind == "docking" and route[-1].view_id


def test_vina_preset_mode_is_guided():
    from amdockvs.workflow_steps import PRESET_WORKFLOWS

    assert PRESET_WORKFLOWS["Vina docking"].mode == "guided"
    assert PRESET_WORKFLOWS["Ligand preparation only"].mode == "auto"


def test_clear_refuses_while_running_then_resets():
    from amdockvs.orchestrator import STEP_RUNNING

    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(make_step("generate_3d_ligands"))
    r.steps[0].status = STEP_RUNNING
    with pytest.raises(ValueError):
        r.clear()
    r.steps[0].status = "completed"
    r.clear()
    assert r.steps == []
