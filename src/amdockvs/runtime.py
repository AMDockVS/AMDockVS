from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional
from uuid import UUID, uuid4

from ms_flow.runtime import AppRuntime

from amdockvs.core.constants import (
    AMDOCKVS_APP_ID,
    AMDOCKVS_APP_NAME,
    AMDOCKVS_DEFAULT_PROJECT_DIRS,
    AMDOCKVS_PROJECT_RESOURCES,
    AMDOCKVS_SCOPE_ID,
    DEFAULT_LOCAL_CPU_EXECUTOR,
    RESOURCE_DOCKING_RESULTS,
    RESOURCE_EXPORTS,
    RESOURCE_JOBS,
    RESOURCE_MOLECULES,
    RESOURCE_QSAR_MODELS,
    TABLE_SCREENING_SHARD_RUNS,
)
from amdockvs.core.configuration import (
    MAX_2D_PREVIEW_HEAVY_ATOMS,
    MAX_2D_PREVIEW_HEAVY_ATOMS_PATH,
    create_amdock_configuration,
    create_contact_map_configuration,
)
import amdockvs.models  # noqa: F401  # Ensure SQLModel metadata is registered before project DB setup.
from amdockvs.core.jobs import STATUS as JOB_STATUS
from amdockvs.core.paths import set_default_project_root
from amdockvs.project.summaries import JobStatus, ProjectSummary
from amdockvs.molecules.storage import DbStore, general_ligand_count, library_kind

if TYPE_CHECKING:
    from amdockvs.chemistry.api import ChemistryAPI
    from amdockvs.complexes.api import ComplexAPI
    from amdockvs.docking.api import DockingAPI
    from amdockvs.io.api import LoaderAPI
    from amdockvs.molecules.api import MoleculeAPI
    from amdockvs.qsar.api import QSARAPI
    from amdockvs.binding_sites.api import BindingSiteAPI
    from amdockvs.integrations.api import ToolAPI
    from amdockvs.shards import ShardAPI

