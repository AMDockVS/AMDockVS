"""Consultas de la herramienta Moleculas.

These statistics feed the headers of the ligand and receptor tables. They are aggregated in
SQL: the previous version read the whole table and counted in Python, which over a library of
a million molecules means the full table in RAM just to show 4 numbers.
"""
from __future__ import annotations

from pathlib import Path

from ms_flow.query import db_rows

from amdockvs.constants import TABLE_BINDING_SITES, TABLE_ENGINES, TABLE_MOLECULES
from amdockvs.summaries import (
    LigandTableStatsSummary,
    NumericRangeSummary,
    ReceptorTableStatsSummary,
    SourceFileCountSummary,
    ValueCountSummary,
)

# A receptor's status is the highest rung it reached: box defined > prepared for some engine >
# 3D structure imported. It is resolved with EXISTS, not by hydrating every row.
_RECEPTOR_STATUS_SQL = (
    "CASE "
    f"WHEN EXISTS (SELECT 1 FROM {TABLE_BINDING_SITES} b WHERE b.molecule_id = m.id "
    "AND b.center_x IS NOT NULL AND b.size_x IS NOT NULL) THEN 'grid_ready' "
    f"WHEN EXISTS (SELECT 1 FROM {TABLE_ENGINES} e WHERE e.molecule_id = m.id "
    "AND e.role_type = 'receptor' AND e.is_ready = 1) THEN 'prepared' "
    "WHEN m.has_3d = 1 THEN 'imported' "
    "ELSE 'unknown' END"
)
_LIGAND_STATUS_SQL = "CASE WHEN m.excluded = 1 THEN 'excluded' ELSE 'active' END"


def _grouped_counts(project_db, *, role_column: str, expression: str) -> list[ValueCountSummary]:
    rows = db_rows(
        project_db,
        query=(
            f"SELECT {expression} AS value, COUNT(*) AS count "
            f"FROM {TABLE_MOLECULES} m WHERE m.{role_column} = 1 "
            "GROUP BY value ORDER BY count DESC, value ASC"
        ),
    )
    return [ValueCountSummary(value=str(row.get("value") or ""), count=int(row.get("count") or 0)) for row in rows]


def _by_source_file(project_db, *, role_column: str) -> list[SourceFileCountSummary]:
    rows = db_rows(
        project_db,
        query=(
            "SELECT m.source AS source_value, COUNT(*) AS count, "
            "MIN(m.source_index) AS min_source_index, MAX(m.source_index) AS max_source_index "
            f"FROM {TABLE_MOLECULES} m WHERE m.{role_column} = 1 "
            "GROUP BY m.source ORDER BY count DESC, source_value ASC"
        ),
    )
    return [
        SourceFileCountSummary(
            source_file=Path(str(row.get("source_value") or "")).expanduser().resolve(),
            count=int(row.get("count") or 0),
            min_source_index=int(row.get("min_source_index") or 0),
            max_source_index=int(row.get("max_source_index") or 0),
        )
        for row in rows
    ]


def _totals_and_atoms(project_db, *, role_column: str) -> tuple[int, NumericRangeSummary]:
    rows = db_rows(
        project_db,
        query=(
            "SELECT COUNT(*) AS total, MIN(m.n_atoms) AS min_atoms, "
            "AVG(m.n_atoms) AS avg_atoms, MAX(m.n_atoms) AS max_atoms "
            f"FROM {TABLE_MOLECULES} m WHERE m.{role_column} = 1"
        ),
    )
    row = rows[0] if rows else {}
    return int(row.get("total") or 0), NumericRangeSummary(
        min=int(row.get("min_atoms") or 0),
        avg=float(row.get("avg_atoms") or 0.0),
        max=int(row.get("max_atoms") or 0),
    )


def ligand_table_stats(project_db) -> LigandTableStatsSummary:
    total, atoms = _totals_and_atoms(project_db, role_column="is_ligand")
    return LigandTableStatsSummary(
        total_ligands=total,
        by_status=_grouped_counts(project_db, role_column="is_ligand", expression=_LIGAND_STATUS_SQL),
        by_input_format=_grouped_counts(project_db, role_column="is_ligand", expression="m.input_format"),
        by_source_file=_by_source_file(project_db, role_column="is_ligand"),
        atoms=atoms,
    )


def receptor_table_stats(project_db) -> ReceptorTableStatsSummary:
    total, atoms = _totals_and_atoms(project_db, role_column="is_receptor")
    return ReceptorTableStatsSummary(
        total_receptors=total,
        by_status=_grouped_counts(project_db, role_column="is_receptor", expression=_RECEPTOR_STATUS_SQL),
        by_input_format=_grouped_counts(project_db, role_column="is_receptor", expression="m.input_format"),
        by_source_file=_by_source_file(project_db, role_column="is_receptor"),
        atoms=atoms,
    )


__all__ = ["ligand_table_stats", "receptor_table_stats"]
