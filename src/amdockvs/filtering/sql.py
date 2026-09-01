"""SQL fast-path for small-molecule filtering.

The id-based FilterEngine streams and model-validates every candidate row through Python — fine for
a few thousand molecules, a non-starter for millions. Since every filter field is a persisted
MoleculeRecord column, the whole thing is a WHERE clause: counts are ``COUNT(*)`` and applying is a
single ``UPDATE ... WHERE``, so nothing is ever materialized in Python.

ponytail: SQLite-only — PAINS/Ro5 use ``json_array_length`` on the JSON list columns. Revisit if the
project DB ever moves off SQLite.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, false, func, not_
from sqlalchemy import update as sql_update
from sqlmodel import select

from ms_flow.core.database import subquery_clause

from amdockvs.chemistry.filtering import (
    SmallMoleculeFilterCriteria,
    SmallMoleculeFilterField as F,
    SmallMoleculeFilterOperator as OP,
)
from amdockvs.models.molecules import MoleculeRecord as M
from amdockvs.scopes import molecule_set_spec

# "Has computed descriptors" proxy: the importer sets n_atoms>0 only when it computed properties, so
# molecules imported without descriptors (n_atoms 0) are not evaluable — same skip the Python path did.
EVALUABLE = M.n_atoms > 0

_NUMERIC_OPS = {
    OP.LT: lambda c, v: c < v,
    OP.LTE: lambda c, v: c <= v,
    OP.GT: lambda c, v: c > v,
    OP.GTE: lambda c, v: c >= v,
    OP.EQ: lambda c, v: c == v,
}

_SCOPE_OPS = {
    "eq": lambda c, v: c == v,
    "gt": lambda c, v: c > v,
    "gte": lambda c, v: c >= v,
    "lt": lambda c, v: c < v,
    "lte": lambda c, v: c <= v,
    "in": lambda c, v: c.in_(list(v) if v else [None]),
}


def _json_len(col):
    return func.coalesce(func.json_array_length(col), 0)


def criteria_conditions(criteria: SmallMoleculeFilterCriteria) -> list:
    """SQLAlchemy conditions for the filter rules (ANDed). Numeric NULLs are coalesced to 0 so the
    result matches the Python predicate, which treats a missing descriptor as 0."""
    conds: list[Any] = []
    for rule in criteria.rules:
        if rule.field in (F.PAINS_MATCHES, F.RO5_VIOLATIONS):
            col = M.pains_matches if rule.field == F.PAINS_MATCHES else M.ro5_violations
            if rule.operator == OP.IS_EMPTY:
                conds.append(_json_len(col) == 0)
            elif rule.operator == OP.HAS_ANY:
                conds.append(_json_len(col) > 0)
            continue
        col = getattr(M, rule.field, None)
        make = _NUMERIC_OPS.get(rule.operator)
        if col is None or make is None or rule.value is None:
            continue
        conds.append(make(func.coalesce(col, 0), rule.value))
    return conds


def scope_conditions(project_db, scope) -> list:
    """Translate the filter UI's MoleculeScope (source set + molecule_type/excluded/in_set/usage_class)
    into MoleculeRecord conditions. Raises on any unexpected key so a scope is never silently widened."""
    conds: list[Any] = []
    if getattr(scope, "source_set_id", None) is not None:
        conds.append(M.id.in_(subquery_clause(molecule_set_spec(int(scope.source_set_id)), null_safe=False)))
    for key, value in dict(getattr(scope, "filters", None) or {}).items():
        field, _, op = str(key).partition("__")
        col = getattr(M, field, None)
        make = _SCOPE_OPS.get(op or "eq")
        if col is None or make is None:
            raise ValueError(f"SQL filter can't translate scope key '{key}'.")
        conds.append(make(col, value))
    return conds


def _count(session, conds) -> int:
    return int(session.exec(select(func.count()).select_from(M).where(*conds)).one() or 0)


def counts(project_db, scope_conds: list, criteria: SmallMoleculeFilterCriteria) -> dict[str, int]:
    crit = criteria_conditions(criteria)
    with project_db.get_session() as session:
        scanned = _count(session, scope_conds)
        evaluable = _count(session, [*scope_conds, EVALUABLE])
        matched = _count(session, [*scope_conds, EVALUABLE, *crit])
    return {
        "scanned": scanned,
        "evaluable": evaluable,
        "matched": matched,
        "skipped": scanned - evaluable,
        "nonmatched": evaluable - matched,
    }


def apply_state(
    project_db,
    scope_conds: list,
    criteria: SmallMoleculeFilterCriteria,
    *,
    activate_matches: bool,
    exclude_nonmatches: bool,
    reason: str,
) -> tuple[int, int]:
    """One UPDATE per requested direction. Enrich excludes the evaluable rows that fail; Recover
    re-includes the evaluable rows that pass. Never both — the caller picks one."""
    crit = criteria_conditions(criteria)
    fail = not_(and_(*crit)) if crit else false()  # at least one rule fails; no rules → nothing fails
    activated = excluded = 0
    with project_db.get_session() as session:
        if activate_matches:
            result = session.exec(
                sql_update(M).where(*scope_conds, EVALUABLE, *crit).values(excluded=False, exclusion_reason="")
            )
            activated = int(getattr(result, "rowcount", 0) or 0)
        if exclude_nonmatches:
            result = session.exec(
                sql_update(M).where(*scope_conds, EVALUABLE, fail).values(excluded=True, exclusion_reason=str(reason or ""))
            )
            excluded = int(getattr(result, "rowcount", 0) or 0)
        session.commit()
    return activated, excluded


def matched_ids(project_db, scope_conds: list, criteria: SmallMoleculeFilterCriteria) -> list[int]:
    crit = criteria_conditions(criteria)
    with project_db.get_session() as session:
        rows = session.exec(select(M.id).where(*scope_conds, EVALUABLE, *crit)).all()
    return [int(r) for r in rows if r is not None]


__all__ = [
    "EVALUABLE",
    "criteria_conditions",
    "scope_conditions",
    "counts",
    "apply_state",
    "matched_ids",
]
