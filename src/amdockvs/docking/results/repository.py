"""Reading docking results back out of the project database.

Everything here is a query over `docking_results` and `interaction_results`: hit lists,
stats, off-target grids, pivots. The write side lives in the jobs; this module only reads
(plus the two deletions the UI needs).
"""
from __future__ import annotations

import json
from contextlib import suppress
from sqlite3 import OperationalError
from typing import Any, Iterable

from ms_flow.query import db_rows
from sqlalchemy import tuple_
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import func, select

from amdockvs.core.constants import (
    TABLE_COMPLEXES,
    TABLE_DOCKING_RESULTS,
    TABLE_DOCKING_RUNS,
    TABLE_MOLECULES,
    TABLE_INTERACTION_RESULTS,
    TABLE_SCREENING_TARGETS,
)
from amdockvs.models import DockingResultRecord, DockingRun
from amdockvs.docking.repository import project_db_path
from amdockvs.docking.results.metrics import SCORE_METRICS
from amdockvs.project.deletion import delete_results_where
from amdockvs.project.summaries import DockingHitSummary, DockingResultsStatsSummary, ReceptorDockingSummary


def docking_run_id(project_db, *, run_id: str, engine: str, run_kind: str, protocol: dict[str, Any]) -> int:
    """The `docking_runs` row a job's results point to, created the first time it is asked for."""
    values = {"run_id": str(run_id or ""), "engine": str(engine).strip().lower(),
              "run_kind": str(run_kind or "screening"), "protocol_hash": str(protocol.get("hash") or "")}
    with project_db.get_session() as session:
        session.exec(insert(DockingRun).values(
            **values, protocol=dict(protocol),
            protocol_label=str(protocol.get("label") or protocol.get("program") or values["engine"]),
        ).on_conflict_do_nothing())
        found = session.exec(select(DockingRun.id).filter_by(**values)).one()
        session.commit()
        return int(found)


def runs_where(*, run_id: str | None = None, engine: str | None = None,
               run_kind: str | None = None, protocol_hash: str | None = None):
    """The ids of the docking runs that match, as a subquery for `docking_run_id IN (...)`."""
    statement = select(DockingRun.id)
    if run_id is not None:
        statement = statement.where(DockingRun.run_id == str(run_id))
    if engine:
        statement = statement.where(DockingRun.engine == str(engine).strip().lower())
    if run_kind:
        statement = statement.where(DockingRun.run_kind == str(run_kind))
    if protocol_hash:
        statement = statement.where(DockingRun.protocol_hash == str(protocol_hash))
    return statement


# --- "already computed" guard ------------------------------------------------
# A job's batch builder filters its inputs against the outputs that already exist, so
# re-running only computes what's missing.

def existing_result_pairs(
    project_db,
    *,
    engine: str,
    run_kind: str | None = None,
    protocol_hash: str | None = None,
) -> set[tuple[int, int]]:
    record = DockingResultRecord
    pairs: set[tuple[int, int]] = set()
    with suppress(OperationalError):  # results table not created until the first docking run
        with project_db.get_session() as session:
            statement = select(record.receptor_molecule_id, record.ligand_molecule_id).where(
                record.docking_run_id.in_(runs_where(engine=engine, run_kind=run_kind,
                                                     protocol_hash=str(protocol_hash or "").strip())))
            for receptor_id, ligand_id in session.exec(statement.distinct()).all():
                pairs.add((int(receptor_id), int(ligand_id)))
    return pairs


def count_docking_results(project_db, *, run_id: str = "", score_lte: float | None = None) -> int:
    """How many result rows a run has already written (optionally, only the ones under a score).

    This is what a capped run asks between chunks: the answer lives in the database because
    the rows are written there by the sink, in another process than the feed.
    """
    clauses = []
    if str(run_id).strip():
        clauses.append(DockingResultRecord.docking_run_id.in_(runs_where(run_id=str(run_id).strip())))
    if score_lte is not None:
        clauses.append(DockingResultRecord.score <= float(score_lte))
    with project_db.get_session() as session:
        return int(
            session.exec(select(func.count()).select_from(DockingResultRecord).where(*clauses)).one()
        )


