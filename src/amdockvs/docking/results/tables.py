"""SQL-backed table configs for the docking results view.

These are declarative specs, not widgets: every computed column here is a SQL
expression the SmartTable adapter compiles. They live outside ``ui/`` so the
widgets stay free of SQLAlchemy (see test_ui_api_boundary), and they import
``table_config`` directly rather than the ``ms_table`` package, which pulls Qt.
"""
from __future__ import annotations

from functools import partial

from sqlalchemy import distinct, exists, func, or_, select
from sqlalchemy.orm import aliased

from ms_components.ms_table.table_config import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    FilterOperator,
    FilterSpec,
    SortSpec,
    TableConfig,
    TableLoadMode,
)

from amdockvs.core.vocab import TargetState
from amdockvs.models import DockingResultRecord, DockingRun, MoleculeRecord, ScreeningTarget


def fmt_float(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _run_field(name: str, model=DockingResultRecord):
    """A column of the row's docking run (`docking_runs`)."""
    return (
        select(getattr(DockingRun, name))
        .where(DockingRun.id == model.docking_run_id)
        .correlate(model)
        .scalar_subquery()
    )


def _run_kind(model=DockingResultRecord):
    return _run_field("run_kind", model)


def _molecule_name(molecule_id):
    molecule = aliased(MoleculeRecord)
    return (
        select(molecule.name)
        .where(molecule.id == molecule_id)
        .correlate(DockingResultRecord)
        .scalar_subquery()
    )


def _receptor_result_count(*, completed: bool = False):
    result = aliased(DockingResultRecord)
    clauses = [
        result.receptor_molecule_id == MoleculeRecord.id,
        _run_kind(result) != "redocking",
    ]
    if completed:
        clauses.append(result.score.is_not(None))
    return (
        select(func.count(distinct(result.ligand_molecule_id)))
        .where(*clauses)
        .correlate(MoleculeRecord)
        .scalar_subquery()
    )


_RECEPTOR_DOCKED = _receptor_result_count()
_RECEPTOR_DONE = _receptor_result_count(completed=True)
_EXPECTED_LIGANDS = (
    select(func.count(distinct(DockingResultRecord.ligand_molecule_id)))
    .where(_run_kind() != "redocking")
    .scalar_subquery()
)
_RECEPTOR_HAS_RESULTS = exists(
    select(DockingResultRecord.id).where(
        DockingResultRecord.receptor_molecule_id == MoleculeRecord.id,
        _run_kind() != "redocking",
    )
)


def _plan_field(column: str):
    """A column of this receptor's latest campaign plan row, or NULL if it never had one.

    The plan is one row per (run, receptor) written when the run is submitted and counted up
    by the parent — so "how many ligands, how many done" is a single indexed lookup instead
    of counting distinct ligands in `docking_results` for every receptor on screen.
    """
    target = aliased(ScreeningTarget)
    return (
        select(getattr(target, column))
        .where(target.receptor_molecule_id == MoleculeRecord.id)
        .order_by(target.id.desc())
        .limit(1)
        .correlate(MoleculeRecord)
        .scalar_subquery()
    )


_RECEPTOR_SCHEDULED = exists(
    select(ScreeningTarget.id).where(ScreeningTarget.receptor_molecule_id == MoleculeRecord.id)
)
# ponytail: plan first, results second. A campaign knows its own totals; a project docked the
# old way has no plan rows, and there the correlated counts above are what it already paid.
_LIGANDS = func.coalesce(_plan_field("ligands_total"), _EXPECTED_LIGANDS)
# Campaigns count `ligands_done` (their non-hits leave no row); row-mode runs plan with 0 and
# write a row per docked pair, so the larger of the two is right for both.
# ponytail: pays the per-receptor result count on campaign rows too; cheap while hits are capped.
_DOCKED = func.max(func.coalesce(_plan_field("ligands_done"), 0), _RECEPTOR_DOCKED)


def _pose_count_expr():
    pose = aliased(DockingResultRecord)
    return (
        select(func.count(pose.id))
        .where(
            pose.receptor_molecule_id == DockingResultRecord.receptor_molecule_id,
            pose.ligand_molecule_id == DockingResultRecord.ligand_molecule_id,
            pose.docking_run_id == DockingResultRecord.docking_run_id,
        )
        .correlate(DockingResultRecord)
        .scalar_subquery()
    )


def _progress_tooltip(row: dict) -> str:
    ligands = int(row.get("ligands") or 0)
    docked = int(row.get("progress") or 0)
    return f"{docked:,} of {ligands:,} ligands docked, {max(ligands - docked, 0):,} pending"


def _progress_tone(row: dict) -> str:
    """Red when this receptor's latest run stopped before the end, failed or canceled."""
    return "red" if row.get("run_state") in (TargetState.FAILED, TargetState.CANCELED) else "green"


def results_receptor_config() -> TableConfig:
    return TableConfig(
        model_class=MoleculeRecord,
        columns=[
            ColumnDef("name", label="Receptor", width=120),
            ColumnDef("ligands", label="Ligands", kind=ColumnKind.INTEGER, align=AlignHint.RIGHT,
                      expr=_LIGANDS, width=75, sortable=False),
            ColumnDef("progress", label="Docked", kind=ColumnKind.PROGRESS, progress_max="ligands",
                      expr=_DOCKED, width=160, sortable=False, filterable=False, tooltip=_progress_tooltip,
                      progress_tone=_progress_tone),
            ColumnDef("run_state", visible=False, expr=_plan_field("state"), sortable=False),
            ColumnDef("scored", label="Scored", visible=False, kind=ColumnKind.INTEGER,
                      align=AlignHint.RIGHT, expr=_RECEPTOR_DONE, width=65, sortable=False),
            # A receptor a run scheduled belongs here from the moment it is scheduled: in a
            # campaign its results land at the end, and until then it would be invisible.
            ColumnDef("in_play", visible=False,
                      expr=or_(_RECEPTOR_HAS_RESULTS, _RECEPTOR_SCHEDULED)),
        ],
        default_filters=[
            FilterSpec("is_receptor", FilterOperator.EQ, True),
            FilterSpec("in_play", FilterOperator.EQ, True),
        ],
        default_sort=[],
        # page_size=20,
        load_mode=TableLoadMode.INFINITE,
        infinite_cache_pages=2,
        multi_select=False,
        show_filters=False,
        show_search=False,
        show_record_count=False,
        empty_message="No receptor has been docked or scheduled yet",
    )


def results_ligand_config() -> TableConfig:
    return TableConfig(
        model_class=DockingResultRecord,
        columns=[
            ColumnDef("ligand_name", label="Ligand", width=120,
                      expr=_molecule_name(DockingResultRecord.ligand_molecule_id)),
            ColumnDef("score", label="Best", kind=ColumnKind.NUMBER, align=AlignHint.RIGHT,
                      formatter=fmt_float, width=65),
            ColumnDef("ligand_efficiency", label="LE", kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, formatter=fmt_float, width=55),
            ColumnDef("lipophilic_efficiency", label="LLE", kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, expr=DockingResultRecord.lle, formatter=fmt_float,
                      width=60),
            ColumnDef("predicted_pki", label="pKi", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, formatter=fmt_float, width=65),
            ColumnDef("fit_quality", label="FQ", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, formatter=fmt_float, width=65),
            ColumnDef("bei", label="BEI", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, formatter=fmt_float, width=65),
            ColumnDef("sei", label="SEI", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, formatter=fmt_float, width=65),
            ColumnDef("pose_count", label="Poses", kind=ColumnKind.INTEGER, align=AlignHint.RIGHT,
                      expr=_pose_count_expr(), width=65),
            ColumnDef("protocol_label", label="Protocol", visible=False, expr=_run_field("protocol_label")),
            ColumnDef("protocol_hash", visible=False, expr=_run_field("protocol_hash")),
            ColumnDef("run_id", visible=False, expr=_run_field("run_id")),
            ColumnDef("run_kind", visible=False, expr=_run_kind()),
            ColumnDef("score_sort", visible=False,
                      expr=func.coalesce(DockingResultRecord.score, 1.0e100)),
        ],
        default_filters=[
            FilterSpec("receptor_molecule_id", FilterOperator.EQ, -1),
            FilterSpec("pose_rank", FilterOperator.EQ, 1),
            FilterSpec("run_kind", FilterOperator.NEQ, "redocking"),
        ],
        default_sort=[SortSpec("score_sort")],
        page_size=100,
        load_mode=TableLoadMode.INFINITE,
        infinite_cache_pages=2,
        multi_select=False,
        show_filters=False,
        show_search=False,
        empty_message="No ligands for this receptor and filter scope",
    )


def _offtarget_score(receptor_id: int, pose_rank: int, protocol_hash: str):
    """The score of this row's ligand on another receptor: that receptor's pose `pose_rank`
    from a run of the same protocol. One `idx_dr_pair` lookup per cell on screen.

    `pose_rank + 0` keeps SQLite on `idx_dr_pair`: on a bare `pose_rank` it prefers the
    (receptor, rank, score) index the outer sort uses, which scans the whole receptor per row
    (O(N²), 100k ligands never finish). MIN, not ORDER BY score LIMIT 1: an ascending sort
    returns a NULL first.
    """
    other = aliased(DockingResultRecord)
    run = aliased(DockingRun)
    return (
        select(func.min(other.score))
        .join(run, run.id == other.docking_run_id)
        .where(
            other.receptor_molecule_id == int(receptor_id),
            other.ligand_molecule_id == DockingResultRecord.ligand_molecule_id,
            other.pose_rank + 0 == int(pose_rank),
            run.protocol_hash == protocol_hash,
            run.run_kind != "redocking",
        )
        .correlate(DockingResultRecord)
        .scalar_subquery()
    )


def offtarget_config(
    *,
    target: tuple[int, str],
    off_targets: list[tuple[int, str]],
    ranks: dict[int, int],
    protocol_hash: str,
    tooltip=None,
) -> TableConfig:
    """One row per ligand docked on the target and on at least one off-target.

    The rows are fixed: the target's P1 results of the ligands some off-target was docked
    with (the off-target stage only runs the target's top-N, so this is that N, not the
    whole library). A column's pose only changes the values: `target_score` is the target at
    its selected pose, each `ddg_<id>` = off-target score - target score at the selected
    poses, all SQL, so they sort like any other column. The off-target's own score is
    `ddg + target_score`. `tooltip(receptor_id, row_data)` is shared.
    """
    target_id, target_name = target
    target_rank = int(ranks.get(target_id, 1))
    target_score = (DockingResultRecord.score if target_rank == 1
                    else _offtarget_score(target_id, target_rank, protocol_hash))

    def tip(receptor_id):
        return None if tooltip is None else partial(tooltip, receptor_id)

    columns = [
        ColumnDef("ligand_name", label="Ligand", width=140,
                  expr=_molecule_name(DockingResultRecord.ligand_molecule_id)),
        ColumnDef("target_score", label=f"{target_name} (T)", kind=ColumnKind.NUMBER, width=110,
                  expr=target_score, tooltip=tip(target_id)),
    ]
    for receptor_id, name in off_targets:
        score = _offtarget_score(receptor_id, ranks.get(receptor_id, 1), protocol_hash)
        columns.append(ColumnDef(f"ddg_{int(receptor_id)}", label=name, kind=ColumnKind.NUMBER,
                                 width=110, expr=score - target_score,
                                 tooltip=tip(receptor_id)))
    filters = [
        FilterSpec("receptor_molecule_id", FilterOperator.EQ, int(target_id)),
        FilterSpec("pose_rank", FilterOperator.EQ, 1),
        FilterSpec("score", FilterOperator.NOT_NULL, None),
        FilterSpec("protocol_hash", FilterOperator.EQ, protocol_hash),
        FilterSpec("run_kind", FilterOperator.NEQ, "redocking"),
    ]
    if off_targets:
        other, run = aliased(DockingResultRecord), aliased(DockingRun)
        docked_elsewhere = (
            select(other.ligand_molecule_id)
            .join(run, run.id == other.docking_run_id)
            .where(other.receptor_molecule_id.in_([int(rid) for rid, _ in off_targets]),
                   run.protocol_hash == protocol_hash, run.run_kind != "redocking")
        )
        filters.append(FilterSpec("ligand_molecule_id", FilterOperator.IN, docked_elsewhere,
                                  label="docked on an off-target"))
    columns += [
        ColumnDef("protocol_hash", visible=False, expr=_run_field("protocol_hash")),
        ColumnDef("run_kind", visible=False, expr=_run_kind()),
    ]
    return TableConfig(
        model_class=DockingResultRecord,
        columns=columns,
        default_filters=filters,
        default_sort=[SortSpec("target_score")],
        page_size=100,
        load_mode=TableLoadMode.INFINITE,
        infinite_cache_pages=2,
        multi_select=False,
        show_filters=False,
        show_search=False,
        row_height=32,
        empty_message="No ligand docked on the target and an off-target with this protocol",
    )


def results_pose_config() -> TableConfig:
    return TableConfig(
        model_class=DockingResultRecord,
        columns=[
            ColumnDef("pose_rank", label="Pose", kind=ColumnKind.INTEGER, align=AlignHint.RIGHT, sortable=False,
                      width=65),
            ColumnDef("score", label="Score", kind=ColumnKind.NUMBER, align=AlignHint.RIGHT,
                      formatter=fmt_float, sortable=False, width=75),
            ColumnDef("rmsd_vs_reference", label="RMSD ref", kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, formatter=fmt_float, width=65),
            ColumnDef("ligand_efficiency", label="LE", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, formatter=fmt_float, width=65),
            ColumnDef("predicted_pki", label="pKi", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, formatter=fmt_float, width=65),
            ColumnDef("protocol_hash", visible=False, expr=_run_field("protocol_hash")),
            ColumnDef("run_id", visible=False, expr=_run_field("run_id")),
            ColumnDef("run_kind", visible=False, expr=_run_kind()),
        ],
        # default_filters=[
        #     FilterSpec("receptor_molecule_id", FilterOperator.EQ, -1),
        #     FilterSpec("ligand_molecule_id", FilterOperator.EQ, -1),
        #     FilterSpec("run_kind", FilterOperator.NEQ, "redocking"),
        # ],
        default_sort=[SortSpec("pose_rank")],
        page_size=20,
        load_mode=TableLoadMode.INFINITE,
        infinite_cache_pages=2,
        multi_select=False,
        show_filters=False,
        show_search=False,
        empty_message="No poses for this ligand",
    )
