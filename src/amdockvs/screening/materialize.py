"""Bringing hits back into the project: threshold AND cap, whichever binds first.

A campaign scores millions of molecules and the project database is supposed to end up holding
the handful that matter. The threshold is the science ("keep everything under -8.5"); the cap is
what makes the campaign *end* — a generous receptor or a biased library hits the threshold on
half the library, and without a ceiling the run quietly writes millions of rows at hour 30 with
nobody watching.

The gate is pure and incremental: hits are promoted as they come back, not accumulated and
sorted at the end. Nothing here holds a result set.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlmodel import select

from amdockvs.models import DockingResult, MoleculeModel, MoleculeRecord
from amdockvs.core.vocab import FileFormat, ModelSource, MoleculeType

PAYLOAD_LIGHT = "light"
PAYLOAD_FULL = "full"

# What a light payload keeps: the pose geometry and the score (unrecomputable) plus the SMILES
# (recomputes everything else). Descriptors, conformers and prepared files are ~60 bytes of
# identity away from being regenerated locally, so they only travel in `full`.
LIGHT_FIELDS = (
    "name", "source", "source_index", "smiles", "engine", "score", "score_type",
    "pose_path", "pose_rank", "receptor_molecule_id", "metrics",
)

# Descriptor columns a `full` payload may carry straight into the molecule row.
DESCRIPTOR_FIELDS = (
    "mw", "exact_mw", "logp", "hbd", "hba", "tpsa", "rotatable_bonds", "ring_count",
    "aromatic_ring_count", "hetero_atom_count", "heavy_atom_count", "formal_charge",
    "fraction_csp3", "n_atoms",
)


@dataclass
class HitGate:
    """Threshold AND cap, whichever binds first. Stateful on purpose: it counts.

    ponytail: first N past the threshold, not a ranked top-N — ranking needs the whole run
    ordered, which is exactly the "accumulate everything" this exists to avoid. Sort the
    materialized rows afterwards; they are at most `cap`.
    """

    cap: int
    threshold: float | None = None
    metric: str = "score"
    op: str = "<="  # docking scores: lower is better
    kept: int = 0

    def __post_init__(self) -> None:
        if int(self.cap) <= 0:
            raise ValueError(
                "HitGate requires a positive cap. The threshold is the scientific criterion; "
                "the cap is what makes the campaign terminate."
            )
        if self.op not in ("<=", ">="):
            raise ValueError(f"HitGate.op must be '<=' or '>=', got {self.op!r}.")

    @property
    def full(self) -> bool:
        return self.kept >= int(self.cap)

    def passes(self, row: Mapping[str, Any]) -> bool:
        if self.threshold is None:
            return True
        value = row.get(self.metric)
        if value is None:
            return False
        return float(value) <= float(self.threshold) if self.op == "<=" else float(value) >= float(self.threshold)

    def take(self, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """The rows that get materialized, never more than `cap` over the gate's lifetime."""
        kept: list[dict[str, Any]] = []
        for row in rows:
            if self.full:
                break
            if not self.passes(row):
                continue
            kept.append(dict(row))
            self.kept += 1
        return kept

    def drain(self) -> list[dict[str, Any]]:
        """What the gate held back for the end. Nothing: this one writes as it goes."""
        return []

    def discarded(self) -> list[dict[str, Any]]:
        """Rows that were let in and later dropped. Never happens without ranking."""
        return []