def list_docking_result_rows(
    project_db,
    *,
    result_ids: Iterable[int] = (),
    run_id: str = "",
    receptor_id: int | None = None,
    score_lte: float | None = None,
    pose_rank: int | None = None,
) -> list[dict[str, Any]]:
    """Poses with a structure and a score, for the interaction and diagram jobs."""
    clauses = [DockingResultRecord.score.is_not(None), DockingResultRecord.pose_payload_id.is_not(None)]
    ids = [int(value) for value in result_ids if int(value) > 0]
    if ids:
        clauses.append(DockingResultRecord.id.in_(ids))
    if str(run_id).strip():
        clauses.append(DockingResultRecord.docking_run_id.in_(runs_where(run_id=str(run_id).strip())))
    if receptor_id is not None:
        clauses.append(DockingResultRecord.receptor_molecule_id == int(receptor_id))
    if score_lte is not None:
        clauses.append(DockingResultRecord.score <= float(score_lte))
    if pose_rank is not None:
        clauses.append(DockingResultRecord.pose_rank == int(pose_rank))
    with project_db.get_session() as session:
        rows = session.exec(
            select(DockingResultRecord).where(*clauses).order_by(DockingResultRecord.id)
        ).all()
        return [row.model_dump(mode="python") for row in rows]


def supersede_results(project_db, *, run_id: str) -> int:
    """Forced re-run: drop the older rows of the pairs this run has results for, so a re-dock
    replaces instead of duplicating. A pair the run did not get to keeps what it had: nothing is
    deleted before its replacement is stored. What was computed from a dropped row (its
    interactions) goes with it: the new row has them only if they are computed again."""
    run = str(run_id).strip()
    if not run:
        return 0
    record, ran = DockingResultRecord, DockingRun
    # What makes two rows results of the same docking: the pair, and the run's engine, kind and protocol.
    same = (record.receptor_molecule_id, record.ligand_molecule_id, ran.engine, ran.run_kind, ran.protocol_hash)
    of_run = select(*same).join(ran, ran.id == record.docking_run_id).where(ran.run_id == run)
    older = (select(record.id).join(ran, ran.id == record.docking_run_id)
             .where(ran.run_id != run, tuple_(*same).in_(of_run)))
    return delete_results_where(project_db, record.id.in_(older))


# --- Docking results ---------------------------------------------------------------
# They used to live in a global `queries.py`: touching docking forced touching a module shared
# with molecules. They carry JOIN and aggregation, which QuerySpec does not express, so they use
# the raw `query=` escape hatch of db_rows.

def _decode_metadata(raw_metadata: str | None) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return dict(raw_metadata)
    if not raw_metadata:
        return {}
    try:
        parsed = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return {"raw_metadata_json": raw_metadata}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _hit_select_sql(*, score_expr: str = "r.score", extra: str = "") -> str:
    """The SELECT/FROM shared by every hit query. `score_expr` is `MIN(r.score)` when the query
    groups by ligand: SQLite then picks the rest of the bare columns from that same (best) row."""
    return (
        "SELECT "
        f"{extra}"
        "r.id AS result_id, "
        "COALESCE(r.complex_id, c.id) AS complex_id, "
        "dr.run_kind AS run_kind, "
        "r.ligand_molecule_id AS ligand_id, "
        "COALESCE(l.name, '') AS ligand_name, "
        "r.receptor_molecule_id AS receptor_id, "
        "COALESCE(rec.name, '') AS receptor_name, "
        "dr.engine AS engine, "
        "dr.protocol_label AS protocol_label, "
        "dr.protocol_hash AS protocol_hash, "
        f"{score_expr} AS score, "
        "r.rmsd_vs_reference AS rmsd_vs_reference, "
        "r.ligand_efficiency, r.predicted_pki, r.lle, r.fit_quality, r.bei, r.sei, "
        "CASE WHEN r.score IS NULL THEN 'failed' ELSE 'completed' END AS status, "
        "COALESCE(r.error, '') AS error, "
        "r.pose_payload_id AS pose_payload_id, "
        "COALESCE(l.current_payload_id, l.stored_payload_id) AS ligand_payload_id, "
        "COALESCE(r.receptor_payload_id, rec.current_payload_id, rec.stored_payload_id) AS receptor_payload_id, "
        "COALESCE(c.reference_ligand_payload_id, l.stored_payload_id) AS reference_ligand_payload_id, "
        "COALESCE(c.reference_receptor_payload_id, rec.current_payload_id, rec.stored_payload_id) AS reference_receptor_payload_id, "
        "r.pose_rank AS pose_rank, "
        "r.extra AS metadata_json, "
        "dr.created_at AS created_at "
        f"FROM {TABLE_DOCKING_RESULTS} r "
        f"JOIN {TABLE_DOCKING_RUNS} dr ON dr.id = r.docking_run_id "
        f"LEFT JOIN {TABLE_MOLECULES} l ON l.id = r.ligand_molecule_id "
        f"LEFT JOIN {TABLE_MOLECULES} rec ON rec.id = r.receptor_molecule_id "
        f"LEFT JOIN {TABLE_COMPLEXES} c ON c.receptor_molecule_id = r.receptor_molecule_id "
        f"AND c.ligand_molecule_id = r.ligand_molecule_id "
    )


