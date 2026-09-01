"""SQL-backed table configs for the docking results view.

These are declarative specs, not widgets: every computed column here is a SQL
expression the SmartTable adapter compiles. They live outside ``ui/`` so the
widgets stay free of SQLAlchemy (see test_ui_api_boundary), and they import
``table_config`` directly rather than the ``ms_table`` package, which pulls Qt.
"""
from __future__ import annotations

from sqlalchemy import Float, cast, distinct, exists, func, select
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

from amdockvs.models import DockingResultRecord, MoleculeRecord


def fmt_float(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def fmt_sci(value: float | None) -> str:
    return "-" if value is None else f"{value:.2e}"


def _result_json(key: str):
    return func.json_extract(DockingResultRecord.metrics, f"$.{key}")


def _result_metric(key: str):
    if key == "lipophilic_efficiency":
        return cast(func.coalesce(_result_json(key), _result_json("lle")), Float)
    return cast(_result_json(key), Float)


def _run_kind(model=DockingResultRecord):
    return func.coalesce(func.json_extract(model.metrics, "$.run_kind"), "screening")


def _protocol_hash(model=DockingResultRecord):
    return func.coalesce(func.json_extract(model.metrics, "$.protocol.hash"), "")


def _protocol_label(model=DockingResultRecord):
    return func.coalesce(func.json_extract(model.metrics, "$.protocol.label"), model.engine)


def _run_id(model=DockingResultRecord):
    return func.coalesce(func.json_extract(model.metrics, "$.run_id"), "")


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


def _pose_count_expr():
    pose = aliased(DockingResultRecord)
    return (
        select(func.count(pose.id))
        .where(
            pose.receptor_molecule_id == DockingResultRecord.receptor_molecule_id,
            pose.ligand_molecule_id == DockingResultRecord.ligand_molecule_id,
            _protocol_hash(pose) == _protocol_hash(),
            _run_kind(pose) == _run_kind(),
            _run_id(pose) == _run_id(),
        )
        .correlate(DockingResultRecord)
        .scalar_subquery()
    )


def results_receptor_config() -> TableConfig:
    return TableConfig(
        model_class=MoleculeRecord,
        columns=[
            ColumnDef("name", label="Receptor", width=120),
            ColumnDef("docked", label="Docked", kind=ColumnKind.INTEGER, align=AlignHint.RIGHT,
                      expr=_RECEPTOR_DOCKED, width=65, sortable=False),
            ColumnDef("done", label="Done", kind=ColumnKind.INTEGER, align=AlignHint.RIGHT,
                      expr=_RECEPTOR_DONE, width=65, sortable=False),
            ColumnDef("missing", label="Missing", kind=ColumnKind.INTEGER, align=AlignHint.RIGHT,
                      expr=_EXPECTED_LIGANDS - _RECEPTOR_DOCKED, width=70, sortable=False,
                      formatter=lambda value: "" if int(value or 0) == 0 else str(int(value))),
            ColumnDef("has_results", visible=False, expr=_RECEPTOR_HAS_RESULTS),
        ],
        default_filters=[
            FilterSpec("is_receptor", FilterOperator.EQ, True),
            FilterSpec("has_results", FilterOperator.EQ, True),
        ],
        default_sort=[],
        # page_size=20,
        load_mode=TableLoadMode.INFINITE,
        infinite_cache_pages=2,
        multi_select=False,
        show_filters=False,
        show_search=False,
        show_record_count=False,
        empty_message="No docking results yet",
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
                      align=AlignHint.RIGHT, expr=_result_metric("ligand_efficiency"), formatter=fmt_float, width=55),
            ColumnDef("lipophilic_efficiency", label="LLE", kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, expr=_result_metric("lipophilic_efficiency"), formatter=fmt_float,
                      width=60),
            ColumnDef("predicted_pki", label="pKi", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, expr=_result_metric("predicted_pki"), formatter=fmt_float, width=65),
            ColumnDef("predicted_ki_m", label="Ki (M)", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, expr=_result_metric("predicted_ki_m"), formatter=fmt_sci, width=65),
            ColumnDef("fit_quality", label="FQ", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, expr=_result_metric("fit_quality"), formatter=fmt_float, width=65),
            ColumnDef("bei", label="BEI", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, expr=_result_metric("bei"), formatter=fmt_float, width=65),
            ColumnDef("sei", label="SEI", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, expr=_result_metric("sei"), formatter=fmt_float, width=65),
            ColumnDef("pose_count", label="Poses", kind=ColumnKind.INTEGER, align=AlignHint.RIGHT,
                      expr=_pose_count_expr(), width=65),
            ColumnDef("protocol_label", label="Protocol", visible=False, expr=_protocol_label()),
            ColumnDef("protocol_hash", visible=False, expr=_protocol_hash()),
            ColumnDef("run_id", visible=False, expr=_run_id()),
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
                      align=AlignHint.RIGHT, expr=_result_metric("ligand_efficiency"), formatter=fmt_float, width=65),
            ColumnDef("predicted_pki", label="pKi", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, expr=_result_metric("predicted_pki"), formatter=fmt_float, width=65),
            ColumnDef("predicted_ki_m", label="Ki (M)", visible=False, kind=ColumnKind.NUMBER,
                      align=AlignHint.RIGHT, expr=_result_metric("predicted_ki_m"), formatter=fmt_sci, width=65),
            ColumnDef("protocol_hash", visible=False, expr=_protocol_hash()),
            ColumnDef("run_id", visible=False, expr=_run_id()),
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
