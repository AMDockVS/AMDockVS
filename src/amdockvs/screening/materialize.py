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
from typing import Any, Iterable, Mapping

from sqlmodel import select

from amdockvs.core.payloads import payload_store
from amdockvs.models import DockingResult, MoleculeModel, MoleculeRecord, MoleculeRepresentation
from amdockvs.core.vocab import ModelSource, MoleculeType, ReprType

PAYLOAD_LIGHT = "light"
PAYLOAD_FULL = "full"

# What a light payload keeps: the pose geometry and the score (unrecomputable) plus the SMILES
# (recomputes everything else). Descriptors, conformers and prepared files are ~60 bytes of
# identity away from being regenerated locally, so they only travel in `full`.
LIGHT_FIELDS = (
    "name", "import_batch_id", "source_index", "smiles", "docking_run_id", "score", "score_type",
    "pose", "pose_rank", "receptor_molecule_id", "extra",
)

# Descriptor columns a `full` payload may carry straight into the molecule row.
DESCRIPTOR_FIELDS = (
    "mw", "logp", "hbd", "hba", "tpsa", "rotatable_bonds", "ring_count",
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
    caller knows what was pushed out.
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


def _molecule_row(hit: Mapping[str, Any], *, payload: str, structure_id: int | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": str(hit.get("name") or ""),
        "molecule_type": MoleculeType.SMALL_MOLECULE,
        "import_batch_id": hit.get("import_batch_id"),
        "source_index": int(hit.get("source_index") or 0),
        "is_ligand": True,
        # The pose is the only 3D a light hit carries, so it is also its structure: one payload
        # that the molecule and its docking result both hold.
        "stored_payload_id": structure_id,
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
    reuse_existing: bool = False,
) -> list[dict[str, Any]]:
    """Promote hits into `molecules` + `docking_results`, gated.

    A hit's `pose` is a `Payload` (series `poses`); a `full` hit's `models` are `Payload`s too.
    Returns the materialized rows with their new `molecule_id` and `pose_payload_id`. Call it per
    returned batch: the gate carries the count across calls, so the cap holds over the whole
    campaign.

    ponytail: writes through a session instead of a job sink because the cap is what bounds
    this — at most `cap` rows ever reach it, and it runs in the process that ingests, not in a
    worker.
    """
    if payload not in (PAYLOAD_LIGHT, PAYLOAD_FULL):
        raise ValueError(f"payload must be '{PAYLOAD_LIGHT}' or '{PAYLOAD_FULL}', got {payload!r}.")
    materialized: list[dict[str, Any]] = []
    selected = gate.take(hits)
    if not selected:
        return materialized
    store = payload_store(project_db)
    replaced: list[int] = []  # poses of results written again: released once the new ones commit
    with project_db.get_session() as session:
        posed = [hit for hit in selected if hit.get("pose") is not None]
        pose_ids = dict(zip(map(id, posed), store.put(session, [hit["pose"] for hit in posed])))
        molecules_by_source: dict[tuple[int | None, int], MoleculeRecord] = {}
        if reuse_existing:
            batches = sorted({hit["import_batch_id"] for hit in selected if hit.get("import_batch_id") is not None})
            existing = session.exec(
                select(MoleculeRecord).where(
                    MoleculeRecord.is_ligand.is_(True),
                    MoleculeRecord.import_batch_id.in_(batches),
                )
            ).all() if batches else []
            molecules_by_source = {
                (row.import_batch_id, int(row.source_index)): row for row in existing
            }
        for hit in selected:
            pose_id = pose_ids.get(id(hit))
            source_key = (hit.get("import_batch_id"), int(hit.get("source_index") or 0))
            molecule = molecules_by_source.get(source_key)
            if molecule is None:
                molecule = MoleculeRecord(**_molecule_row(hit, payload=payload, structure_id=pose_id))
                session.add(molecule)
                session.flush()  # need the id for the result row
                if hit.get("smiles"):  # Meeko's, from the PDBQT: the hit's only 2D
                    session.add(MoleculeRepresentation(molecule_id=int(molecule.id), repr_type=ReprType.SMILES_ISOMERIC,
                                                       value=str(hit["smiles"])))
                if reuse_existing:
                    molecules_by_source[source_key] = molecule
            molecule_id = int(molecule.id or 0)
            # The hit carries the result columns; its ligand is the molecule it just became.
            result_row = DockingResult.build_row(**{
                **hit, "ligand_molecule_id": molecule_id, "pose_payload_id": pose_id,
                "receptor_molecule_id": int(hit.get("receptor_molecule_id") or 0),
                "pose_rank": int(hit.get("pose_rank") or 1),
            })
            docking_result = None
            if reuse_existing:
                docking_result = session.exec(select(DockingResult).where(
                    DockingResult.docking_run_id == result_row["docking_run_id"],
                    DockingResult.receptor_molecule_id == result_row["receptor_molecule_id"],
                    DockingResult.ligand_molecule_id == molecule_id,
                    DockingResult.pose_rank == result_row["pose_rank"],
                )).first()
            if docking_result is None:
                docking_result = DockingResult(**result_row)
            else:
                if docking_result.pose_payload_id not in (None, pose_id):
                    replaced.append(int(docking_result.pose_payload_id))
                docking_result.sqlmodel_update(result_row)
            session.add(docking_result)
            if payload == PAYLOAD_FULL:
                models = list(hit.get("models") or ())
                for model_id in store.put(session, models):
                    session.add(
                        MoleculeModel(
                            **MoleculeModel.build_row(
                                molecule_id=molecule_id,
                                payload_id=model_id,
                                source=ModelSource.IMPORTED,
                            )
                        )
                    )
            materialized.append({**hit, "molecule_id": molecule_id, "pose_payload_id": pose_id})
        session.commit()
    if replaced:
        store.release(replaced)  # a pose that is also a molecule's structure stays
    return materialized


def promote_records(project_db, records: Iterable[Mapping[str, Any]]) -> dict[tuple[int | None, int], int]:
    """Library records into `molecules` rows and nothing else: what a reduction of a sharded
    library keeps (a clustering's representatives, a QSAR cut). A record promoted before — a
    hit, an earlier pick — keeps its row. A record's `structure` is a `Payload`. Returns
    {(import_batch_id, source_index): molecule_id}.

    ponytail: a session, like `ingest_hits`: what reaches it is the reduction, never the library.
    """
    pending: dict[tuple[int | None, int], Mapping[str, Any]] = {
        (record.get("import_batch_id"), int(record.get("source_index") or 0)): record for record in records
    }
    if not pending:
        return {}
    promoted: dict[tuple[int | None, int], int] = {}
    with project_db.get_session() as session:
        batches = sorted({batch for batch, _index in pending if batch is not None})
        for molecule_id, batch, index in session.exec(
            select(MoleculeRecord.id, MoleculeRecord.import_batch_id, MoleculeRecord.source_index).where(
                MoleculeRecord.is_ligand.is_(True), MoleculeRecord.import_batch_id.in_(batches)
            )
        ).all() if batches else ():
            if (batch, int(index or 0)) in pending:
                promoted[(batch, int(index or 0))] = int(molecule_id)
        new = [(key, record) for key, record in pending.items() if key not in promoted]
        structure_ids = payload_store(project_db).put(session, [record["structure"] for _key, record in new])
        for (key, record), structure_id in zip(new, structure_ids):
            molecule = MoleculeRecord(**_molecule_row(record, payload=PAYLOAD_LIGHT, structure_id=structure_id))
            session.add(molecule)
            session.flush()
            if record.get("smiles"):
                session.add(MoleculeRepresentation(molecule_id=int(molecule.id), repr_type=ReprType.SMILES_ISOMERIC,
                                                   value=str(record["smiles"])))
            promoted[key] = int(molecule.id)
        session.commit()
    return promoted


__all__ = [
    "DESCRIPTOR_FIELDS",
    "HitGate",
    "LIGHT_FIELDS",
    "PAYLOAD_FULL",
    "PAYLOAD_LIGHT",
    "TopNGate",
    "ingest_hits",
    "promote_records",
]