def _hit_filter_clauses(
    *,
    receptor_id: int | None = None,
    ligand_id: int | None = None,
    only_completed: bool = False,
    score_lte: float | None = None,
    metric_key: str | None = None,
    metric_lte: float | None = None,
    metric_gte: float | None = None,
    metric_filters: list[tuple[str, str, float]] | None = None,
    run_kind: str | None = None,
    protocol_hash: str | None = None,
    exclude_run_kind: str | None = None,
) -> list[str]:
    clauses: list[str] = []
    if only_completed:
        clauses.append("r.score IS NOT NULL")
    if receptor_id is not None:
        clauses.append(f"r.receptor_molecule_id = {int(receptor_id)}")
    if ligand_id is not None:
        clauses.append(f"r.ligand_molecule_id = {int(ligand_id)}")
    if run_kind is not None:
        normalized_run_kind = str(run_kind or "").strip().replace("'", "''")
        if normalized_run_kind:
            clauses.append(f"dr.run_kind = '{normalized_run_kind}'")
    if exclude_run_kind is not None:
        excluded_run_kind = str(exclude_run_kind or "").strip().replace("'", "''")
        if excluded_run_kind:
            clauses.append(f"dr.run_kind != '{excluded_run_kind}'")
    if protocol_hash is not None:
        normalized_hash = str(protocol_hash).replace("'", "''")
        clauses.append(f"dr.protocol_hash = '{normalized_hash}'")
    if score_lte is not None:
        clauses.append(f"r.score <= {float(score_lte)}")
    metric_expr = _metric_expr(metric_key)
    if metric_expr is not None:
        if metric_lte is not None:
            clauses.append(f"{metric_expr} <= {float(metric_lte)}")
        if metric_gte is not None:
            clauses.append(f"{metric_expr} >= {float(metric_gte)}")
    # Additive conditions from the results view's filter chips: they AND together, and two of
    # them on the same field ("gte" + "lte") are how a range gets expressed.
    for field, op, value in list(metric_filters or []):
        expr = _filter_expr(field)
        sql_op = _FILTER_OPS.get(str(op))
        if expr is not None and sql_op is not None:
            clauses.append(f"{expr} {sql_op} {float(value)}")
    return clauses


# A filterable metric -> its column; LLE keeps its older name in the UI.
_RESULT_METRIC_COLUMNS = {key: key for key in SCORE_METRICS} | {"lipophilic_efficiency": "lle"}


_FILTER_OPS = {"gte": ">=", "lte": "<="}


def _filter_expr(field: str | None) -> str | None:
    """SQL for a filterable result field: the score column, or one of the metric columns."""
    key = str(field or "").strip()
    return "r.score" if key == "score" else _metric_expr(key)


def _metric_expr(metric_key: str | None) -> str | None:
    column = _RESULT_METRIC_COLUMNS.get(str(metric_key or "").strip())
    return None if column is None else f"r.{column}"


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_docking_results_stats(project_db) -> DockingResultsStatsSummary:
    rows = db_rows(
        project_db,
        query=(
            "SELECT "
            # A "result" is a docked receptor-ligand PAIR, not a pose row (N poses per pair).
            # A failed pair is stored as a single row with score IS NULL (see run_vina_docking_rows).
            "COUNT(DISTINCT receptor_molecule_id || '-' || ligand_molecule_id) AS total_results, "
            "COUNT(DISTINCT CASE WHEN score IS NOT NULL THEN receptor_molecule_id || '-' || ligand_molecule_id END) AS completed_results, "
            "(COUNT(DISTINCT receptor_molecule_id || '-' || ligand_molecule_id) "
            "- COUNT(DISTINCT CASE WHEN score IS NOT NULL THEN receptor_molecule_id || '-' || ligand_molecule_id END)) AS failed_results, "
            "0 AS pending_results, "
            "COUNT(DISTINCT ligand_molecule_id) AS unique_ligands, "
            "COUNT(DISTINCT receptor_molecule_id) AS unique_receptors, "
            "MIN(score) AS best_score, "
            "AVG(score) AS avg_score, "
            "MAX(score) AS worst_score "
            f"FROM {TABLE_DOCKING_RESULTS}"
        ),
    )
    row = rows[0] if rows else {}
    return DockingResultsStatsSummary(
        total_results=int(row.get("total_results") or 0),
        completed_results=int(row.get("completed_results") or 0),
        failed_results=int(row.get("failed_results") or 0),
        pending_results=int(row.get("pending_results") or 0),
        unique_ligands=int(row.get("unique_ligands") or 0),
        unique_receptors=int(row.get("unique_receptors") or 0),
        best_score=None if row.get("best_score") is None else float(row.get("best_score")),
        avg_score=None if row.get("avg_score") is None else float(row.get("avg_score")),
        worst_score=None if row.get("worst_score") is None else float(row.get("worst_score")),
    )

