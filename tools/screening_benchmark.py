from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import resource
except Exception:  # pragma: no cover - platform dependent
    resource = None


@dataclass(frozen=True)
class ScreeningBatchPolicy:
    name: str
    import_batch_size: int
    descriptor_batch_size: int
    docking_batch_size: int
    description: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


SCREENING_BATCH_POLICIES: dict[str, ScreeningBatchPolicy] = {
    "latency": ScreeningBatchPolicy(
        name="latency",
        import_batch_size=250,
        descriptor_batch_size=250,
        docking_batch_size=64,
        description="Prioritises early feedback and small batches for interactive debugging.",
    ),
    "balanced": ScreeningBatchPolicy(
        name="balanced",
        import_batch_size=1000,
        descriptor_batch_size=1000,
        docking_batch_size=128,
        description="Default profile for day-to-day workstation use.",
    ),
    "throughput": ScreeningBatchPolicy(
        name="throughput",
        import_batch_size=2000,
        descriptor_batch_size=1500,
        docking_batch_size=256,
        description="Reduces scheduling overhead for large batches and massive screening.",
    ),
}


def resolve_screening_batch_policy(
    policy: str | ScreeningBatchPolicy | None,
    *,
    default: str = "balanced",
) -> ScreeningBatchPolicy:
    if isinstance(policy, ScreeningBatchPolicy):
        return policy
    key = str(policy or default).strip().lower()
    try:
        return SCREENING_BATCH_POLICIES[key]
    except KeyError as exc:
        known = ", ".join(sorted(SCREENING_BATCH_POLICIES))
        raise ValueError(f"Unknown screening batch policy '{key}'. Known: {known}.") from exc


def list_screening_batch_policies() -> list[ScreeningBatchPolicy]:
    return [SCREENING_BATCH_POLICIES[key] for key in ("latency", "balanced", "throughput")]


def _peak_rss_mb() -> float:
    if resource is None:
        return 0.0
    rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if rss <= 0.0:
        return 0.0
    if rss > 1024 * 1024 * 16:
        return rss / (1024.0 * 1024.0)
    return rss / 1024.0


def _make_smiles_file(path: Path, count: int) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for idx in range(count):
            smiles = "CCO" if idx % 2 == 0 else "CCN"
            handle.write(f"{smiles} BENCH_{idx:05d}\n")


