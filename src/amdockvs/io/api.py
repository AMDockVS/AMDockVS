from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from amdockvs.io.jobs import (
    estimate_import_chunks,
    load_molecules_file_job,
    load_ligands_file_job,
    load_ligands_multithreaded_sdf_job,
    load_receptors_file_job,
)
from amdockvs.api_common import PathLike, group_files, normalize_files


# The executor loop keeps at most this many chunks in flight per job. It must be
# comfortably above the CPU count (≥2x) so compute can run ahead while results
# are staged/persisted; at 16 (the old default) chunks waiting to persist filled
# the window and starved the CPU pool. 32 covers a 14-16 CPU box at 2x.
DEFAULT_IMPORT_MAX_INFLIGHT = 32


def _total_import_chunks(files: list[Path], *, batch_size: int) -> int:
    """Declared chunk count for progress. Each chunk closes on whichever cap hits first — ~4MB of
    raw bytes OR ``batch_size`` records (loaders.stream_import_payload_batches) — so the record
    estimate alone is a floor: byte splits only add chunks. It must stay a floor, because the
    executor only completes a job once processed >= max(declared, emitted): declaring chunks the
    feed never emits leaves the job running forever. Undershooting just makes the % finish early.
    """
    total = 0
    for file_path in files:
        try:
            total += estimate_import_chunks(file_path, batch_size=batch_size)
        except Exception:  # noqa: BLE001 — best-effort hint; 1 chunk is always safe to declare
            total += 1
    return max(1, total)