def list_results(project_db, *, limit: int = 5000, offset: int = 0) -> list[DockingHitSummary]:
    # One summary per docked receptor-ligand PAIR: the selected pose (pose_rank=1 = best
    # score). Failed pairs never reach the table (they go to the run report), so every row
    # here is a completed result. Bounded: a screening has as many rows here as pairs docked.
    rows = db_rows(
        project_db,
        query=(
            _hit_select_sql()
            +
            "WHERE r.pose_rank = 1 "
            "ORDER BY r.receptor_molecule_id ASC, r.ligand_molecule_id ASC, protocol_label ASC, r.pose_rank ASC "
            f"LIMIT {max(1, int(limit))} OFFSET {max(0, int(offset))}"
        ),
    )
    return [_hit_summary(project_db, row) for row in rows]

def _hit_summary(project_db, row: dict[str, Any]) -> DockingHitSummary:
    return DockingHitSummary(
        result_id=int(row.get("result_id") or 0),
        complex_id=None if row.get("complex_id") is None else int(row.get("complex_id")),
        run_kind=str(row.get("run_kind") or "screening"),
        ligand_id=int(row.get("ligand_id") or 0),
        ligand_name=str(row.get("ligand_name") or ""),
        receptor_id=int(row.get("receptor_id") or 0),
        receptor_name=str(row.get("receptor_name") or ""),
        engine=str(row.get("engine") or ""),
        protocol_label=str(row.get("protocol_label") or ""),
        protocol_hash=str(row.get("protocol_hash") or ""),
        score=float(row.get("score") or 0.0),
        rmsd_vs_reference=_float_or_none(row.get("rmsd_vs_reference")),
        ligand_efficiency=_float_or_none(row.get("ligand_efficiency")),
        predicted_ki_m=None if row.get("predicted_pki") is None else 10.0 ** -float(row["predicted_pki"]),
        predicted_pki=_float_or_none(row.get("predicted_pki")),
        lipophilic_efficiency=_float_or_none(row.get("lle")),
        fit_quality=_float_or_none(row.get("fit_quality")),
        bei=_float_or_none(row.get("bei")),
        sei=_float_or_none(row.get("sei")),
        status=str(row.get("status") or ""),
        error=str(row.get("error") or ""),
        pose_payload_id=row.get("pose_payload_id"),
        ligand_payload_id=row.get("ligand_payload_id"),
        receptor_payload_id=row.get("receptor_payload_id"),
        reference_ligand_payload_id=row.get("reference_ligand_payload_id"),
        reference_receptor_payload_id=row.get("reference_receptor_payload_id"),
        metadata=_decode_metadata(row.get("metadata_json")),
        pose_rank=int(row.get("pose_rank") or 1),
        created_at=row.get("created_at"),
    ).of_project(project_db)

def list_top_hits(
    project_db,
    *,
    limit: int = 25,
    receptor_id: int | None = None,
    ligand_id: int | None = None,
    only_completed: bool = True,
    score_lte: float | None = None,
    metric_key: str | None = None,
    metric_lte: float | None = None,
    metric_gte: float | None = None,
    metric_filters: list[tuple[str, str, float]] | None = None,
    run_kind: str | None = None,
    protocol_hash: str | None = None,
    exclude_run_kind: str | None = None,
    offset: int = 0,
) -> list[DockingHitSummary]:
    clauses = _hit_filter_clauses(
        receptor_id=receptor_id,
        ligand_id=ligand_id,
        only_completed=only_completed,
        score_lte=score_lte,
        metric_key=metric_key,
        metric_lte=metric_lte,
        metric_gte=metric_gte,
        metric_filters=metric_filters,
        run_kind=run_kind,
        protocol_hash=protocol_hash,
        exclude_run_kind=exclude_run_kind,
    )
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_rows(
        project_db,
        query=(
            _hit_select_sql()
            + f"{where_clause} "
            "ORDER BY (r.score IS NULL) ASC, r.score ASC, r.id DESC "
            f"LIMIT {max(1, int(limit))} OFFSET {max(0, int(offset))}"
        ),
    )
    return [_hit_summary(project_db, row) for row in rows]


