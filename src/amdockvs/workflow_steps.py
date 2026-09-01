"""Declarative step specs: the catalogue of workflow operations.

A workflow step is a *spec* — a stable ``kind`` plus a plain ``config`` dict — not a captured
closure. The ``submit`` callable that actually runs the job is built lazily from (kind, config)
at launch time (``build_submit``), so a step can sit in a saved/predefined workflow long before
it is configured (e.g. an "Import ligands" step with no files chosen yet). That deferral is the
whole point of Fase 1: predefined steps that carry no configuration until the user gives them one.

Qt-free on purpose — the orchestrator (also Qt-free) resolves submits through here, and the UI
palette reads the same catalogue, so both stay in sync from one source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from amdockvs.orchestrator import STEP_NEEDS_CONFIG, STEP_PENDING, WorkflowStep

Config = Mapping[str, Any]
Submit = Callable[[Any], Any]  # runtime -> job_id(s)

DOCKING_VIEW_ID = "workspace.docking"  # kept in sync with docking.py; string only, stays Qt-free


@dataclass(frozen=True)
class StepSpec:
    kind: str
    label: str
    category: str
    build: Callable[[Config], Submit]  # config -> (runtime -> job_ids)
    needs_config: bool = False  # True = can't launch until the user configures it
    manual: bool = False  # guided route: interactive step the user runs in a panel, then marks done
    view_id: str = ""  # panel the guided navigator opens (imports are opened by kind, not view)


# No-argument jobs: their submit ignores config, so a bare step is immediately runnable.
# (label, category, submit). category drives the orchestrator's auto-dependencies.
_NO_ARG: dict[str, tuple[str, str, Submit]] = {
    # Ligand chemistry
    "generate_3d_ligands": ("Generate 3D (ligands)", "chemistry", lambda rt: rt.chemistry.generate_3d_ligands()),
    "standardize_ligands": ("Standardize ligands", "chemistry", lambda rt: rt.chemistry.standardize_ligands()),
    "protonate_ligands": ("Protonate ligands", "chemistry", lambda rt: rt.chemistry.protonate_ligands()),
    "minimize_ligands": ("Minimize ligands", "chemistry", lambda rt: rt.chemistry.minimize_ligands()),
    "generate_ligand_conformers": ("Generate conformers (ligands)", "chemistry", lambda rt: rt.chemistry.generate_ligand_conformers()),
    # Receptor chemistry
    "fix_receptors": ("Fix receptors", "chemistry", lambda rt: rt.chemistry.fix_receptors()),
    "protonate_receptors": ("Protonate receptors", "chemistry", lambda rt: rt.chemistry.protonate_receptors()),
    "minimize_receptors": ("Minimize receptors", "chemistry", lambda rt: rt.chemistry.minimize_receptors()),
    # Preparation. check_required=False: in a deferred workflow these wait for the 3D/prep steps,
    # so a submit-time readiness gate would always fail — the job filters inputs at run time.
    "prepare_ligands": ("Prepare ligands", "prepare", lambda rt: rt.docking.prepare_ligands(check_required=False)),
    "prepare_receptors": ("Prepare receptors", "prepare", lambda rt: rt.docking.prepare_receptors()),
    # Analysis
    "compute_descriptors": ("Compute descriptors", "descriptors", lambda rt: rt.qsar.compute_descriptors(only_missing=True)),
    "diversity_selection": ("Diversity selection (BitBIRCH)", "descriptors", lambda rt: rt.selection.cluster_job()),
}


def _build_import_ligands(config: Config) -> Submit:
    files = list(config.get("files") or [])
    prefilter = config.get("prefilter")

    def submit(rt):
        kwargs = {} if prefilter is None else {"prefilter": prefilter}
        return rt.loader.load_ligands(files, **kwargs)

    return submit


def _build_import_receptors(config: Config) -> Submit:
    files = list(config.get("files") or [])
    return lambda rt: rt.loader.load_receptors(files)


def _no_arg_spec(kind: str) -> StepSpec:
    label, category, submit = _NO_ARG[kind]
    return StepSpec(kind=kind, label=label, category=category, build=lambda _cfg, s=submit: s)


# Imports are manual in a guided route (the user picks files in the real dialog) and needs_config in a
# deferred DAG (that same dialog captures a deferred submit). Everything else is a plain job.
STEP_SPECS: dict[str, StepSpec] = {
    "import_ligands": StepSpec("import_ligands", "Import ligands", "import", _build_import_ligands, needs_config=True, manual=True),
    "import_receptors": StepSpec("import_receptors", "Import receptors", "import", _build_import_receptors, needs_config=True, manual=True),
    **{kind: _no_arg_spec(kind) for kind in _NO_ARG},
}


def is_configured(kind: str, config: Config | None) -> bool:
    """A step is ready to launch unless its spec needs config and none was supplied yet."""
    spec = STEP_SPECS.get(kind)
    if spec is None or not spec.needs_config:
        return True
    return bool(config)


def build_submit(kind: str, config: Config | None) -> Submit | None:
    """The runtime->job_ids callable for a spec step, or None if the kind is unknown."""
    spec = STEP_SPECS.get(kind)
    return None if spec is None else spec.build(config or {})


def make_step(kind: str, *, config: Config | None = None, name: str | None = None) -> WorkflowStep:
    """Build a WorkflowStep from a spec. Starts NEEDS_CONFIG if the spec requires config and none
    was given, else PENDING (ready). The submit is resolved lazily from (kind, config)."""
    spec = STEP_SPECS[kind]
    cfg = dict(config or {})
    status = STEP_PENDING if is_configured(kind, cfg) else STEP_NEEDS_CONFIG
    return WorkflowStep(name=name or spec.label, kind=kind, category=spec.category, config=cfg,
                        manual=spec.manual, view_id=spec.view_id, status=status)


# Predefined workflows (point 1): a template = name + blurb + ordered step kinds. Shipped as code,
# so no persistence layer is needed to offer them; loading one instantiates fresh steps via
# make_step (imports arrive NEEDS_CONFIG). Templates are immutable — the runner holds an editable
# copy, so editing a preset never mutates the template. Docking run / QSAR train are omitted: they
# carry scope/endpoint config that only their own panels build (queued from there).
@dataclass(frozen=True)
class WorkflowPreset:
    name: str
    description: str
    steps: tuple[str, ...]
    mode: str = "auto"  # "auto" = deferred DAG (unattended jobs); "guided" = linear route the user walks


# mode picks the execution style: "guided" routes (docking) open each interactive step's real panel
# with data present; "auto" routes (prep / qsar overnight) submit the whole DAG unattended.
PRESET_WORKFLOWS: dict[str, WorkflowPreset] = {
    p.name: p
    for p in (
        WorkflowPreset(
            "Vina docking",
            "A guided route: import receptors and ligands, clean and 3D-embed the ligands, prepare "
            "both, then configure and run docking in its panel. You walk the steps one at a time.",
            ("import_receptors", "import_ligands",
             "standardize_ligands", "protonate_ligands", "generate_3d_ligands",
             "fix_receptors", "protonate_receptors",
             "prepare_ligands", "prepare_receptors",
             "docking"),
            mode="guided",
        ),
        WorkflowPreset(
            "Ligand preparation only",
            "Unattended: import a ligand library, standardize and protonate it, and generate 3D conformers.",
            ("import_ligands", "standardize_ligands", "protonate_ligands", "generate_3d_ligands"),
        ),
        WorkflowPreset(
            "QSAR — descriptor features",
            "Unattended: import ligands, embed 3D, and compute molecular descriptors for QSAR modelling.",
            ("import_ligands", "generate_3d_ligands", "compute_descriptors"),
        ),
    )
}


def _docking_step(*, manual: bool, depends_on: list[str]) -> WorkflowStep:
    """A docking node. Guided routes make it a manual step opened in the Docking panel; deferred DAGs
    leave it needs_config to be filled from the Docking panel's 'Save to workflow'."""
    # A manual (guided) docking step is runnable — it opens its panel and waits — so it's PENDING.
    # A deferred (auto) docking node has no submit yet, so it stays NEEDS_CONFIG until the panel fills it.
    step = WorkflowStep(name="Docking", kind="docking", category="docking",
                        manual=manual, view_id=DOCKING_VIEW_ID if manual else "",
                        status=STEP_PENDING if manual else STEP_NEEDS_CONFIG)
    step.depends_on = list(depends_on)
    return step


