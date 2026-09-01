from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional
from uuid import UUID

from ms_flow.runtime import AppRuntime

from amdockvs.constants import (
    AMDOCKVS_APP_ID,
    AMDOCKVS_APP_NAME,
    AMDOCKVS_DEFAULT_PROJECT_DIRS,
    AMDOCKVS_PROJECT_RESOURCES,
    AMDOCKVS_SCOPE_ID,
    RESOURCE_DOCKING_RESULTS,
    RESOURCE_EXPORTS,
    RESOURCE_JOBS,
    RESOURCE_MOLECULES,
    RESOURCE_QSAR_MODELS,
)
from amdockvs.configuration import (
    MAX_2D_PREVIEW_HEAVY_ATOMS,
    MAX_2D_PREVIEW_HEAVY_ATOMS_PATH,
    create_amdock_configuration,
)
import amdockvs.models  # noqa: F401  # Ensure SQLModel metadata is registered before project DB setup.
from amdockvs.molecule_paths import set_default_project_root
from amdockvs.summaries import JobStatus, ProjectSummary

if TYPE_CHECKING:
    from amdockvs.chemistry.api import ChemistryAPI
    from amdockvs.complexes.api import ComplexAPI
    from amdockvs.docking.api import DockingAPI
    from amdockvs.io.api import LoaderAPI
    from amdockvs.molecules.api import MoleculeAPI
    from amdockvs.qsar.api import QSARAPI
    from amdockvs.pockets.api import PocketPredictionAPI

KNOWN_JOB_STATUSES = (
    "pending",
    "running",
    "staging",
    "cancel_requested",
    "completed",
    "failed",
    "canceled",
)
TERMINAL_JOB_STATUSES = {"completed", "failed", "canceled"}
NON_TERMINAL_JOB_STATUSES = tuple(s for s in KNOWN_JOB_STATUSES if s not in TERMINAL_JOB_STATUSES)

# Pipeline dependency graph by job category. A newly submitted job auto-waits for every
# active (non-terminal) job of its prerequisite categories, so a whole pipeline can be queued
# up-front (import -> 3D -> prepare -> docking) and chains itself unattended. Coarse on purpose:
# over-waiting a little is safe; a job never starts before its inputs are ready.
# (substring, category) — task_type == job definition name, matched case-insensitively.
_JOB_CATEGORY_RULES = (
    ("chemistry", "chemistry"),   # standardize/protonate/3D/minimize/conformers (ligand+receptor)
    ("prepare_", "prepare"),
    ("descriptor", "descriptors"),
    ("docking", "docking"),       # also matches redocking
    ("load_", "import"),
    ("materialize", "import"),
)
_JOB_PREREQS = {
    "import": (),
    "chemistry": ("import",),
    "descriptors": ("import", "chemistry"),
    "prepare": ("import", "chemistry"),
    "docking": ("import", "chemistry", "prepare"),
}


def _job_category(name: str) -> Optional[str]:
    lowered = str(name or "").lower()
    for substring, category in _JOB_CATEGORY_RULES:
        if substring in lowered:
            return category
    return None