def get_hit(project_db, *, result_id: int) -> DockingHitSummary | None:
    """Hydrate one selected pose for the detail panel and molecular viewer."""
    rows = db_rows(
        project_db,
        query=(
            _hit_select_sql()
            + f"WHERE r.id = {int(result_id)} LIMIT 1"
        ),
    )
    return _hit_summary(project_db, rows[0]) if rows else None

def list_ligand_result_summaries(
    project_db,
    *,
    limit: int = 200,
    offset: int = 0,
    receptor_id: int | None = None,
    only_completed: bool = False,
    score_lte: float | None = None,
    metric_key: str | None = None,
    metric_lte: float | None = None,
    metric_gte: float | None = None,
    metric_filters: list[tuple[str, str, float]] | None = None,
    protocol_hash: str | None = None,
    exclude_run_kind: str | None = None,
) -> list[tuple[DockingHitSummary, int]]:
    """One row per ligand — its best pose plus how many poses it has — paginated.

    The results view used to pull every pose of every ligand and group them in Python, which
    is a full scan of the results table per open. Here SQLite groups, and the page is what
    the table actually shows.
    """
    clauses = _hit_filter_clauses(
        receptor_id=receptor_id,
        only_completed=only_completed,
        score_lte=score_lte,
        metric_key=metric_key,
        metric_lte=metric_lte,
        metric_gte=metric_gte,
        metric_filters=metric_filters,
        protocol_hash=protocol_hash,
        exclude_run_kind=exclude_run_kind,
    )
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_rows(
        project_db,
        query=(
            # MIN(r.score) makes SQLite take every other bare column from that same row,
            # so the summary is literally the best pose's row.
            _hit_select_sql(score_expr="MIN(r.score)", extra="COUNT(*) AS pose_count, ")
            + f"{where_clause} "
            "GROUP BY r.ligand_molecule_id "
            "ORDER BY (score IS NULL) ASC, score ASC, ligand_id ASC "
            f"LIMIT {max(1, int(limit))} OFFSET {max(0, int(offset))}"
        ),
    )
    return [(_hit_summary(project_db, row), int(row.get("pose_count") or 0)) for row in rows]

def list_result_protocols(
    project_db,
    *,
    receptor_id: int | None = None,
    exclude_run_kind: str | None = "redocking",
) -> list[tuple[str, str]]:
    """Distinct (protocol_hash, label) present in results — cheap combo source so the
    results view filters by protocol in SQL instead of scanning a client-side window."""
    # The runs that have a result (for that receptor): a run with none has nothing to filter.
    of_receptor = "" if receptor_id is None else f"AND r.receptor_molecule_id = {int(receptor_id)} "
    clauses = [f"EXISTS (SELECT 1 FROM {TABLE_DOCKING_RESULTS} r WHERE r.docking_run_id = dr.id {of_receptor})"]
    if exclude_run_kind:
        excluded = str(exclude_run_kind).strip().replace("'", "''")
        if excluded:
            clauses.append(f"dr.run_kind != '{excluded}'")
    rows = db_rows(
        project_db,
        query=(
            "SELECT DISTINCT dr.protocol_hash, dr.protocol_label, dr.engine "
            f"FROM {TABLE_DOCKING_RUNS} dr WHERE {' AND '.join(clauses)}"
        ),
    )
    by_hash: dict[str, str] = {}
    for row in rows:
        phash = str(row.get("protocol_hash") or "")
        label = str(row.get("protocol_label") or row.get("engine") or "default")
        by_hash.setdefault(phash, label)
    return sorted(by_hash.items(), key=lambda item: item[1].lower())