def build_route(name: str) -> list[WorkflowStep]:
    """Instantiate a preset as a LINEAR guided route: every step depends on the previous one, so the
    runner walks them one at a time. Manual steps (imports, docking) pause for the user; job steps
    (chemistry, prepare) run and auto-advance. Used for mode=="guided" presets."""
    steps: list[WorkflowStep] = []
    prev: WorkflowStep | None = None
    for kind in PRESET_WORKFLOWS[name].steps:
        step = _docking_step(manual=True, depends_on=[prev.step_id] if prev else []) if kind == "docking" \
            else make_step(kind)
        if step.manual:  # interactive steps are runnable in a route (they open a panel), not blocked
            step.status = STEP_PENDING
        if prev is not None and not step.depends_on:
            step.depends_on = [prev.step_id]
        steps.append(step)
        prev = step
    return steps


def _branch_of(kind: str) -> str:
    """Which molecule stream a step belongs to, so a preset wires into clean parallel branches
    (ligand prep ∥ receptor prep) instead of one fanned-out blob."""
    return "receptor" if "receptor" in kind else "ligand"


def build_preset(name: str) -> list[WorkflowStep]:
    """Instantiate a predefined workflow as a DAG: imports are parallel roots, every later step
    depends on the previous step of its own branch (ligand/receptor), and a final docking step joins
    both branch tails. Gives explicit edges the editor draws cleanly — two parallel chains that
    converge on docking, rather than every-import→every-step links."""
    steps: list[WorkflowStep] = []
    last_in_branch: dict[str, WorkflowStep] = {}
    for kind in PRESET_WORKFLOWS[name].steps:
        if kind == "docking":  # not a spec kind: a join node, filled from the Docking panel later
            step = _docking_step(manual=False, depends_on=[s.step_id for s in last_in_branch.values()])
            steps.append(step)
            continue
        step = make_step(kind)
        branch = _branch_of(kind)
        prev = last_in_branch.get(branch)
        if prev is not None:  # imports (branch's first step) stay roots -> run in parallel
            step.depends_on = [prev.step_id]
        last_in_branch[branch] = step
        steps.append(step)
    return steps


__all__ = [
    "StepSpec",
    "STEP_SPECS",
    "WorkflowPreset",
    "PRESET_WORKFLOWS",
    "build_preset",
    "build_route",
    "build_submit",
    "is_configured",
    "make_step",
]
