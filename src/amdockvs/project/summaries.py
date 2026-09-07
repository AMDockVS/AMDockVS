from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


class ProjectSummary(BaseModel):
    id: str
    name: str
    path: Path
    app_id: str
    scope: str = "full"
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    favorite: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LigandSummary(BaseModel):
    id: int
    name: str = ""
    n_atoms: int = 0
    input_format: str = ""
    source: Path
    stored_path: Optional[Path] = None
    status: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None


class ReceptorSummary(BaseModel):
    id: int
    source_file: Path
    source_index: int = 0
    name: str = ""
    n_atoms: int = 0
    input_format: str = ""
    stored_path: Optional[Path] = None
    active: bool = True
    selected: bool = True
    status: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DescriptorSummary(BaseModel):
    id: int
    molecule_id: int
    mw: Optional[float] = None
    logp: Optional[float] = None
    hbd: Optional[int] = None
    hba: Optional[int] = None
    tpsa: Optional[float] = None
    rotatable_bonds: Optional[int] = None
    fragment_count: Optional[int] = None
    status: str = ""
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None


class DockingResultSummary(BaseModel):
    id: int
    complex_id: Optional[int] = None
    run_kind: str = "screening"
    ligand_id: int
    receptor_id: int
    ligand_path: Optional[Path] = None
    receptor_path: Optional[Path] = None
    selected_pose_path: Optional[Path] = None
    selected_affinity: float = 0.0
    poses: list[dict[str, Any]] = Field(default_factory=list)
    grid: dict[str, Any] = Field(default_factory=dict)
    status: str = ""
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class JobStatus(BaseModel):
    job_id: str
    project_id: Optional[str] = None
    origin_id: str = ""
    task_type: str = ""
    status: str = ""
    executor_name: str = ""
    progress: float = 0.0
    progress_structural: float = 0.0
    progress_operational: float = 0.0
    progress_running_chunks_avg: float = 0.0
    priority: int = 0
    queue_policy: str = ""
    chunks_total: int = 0
    chunks_emitted: int = 0
    chunks_dispatched: int = 0
    chunks_done: int = 0
    chunks_failed: int = 0
    chunks_stage_failed: int = 0
    chunks_running: int = 0
    chunks_pending: int = 0
    chunks_staging: int = 0
    chunks_ready_not_dispatched: int = 0
    feed_exhausted: bool = True
    output_sink: Any = None
    job_queue_wait_s: Optional[float] = None
    chunks_started: int = 0
    chunk_queue_wait_avg_s: float = 0.0
    chunk_queue_wait_max_s: float = 0.0
    feed_cursor_position: int = 0
    feed_items_acked: int = 0
    loop_latency_ms: float = 0.0
    throughput_eps: float = 0.0
    running_cpu: int = 0
    max_job_cpu: Optional[int] = None
    scheduler_block_reason: str = ""
    last_dispatch_attempt_at: Optional[datetime] = None
    last_scheduler_reason_at: Optional[datetime] = None
    last_scheduler_reason: str = ""
    first_chunk_emitted_at: Optional[datetime] = None
    first_chunk_dispatched_at: Optional[datetime] = None
    last_progress_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class ValueCountSummary(BaseModel):
    value: str
    count: int


class SourceFileCountSummary(BaseModel):
    source_file: Path
    count: int
    min_source_index: int = 0
    max_source_index: int = 0


class NumericRangeSummary(BaseModel):
    min: int = 0
    avg: float = 0.0
    max: int = 0


class LigandTableStatsSummary(BaseModel):
    total_ligands: int = 0
    by_status: list[ValueCountSummary] = Field(default_factory=list)
    by_input_format: list[ValueCountSummary] = Field(default_factory=list)
    by_source_file: list[SourceFileCountSummary] = Field(default_factory=list)
    atoms: NumericRangeSummary = Field(default_factory=NumericRangeSummary)


class ReceptorTableStatsSummary(BaseModel):
    total_receptors: int = 0
    by_status: list[ValueCountSummary] = Field(default_factory=list)
    by_input_format: list[ValueCountSummary] = Field(default_factory=list)
    by_source_file: list[SourceFileCountSummary] = Field(default_factory=list)
    atoms: NumericRangeSummary = Field(default_factory=NumericRangeSummary)


class DockingResultsStatsSummary(BaseModel):
    total_results: int = 0
    completed_results: int = 0
    failed_results: int = 0
    pending_results: int = 0
    unique_ligands: int = 0
    unique_receptors: int = 0
    best_score: Optional[float] = None
    avg_score: Optional[float] = None
    worst_score: Optional[float] = None


class DockingHitSummary(BaseModel):
    result_id: int
    complex_id: Optional[int] = None
    run_kind: str = "screening"
    ligand_id: int
    ligand_name: str = ""
    receptor_id: int
    receptor_name: str = ""
    engine: str = ""
    protocol_label: str = ""
    protocol_hash: str = ""
    score: float = 0.0
    rmsd_vs_reference: Optional[float] = None
    ligand_efficiency: Optional[float] = None
    predicted_ki_m: Optional[float] = None
    predicted_pki: Optional[float] = None
    lipophilic_efficiency: Optional[float] = None
    fit_quality: Optional[float] = None
    bei: Optional[float] = None
    sei: Optional[float] = None
    status: str = ""
    error: str = ""
    output_path: Optional[Path] = None
    ligand_path: Optional[Path] = None
    receptor_path: Optional[Path] = None
    reference_ligand_path: Optional[Path] = None
    reference_receptor_path: Optional[Path] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ReceptorDockingSummary(BaseModel):
    receptor_id: int
    receptor_name: str = ""
    total_results: int = 0
    completed_results: int = 0
    failed_results: int = 0
    # Ligands docked against ANY receptor in the project: the screening's ligand universe, so a
    # receptor that lags behind the others is visible instead of just showing a smaller number.
    expected_ligands: int = 0
    best_score: Optional[float] = None
    avg_score: Optional[float] = None


class ScreeningSummary(BaseModel):
    ligand_import_jobs: list[str] = Field(default_factory=list)
    receptor_import_jobs: list[str] = Field(default_factory=list)
    descriptor_job: str
    ligand_preparation_job: str
    receptor_preparation_job: str
    docking_job: str
    import_statuses: list[JobStatus] = Field(default_factory=list)
    descriptor_status: JobStatus
    ligand_preparation_status: JobStatus
    receptor_preparation_status: JobStatus
    docking_status: JobStatus