def list_offtarget_rows(
    project_db,
    *,
    receptor_ids: list[int] | None = None,
    ligand_ids: list[int] | None = None,
    protocol_hash: str | None = None,
    limit: int = 50000,
) -> list[dict[str, Any]]:
    """Flat per-pose rows for the ligand-centric off-target matrix: one dict per
    (ligand, receptor, pose) with score, ligand efficiency and the payload ids of pose, receptor, ligand.
    With `protocol_hash`, only the screening poses of that protocol (what the matrix compares)."""
    clauses: list[str] = []
    if protocol_hash is not None:
        clauses.append(
            f"r.docking_run_id IN (SELECT id FROM {TABLE_DOCKING_RUNS} WHERE run_kind != 'redocking' "
            f"AND protocol_hash = '{str(protocol_hash).replace(chr(39), chr(39) * 2)}')"
        )
    if receptor_ids:
        ids = ",".join(str(int(rid)) for rid in receptor_ids)
        clauses.append(f"r.receptor_molecule_id IN ({ids})")
    if ligand_ids:
        lids = ",".join(str(int(lid)) for lid in ligand_ids)
        clauses.append(f"r.ligand_molecule_id IN ({lids})")
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_rows(
        project_db,
        query=(
            "SELECT "
            "r.ligand_molecule_id AS ligand_id, "
            "COALESCE(l.name, '') AS ligand_name, "
            "r.receptor_molecule_id AS receptor_id, "
            "COALESCE(rec.name, '') AS receptor_name, "
            "r.pose_rank AS pose_rank, "
            "r.score AS score, "
            "r.ligand_efficiency AS ligand_efficiency, "
            "r.pose_payload_id AS pose_payload_id, "
            "COALESCE(r.receptor_payload_id, rec.current_payload_id, rec.stored_payload_id) AS receptor_payload_id, "
            "COALESCE(l.current_payload_id, l.stored_payload_id) AS ligand_payload_id "
            f"FROM {TABLE_DOCKING_RESULTS} r "
            f"LEFT JOIN {TABLE_MOLECULES} l ON l.id = r.ligand_molecule_id "
            f"LEFT JOIN {TABLE_MOLECULES} rec ON rec.id = r.receptor_molecule_id "
            f"{where_clause} "
            "ORDER BY r.ligand_molecule_id ASC, r.receptor_molecule_id ASC, r.pose_rank ASC "
            f"LIMIT {max(1, int(limit))}"
        ),
    )
    return [
        {
            "ligand_id": int(row.get("ligand_id") or 0),
            "ligand_name": str(row.get("ligand_name") or ""),
            "receptor_id": int(row.get("receptor_id") or 0),
            "receptor_name": str(row.get("receptor_name") or ""),
            "pose_rank": int(row.get("pose_rank") or 1),
            "score": None if row.get("score") is None else float(row.get("score")),
            "ligand_efficiency": (
                None if row.get("ligand_efficiency") is None else float(row.get("ligand_efficiency"))
            ),
            "pose_payload_id": row.get("pose_payload_id"),
            "receptor_payload_id": row.get("receptor_payload_id"),
            "ligand_payload_id": row.get("ligand_payload_id"),
        }
        for row in rows
    ]

def max_pose_rank(project_db, *, receptor_ids: list[int]) -> int:
    """The deepest pose any of these receptors has: the off-target pose selector's range."""
    ids = ",".join(str(int(rid)) for rid in receptor_ids) or "-1"
    rows = db_rows(
        project_db,
        query=f"SELECT MAX(pose_rank) AS n FROM {TABLE_DOCKING_RESULTS} WHERE receptor_molecule_id IN ({ids})",
    )
    return int((rows[0].get("n") if rows else None) or 1)