@dataclass
class TopNGate(HitGate):
    """The best `cap` of the whole campaign, ranked — written only when the campaign ends.

    What it buys over `HitGate`: the rows in the project are the top N, not the first N to beat
    the threshold. What it costs: the database stays empty until the run finishes, and the run
    cannot stop early (there is no "enough" before the last ligand is scored).

    It never holds the library: the threshold is still the coarse filter in the worker, and the
    buffer here is exactly `cap` rows. `discarded()` hands back what got pushed out, so the
    caller can delete the pose files that will never be needed.
    """

    # (rank_key, sequence, row) — a min-heap whose smallest element is the worst kept row, so
    # one pop per insertion past the cap keeps it at N.
    _heap: list[tuple[float, int, dict[str, Any]]] = field(default_factory=list, repr=False)
    _seq: int = 0
    _evicted: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def full(self) -> bool:
        return False  # a ranked campaign ends when the library does, not when the buffer fills

    def _rank(self, row: Mapping[str, Any]) -> float:
        value = float(row[self.metric])
        return -value if self.op == "<=" else value  # bigger rank = better

    def take(self, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        for row in rows:
            if not self.passes(row) or row.get(self.metric) is None:
                continue
            self._seq += 1
            heapq.heappush(self._heap, (self._rank(row), self._seq, dict(row)))
            if len(self._heap) > int(self.cap):
                self._evicted.append(heapq.heappop(self._heap)[2])
        self.kept = len(self._heap)
        return []  # nothing is written before the ranking is final

    def drain(self) -> list[dict[str, Any]]:
        rows = [row for _rank, _seq, row in sorted(self._heap, reverse=True)]
        self._heap.clear()
        return rows

    def discarded(self) -> list[dict[str, Any]]:
        evicted, self._evicted = self._evicted, []
        return evicted


def _relative(path: str, project_root: Path | None) -> str:
    text = str(path or "").strip()
    if not text or project_root is None:
        return text
    candidate = Path(text)
    if not candidate.is_absolute():
        return text
    try:
        return str(candidate.relative_to(project_root))
    except ValueError:
        return str(candidate)


def _molecule_row(hit: Mapping[str, Any], *, payload: str, project_root: Path | None) -> dict[str, Any]:
    pose = _relative(str(hit.get("pose_path") or ""), project_root)
    row: dict[str, Any] = {
        "name": str(hit.get("name") or ""),
        "molecule_type": MoleculeType.SMALL_MOLECULE,
        "source": str(hit.get("source") or ""),
        "source_index": int(hit.get("source_index") or 0),
        "input_format": str(hit.get("input_format") or FileFormat.SDF),
        "is_ligand": True,
        # The pose is the only 3D a light hit carries, so it is also its canonical copy.
        "stored_path": pose,
        "current_path": pose,
        "extra_data": {"smiles": str(hit.get("smiles") or "")},
    }
    if payload == PAYLOAD_FULL:
        row.update({key: hit[key] for key in DESCRIPTOR_FIELDS if hit.get(key) is not None})
    return row


def ingest_hits(
    project_db,
    hits: Iterable[Mapping[str, Any]],
    *,
    gate: HitGate,
    payload: str = PAYLOAD_LIGHT,
    project_root: Path | str | None = None,
    reuse_existing: bool = False,
) -> list[dict[str, Any]]:
    """Promote hits into `molecules` + `docking_results`, gated.

    Returns the materialized rows with their new `molecule_id`. Call it per returned batch:
    the gate carries the count across calls, so the cap holds over the whole campaign.

    ponytail: writes through a session instead of a job sink because the cap is what bounds
    this — at most `cap` rows ever reach it, and it runs in the process that ingests, not in a
    worker.
    """
    if payload not in (PAYLOAD_LIGHT, PAYLOAD_FULL):
        raise ValueError(f"payload must be '{PAYLOAD_LIGHT}' or '{PAYLOAD_FULL}', got {payload!r}.")
    root = Path(project_root).expanduser().resolve() if project_root else None
    materialized: list[dict[str, Any]] = []
    selected = gate.take(hits)
    if not selected:
        return materialized
    with project_db.get_session() as session:
        molecules_by_source: dict[tuple[str, int], MoleculeRecord] = {}
        if reuse_existing:
            sources = sorted({str(hit.get("source") or "") for hit in selected})
            existing = session.exec(
                select(MoleculeRecord).where(
                    MoleculeRecord.is_ligand.is_(True),
                    MoleculeRecord.source.in_(sources),
                )
            ).all() if sources else []
            molecules_by_source = {
                (str(row.source), int(row.source_index)): row for row in existing
            }
        for hit in selected:
            source_key = (str(hit.get("source") or ""), int(hit.get("source_index") or 0))
            molecule = molecules_by_source.get(source_key)
            if molecule is None:
                molecule = MoleculeRecord(**_molecule_row(hit, payload=payload, project_root=root))
                session.add(molecule)
                session.flush()  # need the id for the result row
                if reuse_existing:
                    molecules_by_source[source_key] = molecule
            molecule_id = int(molecule.id or 0)
            result_row = DockingResult.build_row(
                receptor_molecule_id=int(hit.get("receptor_molecule_id") or 0),
                ligand_molecule_id=molecule_id,
                engine=str(hit.get("engine") or ""),
                pose_rank=int(hit.get("pose_rank") or 1),
                score=None if hit.get("score") is None else float(hit["score"]),
                score_type=str(hit.get("score_type") or ""),
                pose_path=_relative(str(hit.get("pose_path") or ""), root),
                metrics=dict(hit.get("metrics") or {}),
            )
            docking_result = None
            if reuse_existing:
                wanted_metrics = dict(result_row.get("metrics") or {})
                wanted_run = str(wanted_metrics.get("run_id") or "")
                wanted_protocol = str(dict(wanted_metrics.get("protocol") or {}).get("hash") or "")
                candidates = session.exec(select(DockingResult).where(
                    DockingResult.receptor_molecule_id == result_row["receptor_molecule_id"],
                    DockingResult.ligand_molecule_id == molecule_id,
                    DockingResult.engine == result_row["engine"],
                    DockingResult.pose_rank == result_row["pose_rank"],
                )).all()
                docking_result = next((
                    row for row in candidates
                    if str(dict(row.metrics or {}).get("run_id") or "") == wanted_run
                    and str(dict(dict(row.metrics or {}).get("protocol") or {}).get("hash") or "")
                    == wanted_protocol
                ), None)
            if docking_result is None:
                docking_result = DockingResult(**result_row)
            else:
                docking_result.score = result_row["score"]
                docking_result.score_type = result_row["score_type"]
                docking_result.pose_path = result_row["pose_path"]
                docking_result.metrics = result_row["metrics"]
            session.add(docking_result)
            if payload == PAYLOAD_FULL:
                for index, model_path in enumerate(hit.get("models") or ()):
                    session.add(
                        MoleculeModel(
                            **MoleculeModel.build_row(
                                molecule_id=molecule_id,
                                model_index=index,
                                file_path=_relative(str(model_path), root),
                                source=ModelSource.IMPORTED,
                            )
                        )
                    )
            materialized.append({**hit, "molecule_id": molecule_id})
        session.commit()
    return materialized


__all__ = [
    "DESCRIPTOR_FIELDS",
    "HitGate",
    "LIGHT_FIELDS",
    "PAYLOAD_FULL",
    "PAYLOAD_LIGHT",
    "TopNGate",
    "ingest_hits",
]
