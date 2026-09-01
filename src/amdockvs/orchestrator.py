"""Dynamic workflow orchestrator — a live DAG of job steps you can grow, prune and run.

A workflow is a set of steps, each either a not-yet-submitted operation (a submit callable) or
an already-running job adopted into the workflow. Steps carry intra-workflow dependencies; the
runner submits every step whose dependencies are satisfied — so independent steps run
concurrently on their own executors, while dependent ones wait (the "sentinel" gap). You can:

  * add a step from any panel (Add to workflow) into the single active workflow,
  * adopt a job that is already running,
  * pause at a boundary and edit/reorder steps that haven't been submitted,
  * remove a pending step freely; removing a RUNNING step cancels it and aborts the workflow.

Qt-free and runtime-agnostic: tick() from a GUI QTimer or run_blocking() in a notebook. Not an
executor job itself — it submits jobs and polls list_jobs. Concurrency is automatic: tick()
submits all ready steps, MolSuite runs them on whichever executor each step targets.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from uuid import uuid4

from amdockvs.runtime import _JOB_PREREQS, TERMINAL_JOB_STATUSES

STEP_NEEDS_CONFIG = "needs_config"  # added to a workflow but not yet configured -> can't launch
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
_STEP_DONE = {STEP_COMPLETED, STEP_SKIPPED}
_STEP_TERMINAL = {STEP_COMPLETED, STEP_SKIPPED, STEP_FAILED}
_STEP_EDITABLE = {STEP_PENDING, STEP_NEEDS_CONFIG}  # removable / reorderable / (re)configurable

WF_IDLE = "idle"
WF_RUNNING = "running"
WF_PAUSED = "paused"
WF_COMPLETED = "completed"
WF_FAILED = "failed"
WF_ABORTED = "aborted"
WF_TERMINAL = {WF_COMPLETED, WF_FAILED, WF_ABORTED}

_FAILED_STATUSES = {"failed", "canceled"}


@dataclass
class WorkflowStep:
    name: str
    submit: Callable[[Any], Any] | None = None  # runtime -> job_id(s); None = build from kind/config or adopted job
    depends_on: list[str] = field(default_factory=list)  # step ids within this workflow
    category: str | None = None  # import|chemistry|prepare|descriptors|docking (for auto-deps)
    kind: str = ""  # stable op identity for upsert + spec lookup ("generate_3d_ligands"…); "" = always-add
    config: dict = field(default_factory=dict)  # spec config; submit is built lazily from (kind, config)
    manual: bool = False  # guided route: an interactive step the user runs in a panel, then marks done
    view_id: str = ""  # panel the guided navigator opens for a manual step (imports open their dialog)
    status: str = STEP_PENDING
    job_ids: list[str] = field(default_factory=list)
    error: str = ""
    step_id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def editable(self) -> bool:
        return self.status in _STEP_EDITABLE


def _normalize_job_ids(result: Any) -> list[str]:
    if result is None:
        return []
    if isinstance(result, (list, tuple, set)):
        return [str(item) for item in result if item]
    return [str(result)]


class WorkflowRunner:
    def __init__(self, runtime, steps: Iterable[WorkflowStep] | None = None):
        self.runtime = runtime
        self.steps: list[WorkflowStep] = list(steps or [])
        self.status = WF_IDLE

    # --- editing --------------------------------------------------------------
    def configure_step(
        self,
        step_id: str,
        config: dict | None = None,
        *,
        name: str | None = None,
        submit: Callable[[Any], Any] | None = None,
        category: str | None = None,
    ) -> WorkflowStep:
        """Apply a step's configuration (from its dialog) and flip NEEDS_CONFIG -> PENDING once it's
        runnable. Pass `config` for spec steps (import files…) or an explicit `submit` for panel-built
        steps (docking). Only editable (pending / unconfigured) steps; running/done steps are frozen."""
        from amdockvs.workflow_steps import is_configured

        step = self._by_id(step_id)
        if step is None or not step.editable:
            raise ValueError("Only pending or unconfigured steps can be configured.")
        if config is not None:
            step.config = dict(config)
        if submit is not None:
            step.submit = submit
        if name:
            step.name = name
        if category is not None:
            step.category = category
        runnable = step.submit is not None or is_configured(step.kind, step.config)
        step.status = STEP_PENDING if runnable else STEP_NEEDS_CONFIG
        return step

    def unconfigured_steps(self) -> list[WorkflowStep]:
        """Steps still waiting on configuration — the workflow can't launch while any exist."""
        return [s for s in self.steps if s.status == STEP_NEEDS_CONFIG]

    def _require_configured(self) -> None:
        pending = self.unconfigured_steps()
        if pending:
            names = ", ".join(s.name for s in pending)
            raise ValueError(f"Configure these steps before launching: {names}")

    def _resolve_submit(self, step: WorkflowStep) -> Callable[[Any], Any] | None:
        """The runtime->job_ids callable for a step: an explicit closure if it has one (legacy
        panel 'Save to workflow'), else built lazily from the step's (kind, config) spec, else
        None (an adopted running job or a kind-less no-op)."""
        if step.submit is not None:
            return step.submit
        if step.kind:
            from amdockvs.workflow_steps import build_submit

            return build_submit(step.kind, step.config)
        return None

    def add_step(self, step: WorkflowStep) -> str:
        """Append a step. Ordering between categorized steps is resolved dynamically at run time
        (a 'prepare' waits for any 'import'/'chemistry' present, regardless of insertion order),
        so the workflow stays correct as steps are added/removed live. Use step.depends_on for
        explicit cross-step dependencies beyond the category graph."""
        self.steps.append(step)
        return step.step_id

    def upsert_step(self, step: WorkflowStep) -> tuple[WorkflowStep, bool]:
        """Save a configured step: if a PENDING step with the same non-empty kind already exists,
        update it in place (re-configure from its panel); otherwise append it. Returns
        (step, created). Running/finished steps are never mutated — re-saving queues a fresh one."""
        if step.kind:
            for existing in self.steps:
                if existing.kind == step.kind and existing.status == STEP_PENDING:
                    existing.name = step.name
                    existing.submit = step.submit
                    existing.category = step.category
                    existing.depends_on = list(step.depends_on)
                    return existing, False
        self.steps.append(step)
        return step, True

    def adopt_running_job(self, name: str, job_ids: Iterable[str], *, category: str | None = None) -> str:
        """Incorporate an already-running job as a workflow step (no submit; just waited on)."""
        step = WorkflowStep(name=name, submit=None, category=category, status=STEP_RUNNING, job_ids=list(job_ids))
        self.steps.append(step)
        if self.status == WF_IDLE:
            self.status = WF_RUNNING
        return step.step_id

    def clear(self) -> None:
        """Drop all steps to start a fresh workflow. Refuses while a step is running (abort first)."""
        if any(s.status == STEP_RUNNING for s in self.steps):
            raise ValueError("Abort the running workflow before clearing it.")
        self.steps.clear()
        self.status = WF_IDLE

    def remove_step(self, step_id: str) -> None:
        step = self._by_id(step_id)
        if step is None:
            return
        if step.status == STEP_RUNNING:
            # pulling a running step out cancels it and aborts the workflow (user's rule)
            self._cancel(step)
            self.steps.remove(step)
            self.status = WF_ABORTED
            return
        if not step.editable:
            raise ValueError("Only pending or running steps can be removed.")
        self.steps.remove(step)

    def move_step(self, step_id: str, to: int) -> None:
        step = self._by_id(step_id)
        if step is None or not step.editable:
            raise ValueError("Only pending steps can be reordered.")
        self.steps.remove(step)
        self.steps.insert(max(0, min(int(to), len(self.steps))), step)

    def skip_step(self, step_id: str) -> None:
        step = self._by_id(step_id)
        if step is None or not step.editable:
            raise ValueError("Only pending steps can be skipped.")
        step.status = STEP_SKIPPED

    # --- guided route (Type 2: step-by-step linear execution) -----------------
    def current_step(self) -> WorkflowStep | None:
        """The step a guided route is on: the running one, else the next pending in order."""
        running = next((s for s in self.steps if s.status == STEP_RUNNING), None)
        if running is not None:
            return running
        return next((s for s in self.steps if s.status == STEP_PENDING), None)

    def mark_step_done(self, step_id: str) -> str:
        """Complete a running MANUAL step (the user finished its panel work) and advance the route."""
        step = self._by_id(step_id)
        if step is None or step.status != STEP_RUNNING or not step.manual:
            raise ValueError("Only a running manual step can be marked done.")
        step.status = STEP_COMPLETED
        return self.tick()

    # --- control --------------------------------------------------------------
    def start(self) -> str:
        self._require_configured()
        if self.status not in WF_TERMINAL:
            self.status = WF_RUNNING
        return self.tick()

    def pause(self) -> None:
        if self.status == WF_RUNNING:
            self.status = WF_PAUSED

    def resume(self) -> str:
        if self.status == WF_PAUSED:
            self.status = WF_RUNNING
            return self.tick()
        return self.status

    def abort(self, *, cancel_running: bool = True) -> None:
        if cancel_running:
            for step in self.steps:
                if step.status == STEP_RUNNING:
                    self._cancel(step)
        self.status = WF_ABORTED

    # --- driving --------------------------------------------------------------
    def tick(self) -> str:
        """Reconcile once: finish terminal running steps, then submit every ready step
        (dependencies done) unless paused. Returns the workflow status."""
        if self.status not in {WF_RUNNING, WF_PAUSED}:
            return self.status

        for step in self.steps:
            if step.status != STEP_RUNNING:
                continue
            statuses = self._job_statuses(step.job_ids)
            if statuses and all(s in TERMINAL_JOB_STATUSES for s in statuses):
                if any(s in _FAILED_STATUSES for s in statuses):
                    step.status = STEP_FAILED
                    step.error = f"jobs ended: {statuses}"
                else:
                    step.status = STEP_COMPLETED

        # Per-step failure isolation: keep submitting ready independent steps even if another
        # branch failed. A step whose prereq failed never satisfies _deps_satisfied, so it stays
        # pending (blocked) rather than running. The workflow only goes terminal once nothing can
        # progress: every step terminal (-> FAILED if any failed, else COMPLETED), or all remaining
        # pending steps are permanently blocked by a failed prereq.
        if self.status == WF_RUNNING:
            for step in self.steps:
                if step.status == STEP_PENDING and self._deps_satisfied(step):
                    if step.manual:
                        # Guided route: an interactive step (import/docking). Don't submit a job —
                        # open its panel (the UI does that) and wait for the user's mark_step_done.
                        step.status = STEP_RUNNING
                    else:
                        self._submit(step)

        if self.steps and all(step.status in _STEP_TERMINAL for step in self.steps):
            self.status = WF_FAILED if any(s.status == STEP_FAILED for s in self.steps) else WF_COMPLETED
        elif not self._has_progressable_steps():
            # nothing running and no pending step can ever become ready (blocked by failed prereqs)
            self.status = WF_FAILED if any(s.status == STEP_FAILED for s in self.steps) else self.status
        return self.status

    def _has_progressable_steps(self) -> bool:
        for step in self.steps:
            if step.status == STEP_RUNNING:
                return True
            if step.status == STEP_PENDING and all(
                dep.status not in {STEP_FAILED} for dep in self._prereq_steps(step)
            ):
                return True  # pending and not blocked by a failed prereq -> can still run
        return False

    def run_blocking(self, *, poll_s: float = 0.5, timeout_s: float = 86_400.0) -> str:
        if self.status == WF_IDLE:
            self.start()
        deadline = time.monotonic() + float(timeout_s)
        while self.status not in WF_TERMINAL and time.monotonic() < deadline:
            self.tick()
            if self.status not in WF_TERMINAL:
                time.sleep(max(0.05, float(poll_s)))
        return self.status

    # --- Option B: hand the whole DAG to MolSuite at once ---------------------
    def materialize(self) -> str:
        """Launch every pending step to MolSuite now, in dependency order, each carrying native
        depends_on (via the runtime's auto-dependency layer). MolSuite then drives execution
        durably: independent steps run in parallel on their executors, dependents wait in the
        feeder — and it survives the app closing. Re-callable: only PENDING steps are launched,
        so adding steps after launch and pressing again queues them behind the running ones.
        Use refresh_status() (not tick()) afterwards — MolSuite owns submission from here.
        """
        self._require_configured()
        for step in self._topo_order():
            if step.status != STEP_PENDING:
                continue  # already running/adopted/skipped/unconfigured
            if self._resolve_submit(step) is None:
                continue
            step.status = STEP_RUNNING
            try:
                # Force native depends_on onto THIS step's prerequisite steps' jobs — including the
                # previous same-category step, which the category auto-deps alone don't serialize
                # (so 3D/standardize/protonate don't race on the same molecules).
                step.job_ids = _normalize_job_ids(self._submit_with_prereq_deps(step))
            except Exception as exc:
                # Isolate the failure: mark THIS step failed but keep launching the others, and do
                # NOT flip the workflow terminal here — refresh_status() decides that once every
                # step is terminal. (Aborting early froze the already-running steps' status.)
                step.status = STEP_FAILED
                step.error = str(exc)
                continue
            if not step.job_ids:
                step.status = STEP_COMPLETED
        if self.status not in WF_TERMINAL:
            self.status = WF_RUNNING
        return self.refresh_status()

    def refresh_status(self) -> str:
        """Poll-only reconciliation for display: advance running steps to terminal without
        submitting anything. Safe to call on a GUI timer after materialize()."""
        if self.status in WF_TERMINAL:
            return self.status
        for step in self.steps:
            if step.status == STEP_RUNNING and step.job_ids:
                statuses = self._job_statuses(step.job_ids)
                if statuses and all(s in TERMINAL_JOB_STATUSES for s in statuses):
                    if any(s in _FAILED_STATUSES for s in statuses):
                        step.status = STEP_FAILED
                        step.error = f"jobs ended: {statuses}"
                    else:
                        step.status = STEP_COMPLETED
        # Per-step failure isolation: a failed step does NOT abort the workflow — MolSuite
        # cancels only its actual dependents (they surface here as failed too), while independent
        # branches keep running. The workflow goes terminal only when every step is terminal:
        # FAILED if any failed, else COMPLETED.
        if self.steps and all(step.status in _STEP_TERMINAL for step in self.steps):
            self.status = WF_FAILED if any(s.status == STEP_FAILED for s in self.steps) else WF_COMPLETED
        return self.status

    # --- internals ------------------------------------------------------------
    def _by_id(self, step_id: str) -> WorkflowStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def _prereq_steps(self, step: WorkflowStep) -> list[WorkflowStep]:
        """Steps that must finish before ``step`` runs: explicit depends_on, every step of a
        prerequisite category (import->chemistry->prepare->docking), and the previous step of the
        SAME category (so same-category transforms — standardize/protonate/3D — serialize in
        insertion order instead of racing on the same molecules)."""
        prereqs: list[WorkflowStep] = []
        for dep_id in step.depends_on:
            dep = self._by_id(dep_id)
            if dep is not None and dep not in prereqs:
                prereqs.append(dep)
        prereq_categories = _JOB_PREREQS.get(step.category or "", ())
        # Same-category serialization guards transforms that mutate the SAME molecule set
        # (standardize/protonate/3D). Imports target different files and are independent, so they
        # must NOT chain — that's what lets "load receptors" and "load ligands" run in parallel.
        same_category_before = None
        if step.category and step.category != "import":
            for other in self.steps:
                if other is step:
                    break
                if other.category == step.category:
                    same_category_before = other  # keep the latest one before this step
        for other in self.steps:
            if other is step:
                continue
            if (other.category in prereq_categories or other is same_category_before) and other not in prereqs:
                prereqs.append(other)
        return prereqs

    def _deps_satisfied(self, step: WorkflowStep) -> bool:
        # acyclic by construction (category graph is a chain; same-category links point backwards),
        # so this can't deadlock.
        return all(dep.status in _STEP_DONE for dep in self._prereq_steps(step))

    # --- graph view helpers (public, for the Workflow panel) ------------------
    def dependency_edges(self) -> list[tuple[str, str]]:
        """(prereq_step_id, step_id) pairs — the DAG edges to draw."""
        edges: list[tuple[str, str]] = []
        for step in self.steps:
            for prereq in self._prereq_steps(step):
                edges.append((prereq.step_id, step.step_id))
        return edges

    def step_levels(self) -> dict[str, int]:
        """Longest-path level per step (0 = a root with no prerequisites) for a layered layout;
        parallel branches share a level."""
        level: dict[str, int] = {}
        for step in self._topo_order():
            prereqs = self._prereq_steps(step)
            level[step.step_id] = 1 + max((level.get(p.step_id, 0) for p in prereqs), default=-1)
        return level

    # The editor draws ONLY the edges the user actually built (explicit depends_on), not the coarse
    # category auto-dependencies — those still gate execution but would clutter the graph with links
    # like "every import -> every chemistry step". Presets wire their depends_on so they show clean.
    def explicit_edges(self) -> list[tuple[str, str]]:
        """(prereq_step_id, step_id) pairs from explicit depends_on only — what to draw."""
        ids = {s.step_id for s in self.steps}
        return [(dep, s.step_id) for s in self.steps for dep in s.depends_on if dep in ids]

    def explicit_levels(self) -> dict[str, int]:
        """Longest-path level per step over explicit depends_on only (roots = no depends_on)."""
        memo: dict[str, int] = {}

        def level(step: WorkflowStep) -> int:
            if step.step_id in memo:
                return memo[step.step_id]
            memo[step.step_id] = 0  # guard against accidental cycles
            deps = [self._by_id(d) for d in step.depends_on]
            memo[step.step_id] = 1 + max((level(d) for d in deps if d is not None), default=-1)
            return memo[step.step_id]

        for step in self.steps:
            level(step)
        return memo

    def _topo_order(self) -> list[WorkflowStep]:
        ordered: list[WorkflowStep] = []
        placed: set[str] = set()
        remaining = list(self.steps)
        while remaining:
            progressed = False
            for step in list(remaining):
                if all(p.step_id in placed for p in self._prereq_steps(step) if p in self.steps):
                    ordered.append(step)
                    placed.add(step.step_id)
                    remaining.remove(step)
                    progressed = True
            if not progressed:  # unexpected cycle — fall back to insertion order
                ordered.extend(remaining)
                break
        return ordered

    def _submit_with_prereq_deps(self, step: WorkflowStep):
        """Invoke the step's submit while forcing native depends_on onto its prerequisite steps'
        jobs. The submit callables don't take depends_on, so we hand the ids to the runtime via a
        short-lived channel that submit_job unions in. Covers the same-category serialization the
        category-based auto-deps miss."""
        submit = self._resolve_submit(step)
        if submit is None:
            return None
        prereq_job_ids = [jid for prereq in self._prereq_steps(step) for jid in prereq.job_ids]
        setter = getattr(self.runtime, "set_forced_dependencies", None)
        if callable(setter):
            setter(prereq_job_ids or None)
        try:
            return submit(self.runtime)
        finally:
            if callable(setter):
                setter(None)

    def _submit(self, step: WorkflowStep) -> None:
        if self._resolve_submit(step) is None:
            return  # adopted running job with no callable; nothing to submit
        step.status = STEP_RUNNING
        try:
            step.job_ids = _normalize_job_ids(self._submit_with_prereq_deps(step))
        except Exception as exc:
            step.status = STEP_FAILED
            step.error = str(exc)
            self.status = WF_FAILED
            return
        if not step.job_ids:
            step.status = STEP_COMPLETED  # no-op step

    def _cancel(self, step: WorkflowStep) -> None:
        for job_id in step.job_ids:
            try:
                self.runtime.cancel_job(job_id)
            except Exception:
                pass

    def _job_statuses(self, job_ids: list[str]) -> list[str]:
        if not job_ids:
            return []
        wanted = set(job_ids)
        observed = {row.job_id: row.status for row in self.runtime.list_jobs() if row.job_id in wanted}
        return [observed.get(job_id, "pending") for job_id in job_ids]


__all__ = [
    "WorkflowRunner",
    "WorkflowStep",
    "STEP_COMPLETED",
    "STEP_FAILED",
    "STEP_NEEDS_CONFIG",
    "STEP_PENDING",
    "STEP_RUNNING",
    "STEP_SKIPPED",
    "WF_ABORTED",
    "WF_COMPLETED",
    "WF_FAILED",
    "WF_IDLE",
    "WF_PAUSED",
    "WF_RUNNING",
    "WF_TERMINAL",
]