def list_interactions(project_db, *, result_id: int) -> list[dict[str, Any]]:
    rows = db_rows(
        project_db,
        query=(
            "SELECT interaction_type, residue, residue_index, distance, geometry, created_at "
            f"FROM {TABLE_INTERACTION_RESULTS} "
            f"WHERE pose_payload_id = (SELECT pose_payload_id FROM {TABLE_DOCKING_RESULTS} WHERE id = {int(result_id)}) "
            "ORDER BY interaction_type ASC, residue_index ASC, distance ASC"
        ),
    )
    return [
        {
            "interaction_type": str(row.get("interaction_type") or ""),
            "residue": str(row.get("residue") or ""),
            "residue_index": int(row.get("residue_index") or 0),
            "distance": _float_or_none(row.get("distance")),
            "geometry": _decode_metadata(row.get("geometry")),
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]

def interaction_stats(project_db, *, result_ids: list[int] | None = None) -> dict[str, list[dict[str, Any]]]:
    ids = sorted({int(value) for value in (result_ids or []) if int(value) > 0})
    id_clause = ""
    if ids:
        id_clause = (f"AND pose_payload_id IN (SELECT pose_payload_id FROM {TABLE_DOCKING_RESULTS} "
                     f"WHERE id IN ({','.join(str(value) for value in ids)})) ")
    type_rows = db_rows(
        project_db,
        query=(
            "SELECT interaction_type AS label, "
            "COUNT(DISTINCT pose_payload_id) AS hit_count, "
            "COUNT(*) AS interaction_count "
            f"FROM {TABLE_INTERACTION_RESULTS} "
            "WHERE COALESCE(interaction_type, '') != 'none' "
            f"{id_clause}"
            "GROUP BY interaction_type "
            "ORDER BY hit_count DESC, interaction_count DESC, label ASC"
        ),
    )
    residue_rows = db_rows(
        project_db,
        query=(
            "SELECT "
            "CASE "
            "WHEN COALESCE(residue, '') != '' AND COALESCE(residue_index, 0) > 0 THEN residue || residue_index "
            "WHEN COALESCE(residue, '') != '' THEN residue "
            "ELSE CAST(residue_index AS TEXT) "
            "END AS label, "
            "COUNT(DISTINCT pose_payload_id) AS hit_count, "
            "COUNT(*) AS interaction_count "
            f"FROM {TABLE_INTERACTION_RESULTS} "
            "WHERE COALESCE(interaction_type, '') != 'none' "
            f"{id_clause}"
            "GROUP BY label "
            "ORDER BY hit_count DESC, interaction_count DESC, label ASC "
            "LIMIT 25"
        ),
    )
    return {
        "by_type": [
            {
                "label": str(row.get("label") or ""),
                "hit_count": int(row.get("hit_count") or 0),
                "interaction_count": int(row.get("interaction_count") or 0),
            }
            for row in type_rows
        ],
        "by_residue": [
            {
                "label": str(row.get("label") or ""),
                "hit_count": int(row.get("hit_count") or 0),
                "interaction_count": int(row.get("interaction_count") or 0),
            }
            for row in residue_rows
        ],
    }

def pivot_availability(project_db) -> dict[str, bool]:
    """What the Docking Results pivots have data for. A pivot is gated on being
    *populated*, never on whether a run happened: molecules and runs keep arriving, so
    "you have not redocked yet" has to stop being true the moment you have."""
    rows = db_rows(
        project_db,
        query=(
            "SELECT "
            f"(EXISTS(SELECT 1 FROM {TABLE_DOCKING_RESULTS} h JOIN {TABLE_DOCKING_RUNS} dr "
            "ON dr.id = h.docking_run_id WHERE dr.run_kind != 'redocking' LIMIT 1) "
            # A campaign's receptors are on screen from the moment they are scheduled, so the
            # pivot has something to show long before the first result row exists.
            f"OR EXISTS(SELECT 1 FROM {TABLE_SCREENING_TARGETS} LIMIT 1)) AS hits, "
            f"EXISTS(SELECT 1 FROM {TABLE_DOCKING_RESULTS} rd JOIN {TABLE_DOCKING_RUNS} dr "
            "ON dr.id = rd.docking_run_id WHERE dr.run_kind = 'redocking' LIMIT 1) AS redocked, "
            # MIN/MAX of the indexed receptor column rejects the common one-receptor case in
            # O(log N). Only projects that really have several receptors pay for grouping by
            # ligand to prove that at least one ligand is shared by two of them.
            f"CASE WHEN (SELECT COALESCE(MIN(receptor_molecule_id), -1) "
            f"FROM {TABLE_DOCKING_RESULTS}) = "
            f"(SELECT COALESCE(MAX(receptor_molecule_id), -1) "
            f"FROM {TABLE_DOCKING_RESULTS}) THEN 0 ELSE "
            f"EXISTS(SELECT 1 FROM {TABLE_DOCKING_RESULTS} ot "
            "GROUP BY ot.ligand_molecule_id "
            "HAVING MIN(ot.receptor_molecule_id) != MAX(ot.receptor_molecule_id) "
            "LIMIT 1) END AS shared_ligands"
        ),
    )
    row = rows[0] if rows else {}
    return {
        "hits": int(row.get("hits") or 0) > 0,
        "redocking": int(row.get("redocked") or 0) > 0,
        "offtarget": int(row.get("shared_ligands") or 0) > 0,
    }

def count_docked_pairs(
    project_db,
    *,
    engine: str = "",
    receptor_ids: list[int] | None = None,
    protocol_hash: str | None = None,
    run_kind: str | None = "screening",
) -> int:
    """How many receptor-ligand pairs already have a result. A COUNT, not the pair set:
    the run's skip-existing guard materializes the pairs because it has to compare them
    one by one; a preview only needs the number."""
    runs = ["1=1"]  # the docking runs that count
    normalized_engine = str(engine or "").strip().lower().replace("'", "''")
    if normalized_engine:
        runs.append(f"engine = '{normalized_engine}'")
    if run_kind:
        runs.append("run_kind = '{}'".format(str(run_kind).strip().replace("'", "''")))
    if protocol_hash:
        runs.append("protocol_hash = '{}'".format(str(protocol_hash).replace("'", "''")))
    clauses = [f"docking_run_id IN (SELECT id FROM {TABLE_DOCKING_RUNS} WHERE {' AND '.join(runs)})"]
    if receptor_ids is not None:
        ids = sorted({int(value) for value in receptor_ids if int(value) > 0})
        # An explicit selection that resolves to nothing means "no receptors", not "all of
        # them": IN (NULL) is never true, so the count is 0 instead of the whole project.
        clauses.append(f"receptor_molecule_id IN ({','.join(str(value) for value in ids) or 'NULL'})")
    rows = db_rows(
        project_db,
        query=(
            "SELECT COUNT(*) AS pair_count FROM ("
            "SELECT DISTINCT receptor_molecule_id, ligand_molecule_id "
            f"FROM {TABLE_DOCKING_RESULTS} WHERE {' AND '.join(clauses)})"
        ),
    )
    return int((rows[0] if rows else {}).get("pair_count") or 0)

def list_receptor_result_summaries(project_db) -> list[ReceptorDockingSummary]:
    rows = db_rows(
        project_db,
        query=(
            "SELECT "
            "r.receptor_molecule_id AS receptor_id, "
            "COALESCE(rec.name, '') AS receptor_name, "
            # Count docked LIGANDS, not pose rows (each pair stores several poses).
            # Failed pairs are stored with score IS NULL.
            "COUNT(DISTINCT r.ligand_molecule_id) AS total_results, "
            "COUNT(DISTINCT CASE WHEN r.score IS NOT NULL THEN r.ligand_molecule_id END) AS completed_results, "
            "(COUNT(DISTINCT r.ligand_molecule_id) "
            "- COUNT(DISTINCT CASE WHEN r.score IS NOT NULL THEN r.ligand_molecule_id END)) AS failed_results, "
            "MIN(r.score) AS best_score, "
            "AVG(r.score) AS avg_score, "
            # The screening's ligand universe: how many ligands were docked against ANY
            # receptor. Compared against this receptor's own count it shows what's missing.
            f"(SELECT COUNT(DISTINCT ligand_molecule_id) FROM {TABLE_DOCKING_RESULTS}) AS expected_ligands "
            f"FROM {TABLE_DOCKING_RESULTS} r "
            f"LEFT JOIN {TABLE_MOLECULES} rec ON rec.id = r.receptor_molecule_id "
            "GROUP BY r.receptor_molecule_id, rec.name "
            "ORDER BY best_score ASC, total_results DESC, receptor_id ASC"
        ),
    )
    return [
        ReceptorDockingSummary(
            receptor_id=int(row.get("receptor_id") or 0),
            receptor_name=str(row.get("receptor_name") or ""),
            total_results=int(row.get("total_results") or 0),
            completed_results=int(row.get("completed_results") or 0),
            failed_results=int(row.get("failed_results") or 0),
            expected_ligands=int(row.get("expected_ligands") or 0),
            best_score=None if row.get("best_score") is None else float(row.get("best_score")),
            avg_score=None if row.get("avg_score") is None else float(row.get("avg_score")),
        )
        for row in rows
    ]


__all__ = [
    "count_docked_pairs",
    "docking_run_id",
    "runs_where",
    "supersede_results",
    "docked_ligands_spec",
    "existing_result_pairs",
    "get_docking_results_stats",
    "get_hit",
    "interaction_stats",
    "molecule_scope_spec",
    "molecule_set_spec",
    "prepared_molecules_spec",
    "get_receptor_metadata_json",
    "get_molecule_rows_by_ids",
    "list_entity_rows",
    "list_complex_rows",
    "list_docking_result_rows",
    "list_interactions",
    "list_ligand_result_summaries",
    "list_offtarget_rows",
    "list_receptor_ids_in_set",
    "list_receptor_result_summaries",
    "list_result_protocols",
    "list_results",
    "list_top_hits",
    "persist_receptor_grid",
    "pivot_availability",
    "project_db_path",
    "resolve_docking_output_dir",
    "update_receptor_metadata_json",
]