class AMDockVSRuntime(AppRuntime):
    """Public AMDockVS runtime on top of the stable MolSuite API."""

    def __init__(self):
        super().__init__(
            app_id=AMDOCKVS_APP_ID,
            logger_name=AMDOCKVS_APP_ID,
            project_resources=AMDOCKVS_PROJECT_RESOURCES,
        )
        amdock_configuration = create_amdock_configuration()
        self._migrate_legacy_app_settings(amdock_configuration)
        self._configuration_sources = [
            self.molsuite.settings_manager,
            amdock_configuration,
        ]
        self._loader_api: LoaderAPI | None = None
        self._molecule_api: MoleculeAPI | None = None
        self._complex_api: ComplexAPI | None = None
        self._chemistry_api: ChemistryAPI | None = None
        self._qsar_api: QSARAPI | None = None
        self._docking_api: DockingAPI | None = None
        self._pocket_prediction_api: PocketPredictionAPI | None = None

    def _migrate_legacy_app_settings(self, configuration) -> None:
        """Move first-generation flat settings into AMDock's TOML provider once."""
        manager = self.molsuite.settings_manager
        legacy_values = manager.settings.applications.get(AMDOCKVS_APP_ID, {})
        if MAX_2D_PREVIEW_HEAVY_ATOMS not in legacy_values:
            return
        try:
            current_source = configuration.get_source(MAX_2D_PREVIEW_HEAVY_ATOMS_PATH)
            can_migrate = current_source != "project" if manager.has_project else current_source == "default"
            if can_migrate:
                configuration.set_value(
                    MAX_2D_PREVIEW_HEAVY_ATOMS_PATH,
                    legacy_values[MAX_2D_PREVIEW_HEAVY_ATOMS],
                )
        except (TypeError, ValueError) as exc:
            self.logger.warning("Ignoring invalid legacy AMDock configuration: %s", exc)
        finally:
            manager.remove_app_settings(AMDOCKVS_APP_ID)

    @property
    def loader(self) -> LoaderAPI:
        if self._loader_api is None:
            from amdockvs.io.api import LoaderAPI

            self._loader_api = LoaderAPI(self)
        return self._loader_api

    @property
    def molecules(self) -> MoleculeAPI:
        if self._molecule_api is None:
            from amdockvs.molecules.api import MoleculeAPI

            self._molecule_api = MoleculeAPI(self)
        return self._molecule_api

    @property
    def chemistry(self) -> ChemistryAPI:
        if self._chemistry_api is None:
            from amdockvs.chemistry.api import ChemistryAPI

            self._chemistry_api = ChemistryAPI(self)
        return self._chemistry_api

    @property
    def complexes(self) -> ComplexAPI:
        if self._complex_api is None:
            from amdockvs.complexes.api import ComplexAPI

            self._complex_api = ComplexAPI(self)
        return self._complex_api

    @property
    def qsar(self) -> QSARAPI:
        if self._qsar_api is None:
            from amdockvs.qsar.api import QSARAPI

            self._qsar_api = QSARAPI(self)
        return self._qsar_api

    @property
    def docking(self) -> DockingAPI:
        if self._docking_api is None:
            from amdockvs.docking.api import DockingAPI

            self._docking_api = DockingAPI(self)
        return self._docking_api

    @property
    def pockets(self) -> PocketPredictionAPI:
        if self._pocket_prediction_api is None:
            from amdockvs.pockets.api import PocketPredictionAPI

            self._pocket_prediction_api = PocketPredictionAPI(self)
        return self._pocket_prediction_api

    @property
    def selection(self):
        if getattr(self, "_selection_api", None) is None:
            from amdockvs.selection.api import SelectionAPI

            self._selection_api = SelectionAPI(self)
        return self._selection_api

    def configuration_sources(self) -> tuple[object, ...]:
        return tuple(self._configuration_sources)

    @property
    def amdock_configuration(self):
        return next(item for item in self._configuration_sources if item.config_id == "amdockvs")

    def list_projects(self, page: int = 1, items_per_page: int = 20) -> list[ProjectSummary]:
        projects = self.project_catalog.list_projects(page=page, items_per_page=items_per_page)
        return [self._project_summary_from_project(project) for project in projects]

    def create_project(
        self,
        name: str,
        folder: Path | str,
        description: str = "",
        tags: list[str] | None = None,
        extra_dirs: list[str] | None = None,
    ) -> ProjectSummary:
        context = self.molsuite.create_project(
            name=name,
            folder=folder,
            description=description,
            tags=tags,
            scope=AMDOCKVS_SCOPE_ID,
            activate=True,
            extra_dirs=extra_dirs,
        )
        self.on_project_activated(context)
        return self._project_summary_from_context(context)

    def open_project(self, project_id: UUID | str, extra_dirs: list[str] | None = None) -> ProjectSummary:
        self._validate_project_app_id(project_id)
        context = self.molsuite.open_project(project_id, extra_dirs=extra_dirs)
        self.on_project_activated(context)
        return self._project_summary_from_context(context)

    def create_or_open_project(
        self,
        name: str,
        folder: Path | str,
        description: str = "",
        tags: list[str] | None = None,
        extra_dirs: list[str] | None = None,
    ) -> ProjectSummary:
        context = self.molsuite.create_or_open_project(
            name=name,
            folder=folder,
            description=description,
            tags=tags,
            scope=AMDOCKVS_SCOPE_ID,
            activate=True,
            extra_dirs=extra_dirs,
        )
        self.on_project_activated(context)
        return self._project_summary_from_context(context)

    def get_active_project(self) -> ProjectSummary:
        return self._project_summary_from_context(self._require_active_project())

    def get_project_paths(self) -> dict[str, Path]:
        context = self._require_active_project()
        return {
            "project_root": Path(context.path).expanduser().resolve(),
            "molecule_data_dir": self.get_project_resource_path(RESOURCE_MOLECULES),
            "docking_results_dir": self.get_project_resource_path(RESOURCE_DOCKING_RESULTS),
            "qsar_models_dir": self.get_project_resource_path(RESOURCE_QSAR_MODELS),
            "exports_dir": self.get_project_resource_path(RESOURCE_EXPORTS),
            "jobs_dir": self.get_project_resource_path(RESOURCE_JOBS),
        }

    def on_project_activated(self, context):
        set_default_project_root(Path(context.path).expanduser().resolve())
        for configuration in self._configuration_sources:
            if hasattr(configuration, "set_project_root"):
                configuration.set_project_root(context.path)
        self._migrate_legacy_app_settings(self.amdock_configuration)

    def close_project(self):
        super().close_project()
        for configuration in self._configuration_sources:
            if hasattr(configuration, "set_project_root"):
                configuration.set_project_root(None)

    def create_monitor_bridge(self, *, poll_ms: int | None = None, max_recent_jobs: int | None = None):
        from ms_components.ms_monitor import MolSuiteMonitorBridge

        # Defaults live in the component's MonitorConfig, nested in AMDock's config so
        # the user can persist overrides in AMDock's file; explicit args still win.
        monitor_cfg = self.amdock_configuration.get_value("monitor")
        return MolSuiteMonitorBridge(
            molsuite=self.molsuite,
            poll_ms=monitor_cfg.poll_ms if poll_ms is None else poll_ms,
            max_recent_jobs=monitor_cfg.max_recent_jobs if max_recent_jobs is None else max_recent_jobs,
        )

    @property
    def workflow(self):
        """The single active workflow (a WorkflowRunner). Panels add steps to this shared
        instance via runtime.workflow.add_step(...); it persists for the session."""
        if getattr(self, "_workflow", None) is None:
            from amdockvs.orchestrator import WorkflowRunner

            self._workflow = WorkflowRunner(self)
        return self._workflow

    def set_forced_dependencies(self, job_ids: Optional[list[str]]) -> None:
        """Workflow channel: ids the NEXT submit_job call(s) must also depend on. Lets the
        orchestrator inject same-category serialization that the category auto-deps don't cover,
        without every submit API needing a depends_on parameter. Cleared by passing None."""
        self._forced_dependencies = list(job_ids) if job_ids else None

    def submit_job(self, job, *, auto_depends: bool = True, **kwargs) -> str:
        """Submit a job, auto-waiting for active prerequisite jobs by default.

        With auto_depends=True (default) the job is held until every active job of its
        prerequisite categories finishes, so a deferred pipeline can be queued at once.
        Pass auto_depends=False to submit immediately regardless of running jobs.
        """
        forced = getattr(self, "_forced_dependencies", None)
        if forced:
            explicit = list(kwargs.get("depends_on") or [])
            for job_id in forced:
                if job_id not in explicit:
                    explicit.append(job_id)
            kwargs["depends_on"] = explicit
        if auto_depends and self.molsuite.active_context is not None:
            kwargs["depends_on"] = self.resolve_job_dependencies(job, kwargs.get("depends_on"))
        job_id = self.molsuite.submit_job(job, **kwargs)
        # Single funnel for "a job just started" UI feedback (the main window plugs a toast in
        # here). May fire from a worker thread, so the hook must be thread-safe (a Qt signal).
        hook = getattr(self, "on_job_submitted", None)
        if hook is not None:
            try:
                hook(str(getattr(job, "name", "") or "Job"), str(job_id))
            except Exception:  # noqa: BLE001 - feedback must never break a submit
                pass
        return job_id

    def resolve_job_dependencies(self, job, explicit: Optional[list[str]] = None) -> Optional[list[str]]:
        """Union of explicit depends_on and the ids of active jobs this one should wait for."""
        category = _job_category(getattr(job, "name", "") or "")
        prereqs = _JOB_PREREQS.get(category or "", ())
        deps: list[str] = list(explicit or [])
        if prereqs or category == "chemistry":
            for status in self.list_jobs(statuses=NON_TERMINAL_JOB_STATUSES):
                active_category = _job_category(status.task_type)
                serial_chemistry = category == "chemistry" and active_category == "chemistry"
                if (active_category in prereqs or serial_chemistry) and status.job_id not in deps:
                    deps.append(status.job_id)
        return deps or None

    def list_jobs(self, *, statuses: Iterable[str] = KNOWN_JOB_STATUSES) -> list[JobStatus]:
        context = self._require_active_project()
        normalized_statuses = tuple(str(item).strip() for item in statuses if str(item).strip())
        snapshots = self.molsuite.list_executor_jobs(statuses=normalized_statuses, project_id=context.id)
        return [self._job_status_from_snapshot(snapshot) for snapshot in snapshots]

    def get_job_status(self, job_id: str) -> Optional[JobStatus]:
        normalized_job_id = str(job_id)
        for row in self.list_jobs():
            if row.job_id == normalized_job_id:
                return row
        return None

    def wait_for_job(self, job_id: str, *, poll_s: float = 0.2) -> JobStatus:
        self._require_active_project()
        snapshot = self.molsuite.wait_for_job(str(job_id), poll_s=poll_s)
        return self._job_status_from_snapshot(snapshot)

    def wait_for_jobs(
        self,
        job_ids: Iterable[str],
        *,
        timeout_s: float = 3600.0,
        poll_s: float = 0.2,
    ) -> dict[str, JobStatus]:
        self._require_active_project()
        ordered_job_ids = [str(item) for item in job_ids]
        pending = set(ordered_job_ids)
        status_map: dict[str, JobStatus] = {}
        deadline = time.monotonic() + max(1.0, float(timeout_s))

        while pending and time.monotonic() < deadline:
            observed = {row.job_id: row for row in self.list_jobs()}
            for job_id in list(pending):
                row = observed.get(job_id)
                if row is not None and row.status in TERMINAL_JOB_STATUSES:
                    status_map[job_id] = row
                    pending.remove(job_id)
            if pending:
                time.sleep(max(0.05, float(poll_s)))

        if pending:
            raise TimeoutError(f"Jobs did not finish before timeout: {sorted(pending)}")
        return {job_id: self.wait_for_job(job_id, poll_s=0.0) for job_id in ordered_job_ids}

    def cancel_job(self, job_id: str) -> None:
        self._require_active_project()
        self.molsuite.cancel_job(str(job_id))

    def resubmit_job(
        self,
        job_id: str,
        *,
        executor_name: Optional[str] = None,
        cpu_required: Optional[int] = None,
        retry_limit: Optional[int] = None,
        queue_policy: Optional[str] = None,
        priority: Optional[int] = None,
        project_id: Optional[str] = None,
        store_results: Optional[bool] = None,
        output_spec: Any = None,
        output_flush_every: Optional[int] = None,
    ) -> str:
        self._require_active_project()
        return self.molsuite.resubmit_job(
            str(job_id),
            executor_name=executor_name,
            cpu_required=cpu_required,
            retry_limit=retry_limit,
            queue_policy=queue_policy,
            priority=priority,
            project_id=project_id,
            store_results=store_results,
            output_spec=output_spec,
            output_flush_every=output_flush_every,
        )

    def get_executor_status(self) -> dict[str, Any]:
        self._require_active_project()
        return self.molsuite.get_executor_status()

    def _project_summary_from_project(self, project) -> ProjectSummary:
        return ProjectSummary(
            id=str(project.id),
            name=project.name,
            path=Path(project.path).expanduser().resolve(),
            app_id=project.app_id or self.app_id,
            scope=project.scope or "full",
            description=project.description or "",
            tags=self.project_catalog.parse_tags(getattr(project, "tags", "")),
            favorite=bool(getattr(project, "favorite", False)),
            created_at=getattr(project, "created_at", None),
            updated_at=getattr(project, "updated_at", None),
        )

    def _project_summary_from_context(self, context: Any) -> ProjectSummary:
        try:
            project = self.project_catalog.get_project(context.id)
        except Exception:
            project = None
        if project is not None:
            return self._project_summary_from_project(project)
        return ProjectSummary(
            id=str(context.id),
            name=context.name,
            path=Path(context.path).expanduser().resolve(),
            app_id=context.app_id or self.app_id,
            scope=context.scope or "full",
            description=context.description or "",
            tags=[],
            favorite=False,
            created_at=getattr(context, "created_at", None),
            updated_at=getattr(context, "updated_at", None) or getattr(context, "update_at", None),
        )

    @staticmethod
    def _job_status_from_snapshot(snapshot: Any) -> JobStatus:
        return JobStatus.model_validate(snapshot.model_dump())


__all__ = [
    "AMDOCKVS_APP_ID",
    "AMDOCKVS_APP_NAME",
    "AMDOCKVS_DEFAULT_PROJECT_DIRS",
    "AMDOCKVS_PROJECT_RESOURCES",
    "AMDOCKVS_SCOPE_ID",
    "AMDockVSRuntime",
]