KNOWN_JOB_STATUSES = ("pending", "running", "completed", "failed", "canceled")
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
    ("_dock_", "docking"),         # amdock_dock_shards_job / future explicit dock_* jobs
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
        # A host that `resources.process` names with no environment is given the one of this app.
        self.workspace.host_env = self._host_env
        amdock_configuration = create_amdock_configuration()
        self._migrate_legacy_app_settings(amdock_configuration)
        self._configuration_sources = [
            self.workspace.settings_manager,
            amdock_configuration,
            create_contact_map_configuration(),
        ]
        self._loader_api: LoaderAPI | None = None
        self._molecule_api: MoleculeAPI | None = None
        self._complex_api: ComplexAPI | None = None
        self._chemistry_api: ChemistryAPI | None = None
        self._qsar_api: QSARAPI | None = None
        self._docking_api: DockingAPI | None = None
        self._binding_sites_api: BindingSiteAPI | None = None
        self._tools_api: ToolAPI | None = None
        self._shards_api: ShardAPI | None = None
        self._ligand_store = None

    def _host_env(self, address: str, busy=None, on_line=None) -> str:
        from amdockvs.integrations.hosts import prepare_host

        def say(line: str) -> None:
            self.logger.info(line)
            if on_line is not None:
                on_line(line)

        return prepare_host(address, say, self, busy)

    def _migrate_legacy_app_settings(self, configuration) -> None:
        """Move first-generation flat settings into AMDock's TOML provider once."""
        manager = self.workspace.settings_manager
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
    def binding_sites(self) -> BindingSiteAPI:
        if self._binding_sites_api is None:
            from amdockvs.binding_sites.api import BindingSiteAPI

            self._binding_sites_api = BindingSiteAPI(self)
        return self._binding_sites_api

    @property
    def tools(self) -> ToolAPI:
        """Optional external programs managed by AMDockVS."""
        if self._tools_api is None:
            from amdockvs.integrations.api import ToolAPI

            self._tools_api = ToolAPI(self)
        return self._tools_api

    @property
    def shards(self) -> ShardAPI:
        """Access registered shard containers without wrapping their records."""
        if self._shards_api is None:
            from amdockvs.shards import ShardAPI

            self._shards_api = ShardAPI(self)
        return self._shards_api

    @property
    def diversity(self):
        if getattr(self, "_diversity_api", None) is None:
            from amdockvs.diversity.api import DiversityAPI

            self._diversity_api = DiversityAPI(self)
        return self._diversity_api

    def configuration_sources(self) -> tuple[object, ...]:
        return tuple(self._configuration_sources)

    @property
    def amdock_configuration(self):
        return next(item for item in self._configuration_sources if item.config_id == "amdockvs")

    @property
    def contact_map_configuration(self):
        return next(item for item in self._configuration_sources if item.config_id == "ms_contactmap")

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
        context = self.workspace.create_project(
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
        context = self.workspace.open_project(project_id, extra_dirs=extra_dirs)
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
        context = self.workspace.create_or_open_project(
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

    @property
    def ligands_store(self):
        """The row tools' view of the library. The shard tools build their own store."""
        self._require_active_project()
        return self._ligand_store

    @property
    def mode(self) -> str:
        """Where the screening library lives, read off the data (`vs` = rows, `htpvs` = shards).

        Not stored anywhere: a project becomes a campaign by holding shards, not by having been
        declared one before it held anything.
        """
        self._require_active_project()
        return library_kind(self.workspace.project_db)

    def general_ligand_count(self) -> int:
        """How many screening ligands are rows here — what the importer asks before offering
        to shard, since the two libraries cannot coexist."""
        self._require_active_project()
        return general_ligand_count(self.workspace.project_db)

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
        # SQLModel creates new app tables but intentionally does not alter existing ones.
        # Keep this additive migration beside the app model that owns the column.
        with self.workspace.project_db.get_session() as session:
            connection = session.connection()
            columns = {
                str(row[1])
                for row in connection.exec_driver_sql(
                    f"PRAGMA table_info({TABLE_SCREENING_SHARD_RUNS});"
                ).fetchall()
            }
            if columns and "result_path" not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {TABLE_SCREENING_SHARD_RUNS} "
                    "ADD COLUMN result_path VARCHAR DEFAULT '';"
                )
                session.commit()
        # Built once per project: a feed asks the runtime where the ligands are, it does not ask
        # the mode — there is no mode to ask.
        self._ligand_store = DbStore(self.workspace.project_db)
        set_default_project_root(Path(context.path).expanduser().resolve())
        for configuration in self._configuration_sources:
            if hasattr(configuration, "set_project_root"):
                configuration.set_project_root(context.path)
        self._migrate_legacy_app_settings(self.amdock_configuration)

    def close_project(self):
        super().close_project()
        self._ligand_store = None
        for configuration in self._configuration_sources:
            if hasattr(configuration, "set_project_root"):
                configuration.set_project_root(None)

    def create_monitor_bridge(self, *, poll_ms: int | None = None, max_recent_jobs: int | None = None):
        from ms_components.ms_monitor import MolSuiteMonitorBridge

        # Defaults live in the component's MonitorConfig, nested in AMDock's config so
        # the user can persist overrides in AMDock's file; explicit args still win.
        monitor_cfg = self.amdock_configuration.get_value("monitor")
        return MolSuiteMonitorBridge(
            workspace=self.workspace,
            poll_ms=monitor_cfg.poll_ms if poll_ms is None else poll_ms,
            max_recent_jobs=monitor_cfg.max_recent_jobs if max_recent_jobs is None else max_recent_jobs,
        )

    @property
    def workflow(self):
        """The single active workflow (a WorkflowRunner). Panels add steps to this shared
        instance via runtime.workflow.add_step(...); it persists for the session."""
        if getattr(self, "_workflow", None) is None:
            from amdockvs.workflows.orchestrator import WorkflowRunner

            self._workflow = WorkflowRunner(self)
        return self._workflow

    def set_forced_dependencies(self, job_ids: Optional[list[str]]) -> None:
        """Workflow channel: ids the NEXT submit_job call(s) must also depend on. Lets the
        orchestrator inject same-category serialization that the category auto-deps don't cover,
        without every submit API needing a depends_on parameter. Cleared by passing None."""
        self._forced_dependencies = list(job_ids) if job_ids else None

    def submit_job(
        self,
        job,
        *,
        params: Any = None,
        config: Optional[dict] = None,
        executor_name: Optional[str] = None,
        cpu_required: Optional[int] = None,
        max_job_cpu: Optional[int] = None,
        max_inflight_tasks: Optional[int] = None,
        total_chunks: Optional[int] = None,
        depends_on: Optional[list[str]] = None,
        priority: int = 0,
        result_handler_kwargs: Optional[dict] = None,
        auto_depends: bool = True,
    ) -> str:
        """Submit a job, auto-waiting for active prerequisite jobs by default.

        With auto_depends=True (default) the job is held until every active job of its
        prerequisite categories finishes, so a deferred pipeline can be queued at once.
        Pass auto_depends=False to submit immediately regardless of running jobs.
        Returns the key of the job in the project's journal, which is its id everywhere here.
        """
        self._require_active_project()
        depends_on = list(depends_on or [])
        depends_on += [job_id for job_id in getattr(self, "_forced_dependencies", None) or () if job_id not in depends_on]
        if auto_depends:
            depends_on = self.resolve_job_dependencies(job, depends_on) or []
        workspace = self.workspace
        job_config = {
            "project_db": workspace.project_db,
            "project_store": workspace.advanced.project_store,
            "project_context": workspace.active_context,
            "project_resources": workspace.get_project_resource_map(),
            **(config or {}),
        }
        engine_job, on_error = job.engine_job(config=job_config, cpu=cpu_required, handler_kwargs=result_handler_kwargs)
        job_params = job.validate_params(params)
        total = total_chunks if total_chunks is not None else job.count_chunks(job_params, config=job_config)
        job_id = uuid4().hex
        # ponytail: `compute` is whoever plays `process` in the settings (pool, Ray or a queue):
        # a destination by name comes back when the engine has more than one of them.
        engine_ids = self._engine_ids()
        engine_ids[job_id] = workspace.submit(
            engine_job,
            job_params,
            key=job_id,
            executor="thread" if (executor_name or job.executor) == "thread" else "process",
            # A job that ended in an earlier session has nothing left to wait for.
            after=[engine_ids[dep] for dep in depends_on if dep in engine_ids],
            total=total,
            window=max_inflight_tasks,
            max_cpu=max_job_cpu,
            priority=priority,
            on_error=on_error,
        )
        # Single funnel for "a job just started" UI feedback (the main window plugs a toast in
        # here). May fire from a worker thread, so the hook must be thread-safe (a Qt signal).
        hook = getattr(self, "on_job_submitted", None)
        if hook is not None:
            try:
                hook(str(getattr(job, "name", "") or "Job"), job_id)
            except Exception:  # noqa: BLE001 - feedback must never break a submit
                pass
        return job_id

    def _engine_ids(self) -> dict[str, int]:
        """Job id -> what the engine calls it, for the jobs this session submitted."""
        if getattr(self, "_job_engine_ids", None) is None:
            self._job_engine_ids = {}
        return self._job_engine_ids

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
        self._require_active_project()
        wanted = {str(item).strip() for item in statuses if str(item).strip()}
        rows = [self._job_status_from_snapshot(snapshot) for snapshot in self.workspace.jobs()]
        return [row for row in rows if row.status in wanted]

    def get_job_status(self, job_id: str) -> Optional[JobStatus]:
        normalized_job_id = str(job_id)
        for row in self.list_jobs():
            if row.job_id == normalized_job_id:
                return row
        return None

    def wait_for_job(self, job_id: str, *, poll_s: float = 0.2) -> JobStatus:
        """Block until the job has ended. `poll_s` is not used: the engine tells."""
        self._require_active_project()
        engine_id = self._engine_ids().get(str(job_id))
        if engine_id is None:  # of an earlier session: it ended with it
            status = self.get_job_status(job_id)
            if status is None:
                raise KeyError(f"Unknown job: {job_id}")
            return status
        return self._job_status_from_snapshot(self.workspace.engine.wait(engine_id))

    def watch_job(self, job_id: str, *, poll_s: float = 0.2) -> JobStatus:
        """Display tqdm progress in a terminal or notebook and return the job status."""
        return self.watch_jobs([job_id], poll_s=poll_s)[str(job_id)]

    def watch_jobs(self, job_ids: Iterable[str], *, poll_s: float = 0.2) -> dict[str, JobStatus]:
        """Display concurrent tqdm progress for several jobs of this session."""
        self._require_active_project()
        from ms_flow.progress import watch_jobs

        job_ids = [str(item) for item in job_ids]
        engine_ids = self._engine_ids()
        snapshots = watch_jobs(self.workspace.engine, [engine_ids[job_id] for job_id in job_ids], poll_s=poll_s)
        return {job_id: self._job_status_from_snapshot(snapshots[engine_ids[job_id]]) for job_id in job_ids}

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
        engine_id = self._engine_ids().get(str(job_id))
        if engine_id is not None:  # only a job of this session can still be running
            self.workspace.engine.cancel(engine_id)

    def run_destinations(self) -> list[tuple[str, str]]:
        """Where a job may be sent, as (executor_name, label).

        One for now: who runs the jobs of this machine (the local pool, Ray or a cluster's
        queue) is the `resources.process` setting, not something chosen for each job.
        """
        self._require_active_project()
        return [(DEFAULT_LOCAL_CPU_EXECUTOR, "Local")]

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
        """A job of the engine as AMDockVS reads it: its key as id, a percentage as progress."""
        total = snapshot.chunks_total or 0
        ended = snapshot.chunks_done + snapshot.chunks_failed
        status = JOB_STATUS.get(snapshot.status, snapshot.status)
        progress = 100.0 if status == "completed" else 100.0 * (snapshot.progress or 0.0)
        started_at = datetime.fromtimestamp(snapshot.started_at) if snapshot.started_at else None
        return JobStatus(
            job_id=str(snapshot.key or snapshot.id),
            task_type=snapshot.name,
            status=status,
            progress=progress,
            progress_structural=progress,
            progress_operational=progress,
            chunks_total=total,
            chunks_emitted=ended + snapshot.chunks_inflight,
            chunks_dispatched=ended + snapshot.chunks_inflight,
            chunks_started=ended + snapshot.chunks_inflight,
            chunks_done=snapshot.chunks_done,
            chunks_failed=snapshot.chunks_failed,
            chunks_running=snapshot.chunks_inflight,
            chunks_pending=max(0, total - ended - snapshot.chunks_inflight),
            feed_exhausted=snapshot.chunks_total is not None,
            error=snapshot.error or "",
            created_at=started_at,
            started_at=started_at,
            finished_at=datetime.fromtimestamp(snapshot.finished_at) if snapshot.finished_at else None,
        )


__all__ = [
    "AMDOCKVS_APP_ID",
    "AMDOCKVS_APP_NAME",
    "AMDOCKVS_DEFAULT_PROJECT_DIRS",
    "AMDOCKVS_PROJECT_RESOURCES",
    "AMDOCKVS_SCOPE_ID",
    "AMDockVSRuntime",
]
