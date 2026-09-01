import pytest

from amdockvs.orchestrator import (
    STEP_RUNNING,
    WF_ABORTED,
    WF_COMPLETED,
    WF_FAILED,
    WF_PAUSED,
    WF_RUNNING,
    WorkflowRunner,
    WorkflowStep,
)


class _Row:
    def __init__(self, job_id, status):
        self.job_id = job_id
        self.status = status


class _FakeRuntime:
    def __init__(self):
        self.submitted: list[str] = []
        self.statuses: dict[str, str] = {}
        self.canceled: list[str] = []

    def submit(self, name):
        def _do(_runtime):
            jid = f"job_{name}"
            self.submitted.append(name)
            self.statuses[jid] = "running"
            return jid
        return _do

    def list_jobs(self, *, statuses=()):
        return [_Row(j, s) for j, s in self.statuses.items()]

    def cancel_job(self, job_id):
        self.canceled.append(job_id)


def _step(rt, name, *, category=None, depends_on=None):
    return WorkflowStep(name=name, submit=rt.submit(name), category=category, depends_on=depends_on or [])


def test_guided_route_manual_step_waits_then_advances():
    # A linear guided route: manual step (import) pauses for the user, then a job step runs.
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    imp = r.add_step(WorkflowStep(name="Import ligands", manual=True))            # interactive
    job = r.add_step(_step(rt, "standardize", category="chemistry", depends_on=[imp]))
    r.start()
    # the manual step is the current step and is RUNNING (its panel opened) — nothing submitted yet
    assert r.current_step().step_id == imp
    assert r.steps[0].status == STEP_RUNNING and rt.submitted == []
    # user finishes it -> route advances and the job step submits
    r.mark_step_done(imp)
    assert rt.submitted == ["standardize"]
    assert r.current_step().name == "standardize"
    # job completes -> route done
    rt.statuses["job_standardize"] = "completed"
    assert r.tick() == WF_COMPLETED


def test_mark_step_done_rejects_non_manual():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    sid = r.add_step(_step(rt, "a", category="chemistry"))
    r.start()  # non-manual -> submitted + RUNNING
    with pytest.raises(ValueError):
        r.mark_step_done(sid)  # can't hand-complete a job step


def test_independent_steps_run_concurrently():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(_step(rt, "a"))
    r.add_step(_step(rt, "b"))  # no deps -> both submit at once
    r.start()
    assert set(rt.submitted) == {"a", "b"}


def test_category_autodeps_chain_steps():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    imp = r.add_step(_step(rt, "import", category="import"))
    r.add_step(_step(rt, "prep", category="prepare"))  # auto-depends on import (+chemistry if present)
    r.start()
    assert rt.submitted == ["import"]            # prep waits for import
    rt.statuses["job_import"] = "completed"
    r.tick()
    assert rt.submitted == ["import", "prep"]
    rt.statuses["job_prep"] = "completed"
    assert r.tick() == WF_COMPLETED
    del imp  # silence unused


def test_category_gating_is_order_independent():
    # add prepare BEFORE import; dynamic gating must still make prepare wait for import.
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(_step(rt, "prep", category="prepare"))
    r.add_step(_step(rt, "import", category="import"))
    r.start()
    assert rt.submitted == ["import"]            # prep blocked by the later-added import
    rt.statuses["job_import"] = "completed"
    r.tick()
    assert set(rt.submitted) == {"import", "prep"}


def test_pause_holds_new_submissions():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(_step(rt, "import", category="import"))
    r.add_step(_step(rt, "prep", category="prepare"))
    r.start()
    r.pause()
    rt.statuses["job_import"] = "completed"
    assert r.tick() == WF_PAUSED
    assert rt.submitted == ["import"]
    r.resume()
    assert rt.submitted == ["import", "prep"]


def test_failure_blocks_dependents():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(_step(rt, "import", category="import"))
    r.add_step(_step(rt, "prep", category="prepare"))
    r.start()
    rt.statuses["job_import"] = "failed"
    assert r.tick() == WF_FAILED
    assert rt.submitted == ["import"]


def test_failure_isolates_independent_branch():
    # import->prep is one branch; an independent "desc" runs in parallel. import fails -> prep is
    # blocked, but desc must still run to completion. WF goes terminal (FAILED) only once nothing
    # can progress, not the instant import fails.
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(_step(rt, "import", category="import"))
    r.add_step(_step(rt, "prep", category="prepare"))   # depends on import
    r.add_step(_step(rt, "desc"))                        # no category -> independent
    r.start()
    assert set(rt.submitted) == {"import", "desc"}      # both independent roots launched
    rt.statuses["job_import"] = "failed"
    rt.statuses["job_desc"] = "running"
    assert r.tick() == WF_RUNNING                        # desc still running -> not terminal yet
    assert "prep" not in rt.submitted                    # prep blocked by failed import
    rt.statuses["job_desc"] = "completed"
    assert r.tick() == WF_FAILED                         # now everything terminal; import failed
    assert r.steps[2].status == "completed"              # independent branch survived