def _make_receptor_pdb(path: Path, index: int) -> None:
    path.write_text(
        "\n".join(
            [
                f"ATOM      1  N   MET A   {index + 1:>1}      11.104  13.207  10.147  1.00 20.00           N",
                f"ATOM      2  CA  MET A   {index + 1:>1}      12.560  13.100  10.350  1.00 20.00           C",
                "END",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class BenchmarkConfig:
    ligand_count: int = 240
    receptor_count: int = 2
    ligand_file_count: int = 2
    policy: str | ScreeningBatchPolicy = "balanced"
    import_executor_name: str = "thread"
    descriptor_executor_name: str = "thread"
    docking_executor_name: str = "thread"
    timeout_s: float = 240.0
    poll_s: float = 0.05


def _wait_bounded(runtime, job_id: str, config: BenchmarkConfig):
    """Wait bounded by `config.timeout_s`, as the import and preparation phases already did.

    `runtime.wait_for_job` (singular) takes no timeout and does `while True`: if a job never
    reaches a terminal state, the benchmark does not fail — it hangs the whole suite forever.
    """
    return runtime.wait_for_jobs([job_id], timeout_s=config.timeout_s, poll_s=config.poll_s)[job_id]


def run_screening_benchmark(runtime, *, config: BenchmarkConfig, workdir: str | Path) -> dict[str, Any]:
    root = Path(workdir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    policy = resolve_screening_batch_policy(config.policy)

    project_dir = root / "benchmark_project"
    ligand_dir = root / "inputs" / "ligands"
    receptor_dir = root / "inputs" / "receptors"
    ligand_dir.mkdir(parents=True, exist_ok=True)
    receptor_dir.mkdir(parents=True, exist_ok=True)

    per_file = max(1, config.ligand_count // max(1, config.ligand_file_count))
    remaining = int(config.ligand_count)
    ligand_files: list[Path] = []
    for index in range(max(1, config.ligand_file_count)):
        count = per_file if index < max(1, config.ligand_file_count) - 1 else remaining
        remaining -= count
        path = ligand_dir / f"ligands_{index:02d}.smi"
        _make_smiles_file(path, count=max(1, count))
        ligand_files.append(path)

    receptor_files: list[Path] = []
    for index in range(max(1, int(config.receptor_count))):
        path = receptor_dir / f"receptor_{index:02d}.pdb"
        _make_receptor_pdb(path, index)
        receptor_files.append(path)

    runtime.create_project(name="benchmark", folder=project_dir, description="AMDock benchmark project")

    phase_started = time.perf_counter()
    import_job_ids = [
        *runtime.loader.load_ligands(
            ligand_files,
            batch_size=policy.import_batch_size,
            executor_name=config.import_executor_name,
        ),
        *runtime.loader.load_receptors(
            receptor_files,
            batch_size=policy.import_batch_size,
            executor_name=config.import_executor_name,
        ),
    ]
    import_statuses = runtime.wait_for_jobs(import_job_ids, timeout_s=config.timeout_s, poll_s=config.poll_s)
    import_s = time.perf_counter() - phase_started

    # Store a docking box on each imported receptor so docking.run's readiness check passes
    # (it requires a stored binding site/grid, not just an explicit box argument).
    for receptor in runtime.molecules.stream(runtime.molecules.select(role="receptor")):
        runtime.docking.set_grid(
            receptor_id=int(getattr(receptor, "id", 0) or 0),
            center=(12.0, 13.0, 10.0),
            size=(20.0, 20.0, 20.0),
        )

    phase_started = time.perf_counter()
    descriptor_job_id = runtime.qsar.compute_descriptors(
        batch_size=policy.descriptor_batch_size,
        executor_name=config.descriptor_executor_name,
        depends_on=import_job_ids,
    )
    descriptor_status = _wait_bounded(runtime, descriptor_job_id, config)
    descriptor_s = time.perf_counter() - phase_started

    # Vina preparation requires 3D conformers, so generate them before preparing ligands.
    conformer_job_id = runtime.chemistry.generate_3d_ligands(
        batch_size=policy.descriptor_batch_size,
        executor_name=config.descriptor_executor_name,
        depends_on=import_job_ids,
    )
    _wait_bounded(runtime, conformer_job_id, config)

    phase_started = time.perf_counter()
    preparation_job_ids = [
        runtime.docking.prepare_ligands(
            batch_size=policy.docking_batch_size,
            executor_name=config.docking_executor_name,
            depends_on=import_job_ids,
        ),
        runtime.docking.prepare_receptors(
            batch_size=min(32, policy.docking_batch_size),
            executor_name=config.docking_executor_name,
            depends_on=import_job_ids,
        ),
    ]
    preparation_statuses = runtime.wait_for_jobs(preparation_job_ids, timeout_s=config.timeout_s, poll_s=config.poll_s)
    preparation_s = time.perf_counter() - phase_started

    phase_started = time.perf_counter()
    docking_job_id = runtime.docking.run(
        batch_size=policy.docking_batch_size,
        box_center=(12.0, 13.0, 10.0),
        box_size=(20.0, 20.0, 20.0),
        executor_name=config.docking_executor_name,
        depends_on=[descriptor_job_id, *preparation_job_ids],
    )
    docking_status = _wait_bounded(runtime, docking_job_id, config)
    docking_s = time.perf_counter() - phase_started

    ligand_count = runtime.molecules.count(runtime.molecules.select(role="ligand"))
    receptor_count = runtime.molecules.count(runtime.molecules.select(role="receptor"))
    descriptors = runtime.qsar.list_descriptors()
    docking_stats = runtime.docking.result_stats()

    total_wall_s = import_s + descriptor_s + preparation_s + docking_s
    results_count = int(docking_stats.total_results)
    ligands_count = int(ligand_count)
    return {
        "config": asdict(config),
        "policy": policy.to_mapping(),
        "counts": {
            "ligands": ligands_count,
            "receptors": int(receptor_count),
            "descriptors": len(descriptors),
            "results": results_count,
        },
        "jobs": {
            "import_job_ids": import_job_ids,
            "descriptor_job_id": descriptor_job_id,
            "preparation_job_ids": preparation_job_ids,
            "docking_job_id": docking_job_id,
            "import_statuses": {job_id: row.status for job_id, row in import_statuses.items()},
            "descriptor_status": descriptor_status.status,
            "preparation_statuses": {job_id: row.status for job_id, row in preparation_statuses.items()},
            "docking_status": docking_status.status,
            "docking_throughput_eps": float(docking_status.throughput_eps or 0.0),
        },
        "durations_s": {
            "import": import_s,
            "descriptor": descriptor_s,
            "preparation": preparation_s,
            "docking": docking_s,
            "total": total_wall_s,
        },
        "throughput": {
            "ligands_per_s": (ligands_count / total_wall_s) if total_wall_s > 0.0 else 0.0,
            "results_per_s": (results_count / total_wall_s) if total_wall_s > 0.0 else 0.0,
        },
        "memory": {
            "peak_rss_mb": _peak_rss_mb(),
        },
    }
