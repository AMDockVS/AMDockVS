"""SQL-backed table configs for the docking results view.

These are declarative specs, not widgets: every computed column here is a SQL
expression the SmartTable adapter compiles. They live outside ``ui/`` so the
widgets stay free of SQLAlchemy (see test_ui_api_boundary), and they import
``table_config`` directly rather than the ``ms_table`` package, which pulls Qt.
"""
from __future__ import annotations

from functools import partial

from sqlalchemy import String, cast, distinct, exists, func, literal, or_, select
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
_RECEPTOR_HAS_RESULTS = exists(
    select(DockingResultRecord.id).where(
        DockingResultRecord.receptor_molecule_id == MoleculeRecord.id,
        _run_kind() != "redocking",
    )
)


def _docked_of(receptor_id, protocol_hash):
    """Distinct ligands with a result for this receptor from any run of this program.

    By program, not by run: a later run skips the pairs an earlier one already docked."""
    result, run = aliased(DockingResultRecord), aliased(DockingRun)
    return (
        select(func.count(distinct(result.ligand_molecule_id)))
        .where(
            result.receptor_molecule_id == receptor_id,
            result.docking_run_id.in_(
                select(run.id).where(run.protocol_hash == protocol_hash, run.run_kind != "redocking")
            ),
        )
        .scalar_subquery()
    )


def _latest_plans(protocol_hash: str):
    """Ids of this receptor's latest plan row of each program, or of the one program given.

    The plan is one row per (run, receptor) written when the run is submitted and counted up
    by the parent — so "how many ligands, how many done" is an indexed lookup instead of
    counting distinct ligands in `docking_results` for every receptor on screen.
    """
    target, run = aliased(ScreeningTarget), aliased(DockingRun)
    statement = (
        select(func.max(target.id))
        .select_from(target)
        .join(run, run.id == target.docking_run_id)
        .where(target.receptor_molecule_id == MoleculeRecord.id, run.run_kind != "redocking")
    )
    if protocol_hash:
        statement = statement.where(run.protocol_hash == protocol_hash)
    return statement.group_by(run.protocol_hash).correlate(MoleculeRecord)


def _over_plans(protocol_hash: str, value, aggregate=func.sum):
    """`aggregate(value(plan, run))` over the latest plans; NULL when the receptor has none."""
    target, run = aliased(ScreeningTarget), aliased(DockingRun)
    return (
        select(aggregate(value(target, run)))
        .select_from(target)
        .join(run, run.id == target.docking_run_id)
        .where(target.id.in_(_latest_plans(protocol_hash)))
        .correlate(MoleculeRecord)
        .scalar_subquery()
    )


def _plan_docked(target, run):
    # Campaigns count `ligands_done` (their non-hits leave no row); row-mode runs plan with 0
    # and write a row per docked pair, so the larger of the two is right for both.
    return func.max(target.ligands_done, _docked_of(target.receptor_molecule_id, run.protocol_hash))


_STOPPED = (TargetState.FAILED, TargetState.CANCELED)


def _plan_state(protocol_hash: str):
    """The latest plan's state; over several programs a stopped one wins, so the bar is red."""
    target, run = aliased(ScreeningTarget), aliased(DockingRun)
    return (
        select(target.state)
        .where(target.id.in_(_latest_plans(protocol_hash)))
        .order_by(target.state.in_(_STOPPED).desc(), target.id.desc())
        .limit(1)
        .correlate(MoleculeRecord)
        .scalar_subquery()
    )


def _results_of(protocol_hash: str):
    """Distinct ligands with a result for this receptor (of one program)."""
    if protocol_hash:
        return _docked_of(MoleculeRecord.id, protocol_hash).correlate(MoleculeRecord)
    return _RECEPTOR_DOCKED


