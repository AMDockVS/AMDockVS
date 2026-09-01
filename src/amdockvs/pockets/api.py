"""Public AMDock backend API for receptor pocket prediction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from sqlmodel import select

from amdockvs.constants import DEFAULT_LOCAL_CPU_EXECUTOR, RESOURCE_POCKET_PREDICTIONS
from amdockvs.models import BindingSite, MoleculeRecord
from amdockvs.molecule_paths import preferred_molecule_path
from amdockvs.pockets.jobs import P2RankPredictionParams, p2rank_prediction_job
from amdockvs.pockets.p2rank import (
    P2RANK_VERSION,
    P2RankInstallation,
    ensure_p2rank,
    p2rank_status,
)


def defined_reference_ligands(extra_data: Any) -> list[str]:
    """Return receptor ligands explicitly retained during receptor import."""
    metadata = dict(extra_data or {}) if isinstance(extra_data, dict) else {}
    workflow = metadata.get("workflow")
    workflow = dict(workflow or {}) if isinstance(workflow, dict) else {}
    values = workflow.get("reference_ligands")
    if not isinstance(values, (list, tuple, set)):
        return []
    return [str(value) for value in values if str(value or "").strip()]


def _latest_run(sites: list[BindingSite]) -> list[BindingSite]:
    """Sites from the receptor's most recent run, or [] if none declares a run."""
    runs: dict[str, list[BindingSite]] = {}
    for site in sites:
        run_id = str(dict(site.extra_data or {}).get("run_id") or "").strip()
        if run_id:
            runs.setdefault(run_id, []).append(site)
    if not runs:
        return []
    return max(runs.values(), key=lambda group: max(int(site.id or 0) for site in group))


@dataclass
class P2RankPredictionPlan:
    receptor_ids: tuple[int, ...]
    recalculate_receptor_ids: tuple[int, ...]
    reused_receptor_ids: tuple[int, ...]