def test_adopt_running_job_and_chain():
    rt = _FakeRuntime()
    rt.statuses["EXT"] = "running"          # a job already running outside
    r = WorkflowRunner(rt)
    r.adopt_running_job("import (adopted)", ["EXT"], category="import")
    r.add_step(_step(rt, "prep", category="prepare"))  # depends on the adopted import
    r.start()
    assert rt.submitted == []               # prep waits for the adopted running job
    rt.statuses["EXT"] = "completed"
    r.tick()
    assert rt.submitted == ["prep"]


def test_remove_running_step_aborts_workflow():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    sid = r.add_step(_step(rt, "import", category="import"))
    r.start()
    assert r.steps[0].status == STEP_RUNNING
    r.remove_step(sid)
    assert r.status == WF_ABORTED
    assert rt.canceled == ["job_import"]


def test_materialize_launches_all_in_topo_order():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(_step(rt, "prep", category="prepare"))   # added before import on purpose
    r.add_step(_step(rt, "import", category="import"))
    r.add_step(_step(rt, "desc", category="descriptors"))
    r.materialize()
    # everything is submitted at once (MolSuite holds dependents via depends_on); import first
    assert rt.submitted[0] == "import"
    assert set(rt.submitted) == {"import", "prep", "desc"}
    assert all(s.status == STEP_RUNNING for s in r.steps)
    # finishing all jobs -> workflow completes via poll-only refresh
    for jid in list(rt.statuses):
        rt.statuses[jid] = "completed"
    assert r.refresh_status() == WF_COMPLETED


def test_imports_run_in_parallel():
    # two imports target different files -> independent roots, NOT serialized (unlike chemistry).
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(_step(rt, "imp_receptors", category="import"))
    r.add_step(_step(rt, "imp_ligands", category="import"))
    r.start()
    assert set(rt.submitted) == {"imp_receptors", "imp_ligands"}  # both launch at once


def test_same_category_steps_serialize():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(_step(rt, "standardize", category="chemistry"))
    r.add_step(_step(rt, "protonate", category="chemistry"))  # must wait for standardize
    r.start()
    assert rt.submitted == ["standardize"]
    rt.statuses["job_standardize"] = "completed"
    r.tick()
    assert rt.submitted == ["standardize", "protonate"]


def test_upsert_step_updates_pending_same_kind():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    s1, created1 = r.upsert_step(WorkflowStep(name="Dock v1", kind="docking", submit=rt.submit("d1")))
    assert created1 and len(r.steps) == 1
    # same kind, still pending -> update in place (re-configured), no new step
    s2, created2 = r.upsert_step(WorkflowStep(name="Dock v2", kind="docking", submit=rt.submit("d2")))
    assert not created2 and s2 is s1 and len(r.steps) == 1
    assert r.steps[0].name == "Dock v2"
    # different kind -> appended
    _s3, created3 = r.upsert_step(WorkflowStep(name="Prep", kind="prepare", submit=rt.submit("p")))
    assert created3 and len(r.steps) == 2
    # empty kind -> always adds (no dedupe)
    r.upsert_step(WorkflowStep(name="Import a", kind="", submit=rt.submit("ia")))
    r.upsert_step(WorkflowStep(name="Import b", kind="", submit=rt.submit("ib")))
    assert len(r.steps) == 4


def test_upsert_does_not_mutate_running_step():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.upsert_step(WorkflowStep(name="Dock", kind="docking", submit=rt.submit("d1")))
    r.start()  # docking is the only step -> submitted, now running
    assert r.steps[0].status == STEP_RUNNING
    # re-saving while it runs queues a fresh pending step instead of mutating the running one
    _s, created = r.upsert_step(WorkflowStep(name="Dock v2", kind="docking", submit=rt.submit("d2")))
    assert created and len(r.steps) == 2


def test_remove_pending_step_is_free():
    rt = _FakeRuntime()
    r = WorkflowRunner(rt)
    r.add_step(_step(rt, "import", category="import"))
    sid = r.add_step(_step(rt, "prep", category="prepare"))
    r.start()
    r.remove_step(sid)                      # prep still pending -> removable
    assert [s.name for s in r.steps] == ["import"]