def receptor_progress_columns(protocol_hash: str = "") -> dict:
    """The receptor pane's Ligands/Docked/state of one program, or summed over every program.

    Each program counts its own latest plan, so "All" is the sum of what each program planned
    and docked and a finished program alone reads 100 %.
    """
    # ponytail: plan first, results second. A run knows its own totals; a project docked the
    # old way has no plan rows, and there the result counts are what it already paid.
    expected = (
        select(func.count(distinct(DockingResultRecord.ligand_molecule_id)))
        .where(_run_kind() != "redocking",
               *([_run_field("protocol_hash") == protocol_hash] if protocol_hash else []))
        .scalar_subquery()
    )
    columns = {
        "ligands": func.coalesce(_over_plans(protocol_hash, lambda target, _run: target.ligands_total), expected),
        "progress": func.coalesce(_over_plans(protocol_hash, _plan_docked), _results_of(protocol_hash)),
        "run_state": _plan_state(protocol_hash),
        "per_program": literal(None, String),
    }
    if not protocol_hash:
        columns["per_program"] = _over_plans("", lambda target, run: (
            run.protocol_label + "\t" + cast(_plan_docked(target, run), String)
            + "\t" + cast(target.ligands_total, String)
        ), aggregate=lambda line: func.group_concat(line, "\n"))
    return columns


def receptor_progress(project_db, receptor_ids=(), protocol_hash: str = "") -> dict[int, dict]:
    """The same Ligands/Docked/state cells, to patch a live table in place: of these receptors,
    or of every receptor a run planned (how a newly planned one is noticed)."""
    columns = receptor_progress_columns(protocol_hash)
    statement = select(MoleculeRecord.id, *(expr.label(name) for name, expr in columns.items())).where(
        MoleculeRecord.id.in_([int(value) for value in receptor_ids]) if receptor_ids else _RECEPTOR_SCHEDULED)
    with project_db.get_session() as session:
        return {int(row.id): {name: getattr(row, name) for name in columns}
                for row in session.execute(statement)}


_RECEPTOR_SCHEDULED = exists(
    select(ScreeningTarget.id).where(ScreeningTarget.receptor_molecule_id == MoleculeRecord.id)
)


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
    text = f"{docked:,} of {ligands:,} ligands docked, {max(ligands - docked, 0):,} pending"
    each = [line.split("\t") for line in str(row.get("per_program") or "").splitlines()]
    if len(each) < 2:
        return text
    return text + "\n" + "".join(f"\n{label}: {int(done):,} of {int(total):,}" for label, done, total in sorted(each))


def _progress_tone(row: dict) -> str:
    """Red when this receptor's latest run stopped before the end, failed or canceled."""
    return "red" if row.get("run_state") in _STOPPED else "green"


def results_receptor_config(protocol_hash: str = "") -> TableConfig:
    """`protocol_hash` scopes Ligands/Docked to one program; swap them with
    `receptor_progress_columns` when the program filter changes."""
    progress = receptor_progress_columns(protocol_hash)
    return TableConfig(
        model_class=MoleculeRecord,
        columns=[
            ColumnDef("name", label="Receptor", width=120),
            ColumnDef("ligands", label="Ligands", kind=ColumnKind.INTEGER, align=AlignHint.RIGHT,
                      expr=progress["ligands"], width=75, sortable=False),
            ColumnDef("progress", label="Docked", kind=ColumnKind.PROGRESS, progress_max="ligands",
                      expr=progress["progress"], width=160, sortable=False, filterable=False,
                      tooltip=_progress_tooltip, progress_tone=_progress_tone),
            ColumnDef("run_state", visible=False, expr=progress["run_state"], sortable=False),
            ColumnDef("per_program", visible=False, expr=progress["per_program"], sortable=False),
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
            # Shown only under "All programs": a ligand docked by two programs is two rows.
            ColumnDef("protocol_label", label="Program", visible=False, expr=_run_field("protocol_label"),
                      width=110),
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
        multi_select=True,  # several rows side by side in PyMOL
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
        multi_select=True,  # several rows side by side in PyMOL
        show_filters=False,
        show_search=False,
        empty_message="No poses for this ligand",
    )