@dataclass
class PocketPredictionAPI:
    runtime: Any

    def tool_status(self) -> P2RankInstallation:
        return p2rank_status()

    def install_p2rank(self, *, archive_path: str | Path | None = None) -> P2RankInstallation:
        return ensure_p2rank(archive_path=archive_path)

    def list_receptors(self) -> list[dict[str, Any]]:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            prediction_profiles: dict[int, str] = {}
            prediction_rows = session.exec(
                select(BindingSite)
                .where(BindingSite.source == "p2rank")
                .order_by(BindingSite.molecule_id, BindingSite.id.desc())
            ).all()
            for site in prediction_rows:
                receptor_id = int(site.molecule_id or 0)
                if receptor_id not in prediction_profiles:
                    prediction_profiles[receptor_id] = str(
                        dict(site.extra_data or {}).get("profile") or "default"
                    )
            rows = session.exec(
                select(MoleculeRecord)
                .where(MoleculeRecord.is_receptor == True)  # noqa: E712
                .where(MoleculeRecord.excluded == False)  # noqa: E712
                .where(MoleculeRecord.usage_class == "general")
                .order_by(MoleculeRecord.name, MoleculeRecord.id)
            ).all()
            result = []
            for row in rows:
                path = preferred_molecule_path(row)
                defined_ligands = defined_reference_ligands(row.extra_data)
                result.append(
                    {
                        "id": int(row.id or 0),
                        "name": str(row.name or f"receptor_{int(row.id or 0)}"),
                        "path": "" if path is None else str(path),
                        "defined_ligands": defined_ligands,
                        "has_defined_ligands": bool(defined_ligands),
                        "p2rank_profile": prediction_profiles.get(
                            int(row.id or 0),
                            "default",
                        ),
                    }
                )
        return result

    def plan_prediction(
        self,
        *,
        receptor_ids: list[int],
        profile: str = "default",
        profiles: Mapping[int, str] | None = None,
        force: bool = False,
    ) -> P2RankPredictionPlan:
        self.runtime._require_active_project()
        normalized_ids = sorted({int(value) for value in receptor_ids if int(value) > 0})
        normalized_profile_map = {
            int(raw_id): str(raw_profile)
            for raw_id, raw_profile in dict(profiles or {}).items()
            if int(raw_id) > 0
        }
        requested_profiles = {
            receptor_id: str(
                normalized_profile_map.get(receptor_id, profile) or "default"
            ).strip().lower()
            for receptor_id in normalized_ids
        }
        invalid_profiles = sorted(
            {
                value
                for value in requested_profiles.values()
                if value not in {"default", "alphafold"}
            }
        )
        if invalid_profiles:
            raise ValueError(
                "P2Rank profiles must be 'default' or 'alphafold': "
                + ", ".join(invalid_profiles)
            )

        by_receptor: dict[int, list[BindingSite]] = {
            receptor_id: [] for receptor_id in normalized_ids
        }
        if normalized_ids:
            with self.runtime.molsuite.project_db.get_session() as session:
                rows = session.exec(
                    select(BindingSite)
                    .where(BindingSite.source == "p2rank")
                    .where(BindingSite.molecule_id.in_(normalized_ids))
                    .order_by(BindingSite.molecule_id, BindingSite.id)
                ).all()
                for row in rows:
                    by_receptor.setdefault(int(row.molecule_id), []).append(row)

        recalculate: list[int] = []
        reused: list[int] = []
        for receptor_id in normalized_ids:
            # Prediction is additive, so a receptor can accumulate runs. Consistency is judged
            # only on the latest one: the earlier ones are history the user keeps.
            sites = _latest_run(by_receptor.get(receptor_id, []))
            ranks = [str(site.source_ref or "") for site in sites]
            coherent = bool(sites) and len(ranks) == len(set(ranks))
            coherent = coherent and all(
                str(dict(site.extra_data or {}).get("profile") or "default")
                == requested_profiles[receptor_id]
                and str(dict(site.extra_data or {}).get("version") or "")
                == P2RANK_VERSION
                and Path(
                    str(dict(site.extra_data or {}).get("predictions_path") or "")
                ).is_file()
                and Path(
                    str(dict(site.extra_data or {}).get("points_path") or "")
                ).is_file()
                for site in sites
            )
            if force or not coherent:
                recalculate.append(receptor_id)
            else:
                reused.append(receptor_id)
        return P2RankPredictionPlan(
            receptor_ids=tuple(normalized_ids),
            recalculate_receptor_ids=tuple(recalculate),
            reused_receptor_ids=tuple(reused),
        )

    def list_predictions(
        self,
        *,
        receptor_id: int | None = None,
        run_id: str | None = None,
    ) -> list[BindingSite]:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            statement = select(BindingSite).where(BindingSite.source == "p2rank")
            if receptor_id is not None:
                statement = statement.where(BindingSite.molecule_id == int(receptor_id))
            rows = session.exec(
                statement.order_by(BindingSite.molecule_id, BindingSite.id)
            ).all()
            result = list(rows)
        if run_id:
            result = [
                row
                for row in result
                if str((row.extra_data or {}).get("run_id") or "") == str(run_id)
            ]
        return result

    def delete_binding_sites(self, binding_site_ids: Iterable[int]) -> int:
        """Deletes sites by id. The counterpart of prediction being additive."""
        self.runtime._require_active_project()
        from amdockvs.deletion import delete_binding_sites

        return delete_binding_sites(self.runtime.molsuite.project_db, binding_site_ids)

    def get_receptor(self, receptor_id: int) -> MoleculeRecord:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            row = session.get(MoleculeRecord, int(receptor_id))
            if row is None or not bool(row.is_receptor):
                raise ValueError(f"Receptor {receptor_id} does not exist.")
        return row

    def predict(
        self,
        *,
        receptor_ids: list[int],
        profile: str = "default",
        profiles: Mapping[int, str] | None = None,
        threads: int = 1,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        run_id: str | None = None,
        depends_on: list[str] | None = None,
        force: bool = False,
    ) -> str:
        self.runtime._require_active_project()
        installation = p2rank_status()
        if not installation.installed or (installation.java_version or 0) < 17:
            raise RuntimeError(
                f"{installation.message} Install P2Rank from Molecule Tools before running."
            )
        normalized_receptor_ids = sorted(
            {int(value) for value in receptor_ids if int(value) > 0}
        )
        if not normalized_receptor_ids:
            raise ValueError("P2Rank prediction requires at least one receptor id.")
        receptor_id_set = set(normalized_receptor_ids)
        normalized_profiles = {
            int(receptor_id): str(receptor_profile)
            for receptor_id, receptor_profile in dict(profiles or {}).items()
            if int(receptor_id) in receptor_id_set
        }
        plan = self.plan_prediction(
            receptor_ids=normalized_receptor_ids,
            profile=profile,
            profiles=normalized_profiles,
            force=force,
        )
        resolved_run_id = str(run_id or uuid4().hex)
        output_dir = self.runtime.get_project_resource_path(RESOURCE_POCKET_PREDICTIONS)
        params = P2RankPredictionParams(
            receptor_ids=normalized_receptor_ids,
            profile=profile,
            profiles=normalized_profiles,
            recalculate_receptor_ids=list(plan.recalculate_receptor_ids),
            threads=max(1, int(threads)),
            run_id=resolved_run_id,
            output_dir=str(output_dir),
            p2rank_command=str(installation.command),
            java_command=str(installation.java_command or ""),
            version=P2RANK_VERSION,
        )
        return self.runtime.submit_job(
            p2rank_prediction_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
            cpu_required=max(1, int(threads)),
            total_chunks=max(1, len(plan.recalculate_receptor_ids)),
        )


__all__ = [
    "P2RankPredictionPlan",
    "PocketPredictionAPI",
    "defined_reference_ligands",
]