@dataclass
class LoaderAPI:
    runtime: Any

    def _submit_job(
        self,
        job_def,
        *,
        params: dict[str, Any],
        executor_name: str,
        depends_on: list[str] | None = None,
        **kwargs,
    ) -> str:
        self.runtime._require_active_project()
        return job_def.submit_with_options(
            self.runtime,
            params=params,
            executor_name=executor_name,
            depends_on=depends_on,
            config=None,
            **kwargs,
        )

    def load_molecules(
        self,
        files: Iterable[PathLike],
        *,
        batch_size: int = 1000,
        executor_name: str = "compute",
        max_job_cpus: int | None = None,
        depends_on: list[str] | None = None,
        primary_role: str = "",
        primary_context: str = "general",
        molecule_kind: str = "unknown",
        prefilter: Mapping[str, Any] | None = None,
    ) -> list[str]:
        normalized_files = normalize_files(files, label="molecule files")
        if not normalized_files:
            return []
        effective_prefilter = None if not prefilter else dict(prefilter)
        # One job streams all files (like receptors): N files no longer means N jobs.
        params = {
            "file_paths": [str(file_path) for file_path in normalized_files],
            "batch_size": max(1, int(batch_size)),
            "storage_resource": "molecules",
            "primary_role": str(primary_role or ""),
            "primary_context": str(primary_context or "general"),
            "molecule_kind": str(molecule_kind or "unknown"),
        }
        if effective_prefilter is not None:
            params["prefilter"] = effective_prefilter
        return [
            self._submit_job(
                load_molecules_file_job,
                params=params,
                executor_name=executor_name,
                max_job_cpu=None if max_job_cpus is None else max(1, int(max_job_cpus)),
                depends_on=depends_on,
                total_chunks=_total_import_chunks(normalized_files, batch_size=max(1, int(batch_size))),
                max_inflight_tasks=DEFAULT_IMPORT_MAX_INFLIGHT,
            )
        ]

    def load_ligands(
        self,
        files: Iterable[PathLike],
        *,
        batch_size: int = 1000,
        executor_name: str = "compute",
        max_job_cpus: int | None = None,
        depends_on: list[str] | None = None,
        primary_context: str = "general",
        molecule_kind: str = "small_molecule",
        prefilter: Mapping[str, Any] | None = None,
    ) -> list[str]:
        normalized_files = normalize_files(files, label="ligand files")
        if not normalized_files:
            return []
        effective_prefilter = None if not prefilter else dict(prefilter)
        # One job streams all files (like receptors): N files no longer means N jobs.
        params = {
            "file_paths": [str(file_path) for file_path in normalized_files],
            "batch_size": max(1, int(batch_size)),
            "storage_resource": "molecules",
            "primary_role": "ligand",
            "primary_context": str(primary_context or "general"),
            "molecule_kind": str(molecule_kind or "small_molecule"),
        }
        if effective_prefilter is not None:
            params["prefilter"] = effective_prefilter
        return [
            self._submit_job(
                load_ligands_file_job,
                params=params,
                executor_name=executor_name,
                max_job_cpu=None if max_job_cpus is None else max(1, int(max_job_cpus)),
                depends_on=depends_on,
                total_chunks=_total_import_chunks(normalized_files, batch_size=max(1, int(batch_size))),
                max_inflight_tasks=DEFAULT_IMPORT_MAX_INFLIGHT,
            )
        ]

    def load_receptors(
        self,
        files: Iterable[PathLike],
        *,
        batch_size: int = 1000,
        executor_name: str = "compute",
        max_job_cpus: int | None = None,
        depends_on: list[str] | None = None,
        primary_context: str = "general",
        molecule_kind: str = "protein",
        prefilter: Mapping[str, Any] | None = None,
        binding_site_box_size: float | None = None,
        remove_non_structural_waters: bool = True,
        create_binding_sites: bool = True,
        remove_cofactors: bool = False,
        remove_altloc: bool = True,
        use_biological_assembly: bool = False,
        import_mode: str = "receptor",
        per_file: Mapping[str, Mapping[str, Any]] | None = None,
        scans: Mapping[str, Mapping[str, Any]] | None = None,
        build_specs: bool = True,
        extra_data_patch_by_file: Mapping[str, Mapping[str, Any]] | None = None,
        binding_site_specs_by_file: Mapping[str, list[Mapping[str, Any]]] | None = None,
    ) -> list[str]:
        normalized_files = normalize_files(files, label="receptor files")
        if not normalized_files:
            return []
        effective_prefilter = None if not prefilter else dict(prefilter)
        # This method owns the raw-intent -> import-maps assembly, so a headless caller gets the
        # same result as the UI (which just forwards its selections). Pre-assembled maps still win.
        if extra_data_patch_by_file is None and binding_site_specs_by_file is None:
            from amdockvs.configuration import app_config
            from amdockvs.io.receptor_preview import ReceptorImportOptions, build_receptor_import_maps

            box = (
                float(binding_site_box_size)
                if binding_site_box_size is not None
                else float(app_config(self.runtime).docking.binding_site_box_size)
            )
            base_options = ReceptorImportOptions(
                use_biological_assembly=bool(use_biological_assembly),
                remove_non_structural_waters=bool(remove_non_structural_waters),
                create_binding_sites_from_components=bool(create_binding_sites),
                remove_cofactors=bool(remove_cofactors),
                remove_altloc=bool(remove_altloc),
                import_mode=str(import_mode or "receptor"),
                binding_site_box_size=box,
            )
            extra_data_patch_by_file, binding_site_specs_by_file = build_receptor_import_maps(
                [str(path) for path in normalized_files],
                base_options=base_options,
                per_file={str(k): dict(v) for k, v in dict(per_file or {}).items()},
                scans={str(k): dict(v) for k, v in dict(scans or {}).items()},
                build_specs=bool(build_specs),
            )
        extra_data_patch_map = {
            str(Path(path).expanduser().resolve()): dict(value or {})
            for path, value in dict(extra_data_patch_by_file or {}).items()
        }
        binding_site_specs_map = {
            str(Path(path).expanduser().resolve()): [dict(item) for item in list(value or [])]
            for path, value in dict(binding_site_specs_by_file or {}).items()
        }
        params = {
            "file_paths": [str(file_path) for file_path in normalized_files],
            "batch_size": max(1, int(batch_size)),
            "storage_resource": "molecules",
            "primary_role": "receptor",
            "primary_context": str(primary_context or "general"),
            "molecule_kind": str(molecule_kind or "protein"),
            "extra_data_patch_by_file": extra_data_patch_map,
            "binding_site_specs_by_file": binding_site_specs_map,
        }
        if effective_prefilter is not None:
            params["prefilter"] = effective_prefilter
        return [
            self._submit_job(
                load_receptors_file_job,
                params=params,
                executor_name=executor_name,
                max_job_cpu=None if max_job_cpus is None else max(1, int(max_job_cpus)),
                depends_on=depends_on,
            )
        ]

    def load_ligands_multithreaded_sdf(
        self,
        files: Iterable[PathLike],
        *,
        executor_name: str = "compute",
        num_threads: int = 4,
        max_job_cpus: int | None = 4,
        files_per_job: int = 1,
        primary_context: str = "general",
        molecule_kind: str = "small_molecule",
        prefilter: Mapping[str, Any] | None = None,
    ) -> list[str]:
        normalized_files = normalize_files(files, label="files")
        invalid = [path for path in normalized_files if path.suffix.lower() != ".sdf"]
        if invalid:
            raise ValueError(f"load_ligands_multithreaded_sdf only supports .sdf files: {invalid}")

        effective_prefilter = None if not prefilter else dict(prefilter)
        job_ids: list[str] = []
        for group in group_files(normalized_files, files_per_job=files_per_job):
            params = {
                "file_paths": [str(path) for path in group],
                "num_threads": max(1, int(num_threads)),
                "storage_resource": "molecules",
                "primary_role": "ligand",
                "primary_context": str(primary_context or "general"),
                "molecule_kind": str(molecule_kind or "small_molecule"),
            }
            if effective_prefilter is not None:
                params["prefilter"] = effective_prefilter
            job_ids.append(
                self._submit_job(
                    load_ligands_multithreaded_sdf_job,
                    params=params,
                    executor_name=executor_name,
                    max_job_cpu=None if max_job_cpus is None else max(1, int(max_job_cpus)),
                    total_chunks=len(group),
                )
            )
        return job_ids


__all__ = ["LoaderAPI"]
