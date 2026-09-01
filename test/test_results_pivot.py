"""Docking Results pivots: gated on being populated, disabled with the reason, never hidden.

The part worth a test is the gate, because getting it wrong is invisible: a pivot that stays
disabled after its data arrives, or one that silently shows another pivot's table.
"""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import amdockvs.ui.catalog  # noqa: F401 - imported first, it is what breaks the ui import cycle

from amdockvs.ui.tools.docking.results_pivot import ResultsPivotWidget, _freshness_text_and_delay
from amdockvs.ui.workspace import DockingResultsWidget
from amdockvs.models import DockingResultRecord, MoleculeRecord
from ms_components.ms_table import SmartTableView


class _Db:
    def __init__(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def get_session(self):
        return Session(self.engine)


_PIVOT_DB = _Db()


class _Runtime:
    """Only what the widget asks of the runtime: the availability query."""

    def __init__(self, **available):
        self.available = available
        self.molsuite = SimpleNamespace(project_db=_PIVOT_DB)
        self.docking = SimpleNamespace(
            pivot_availability=lambda: self.available,
            result_protocols=lambda **_kwargs: [],
            hit=lambda **_kwargs: None,
        )
        self.active_context = None


def _widget(**available):
    QApplication.instance() or QApplication(["amdockvs-pivot-test"])
    widget = ResultsPivotWidget(runtime=_Runtime(**available))
    widget._apply_availability(available)  # the real load is async; apply it here
    return widget


def _rows(widget):
    model = widget.pivot_combo.model()
    return {
        widget.pivot_combo.itemData(row): (model.item(row).isEnabled(), widget.pivot_combo.itemText(row))
        for row in range(widget.pivot_combo.count())
    }


def test_unpopulated_pivots_are_disabled_with_the_reason_not_hidden():
    widget = _widget(hits=True, offtarget=False, redocking=False)
    rows = _rows(widget)
    assert len(rows) == 3  # nothing is hidden
    assert rows["hits"][0] is True
    assert rows["offtarget"] == (False, "Off-target (ligand × receptor) — Needs a ligand docked against 2 receptors")
    assert rows["redocking"][0] is False


def test_picking_a_disabled_pivot_says_why_and_stays_put():
    widget = _widget(hits=True, offtarget=False, redocking=False)
    widget.pivot_combo.setCurrentIndex(widget.pivot_combo.findData("offtarget"))
    assert widget.reason_label.text() == "Needs a ligand docked against 2 receptors"
    assert widget.pivot_combo.currentData() == "hits"  # naming one pivot while showing another
    assert "offtarget" not in widget._pages  # and no live table built for it


def test_a_pivot_opens_by_itself_once_its_data_lands():
    widget = _widget(hits=True, offtarget=False, redocking=False)
    widget._apply_availability({"hits": True, "offtarget": True, "redocking": False})
    assert _rows(widget)["offtarget"] == (True, "Off-target (ligand × receptor)")


def _job(task_type, *, done, total, status="running"):
    return SimpleNamespace(
        task_type=task_type,
        chunks_done=done,
        chunks_total=total,
        chunks_failed=0,
        chunks_stage_failed=0,
        status=status,
    )


def test_view_level_activity_tracks_the_current_pivot_and_hides_when_done():
    widget = _widget(hits=True, offtarget=True, redocking=True)
    widget._view_visible = True
    widget._on_monitor_snapshot(SimpleNamespace(jobs=[
        _job("amdock_docking_job", done=8, total=12),
        _job("amdock_redocking_job", done=2, total=4),
    ]))

    assert not widget.activity.isHidden()
    assert widget.progress_label.text() == "Hits · 8 / 12 dockings"
    assert widget.updated_label.text() == "Waiting for new results…"

    widget.pivot_combo.setCurrentIndex(widget.pivot_combo.findData("redocking"))
    assert widget.progress_label.text() == "Redocking · 2 / 4 dockings"

    widget._on_monitor_snapshot(SimpleNamespace(jobs=[
        _job("amdock_docking_job", done=8, total=12),
        _job("amdock_redocking_job", done=4, total=4, status="completed"),
    ]))
    assert widget.activity.isHidden()


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0.2, "Updated just now"),
        (1.2, "Updated 1s ago"),
        (9.8, "Updated 9s ago"),
        (10.1, "Updated 10s ago"),
        (17.0, "Updated 15s ago"),
        (29.9, "Updated 25s ago"),
        (30.1, "Updated 30s ago"),
        (49.0, "Updated 40s ago"),
        (60.1, "Updated 1m ago"),
        (125.0, "Updated 2m ago"),
    ],
)
def test_freshness_text_advances_on_non_intrusive_steps(age, expected):
    text, delay_ms = _freshness_text_and_delay(age)
    assert text == expected
    assert delay_ms >= 100


def test_freshness_tracks_data_while_hidden_without_repainting():
    widget = _widget(hits=True, offtarget=True, redocking=True)
    widget._view_visible = True
    widget._on_monitor_snapshot(SimpleNamespace(jobs=[
        _job("amdock_docking_job", done=1, total=4),
    ]))
    assert "hits" not in widget._last_data_refresh

    widget._on_page_refreshed("hits", False)
    assert "hits" not in widget._last_data_refresh

    widget._view_visible = False
    rendered_while_visible = widget.updated_label.text()
    widget._on_page_refreshed("hits", True)
    data_time = widget._last_data_refresh["hits"]
    assert widget.updated_label.text() == rendered_while_visible
    widget._update_activity()
    assert widget.activity.isHidden()
    assert not widget._freshness_timer.isActive()

    widget._view_visible = True
    widget._update_activity()
    assert widget._last_data_refresh["hits"] == data_time
    assert widget.updated_label.text() == "Updated just now"


def test_reopening_results_preserves_the_last_data_time():
    widget = _widget(hits=True, offtarget=True, redocking=True)
    widget._on_monitor_snapshot(SimpleNamespace(jobs=[
        _job("amdock_docking_job", done=1, total=4),
    ]))
    last_data_time = 123.0
    widget._last_data_refresh["hits"] = last_data_time

    widget._view_visible = True
    widget._update_activity()

    assert widget._last_data_refresh["hits"] == last_data_time


def test_hits_results_always_select_a_concrete_receptor():
    QApplication.instance() or QApplication(["amdockvs-results-test"])
    db = _Db()
    with db.get_session() as session:
        session.add_all([
            MoleculeRecord(id=41, name="4UWH", is_receptor=True),
            MoleculeRecord(id=42, name="4UWK", is_receptor=True),
            MoleculeRecord(id=101, name="Ligand", is_ligand=True),
            DockingResultRecord(id=11, receptor_molecule_id=41, ligand_molecule_id=101,
                                engine="vina", pose_rank=1, score=-8.0),
            DockingResultRecord(id=12, receptor_molecule_id=42, ligand_molecule_id=101,
                                engine="vina", pose_rank=1, score=-7.0),
        ])
        session.commit()
    docking = SimpleNamespace(result_protocols=lambda **_kwargs: [], hit=lambda **_kwargs: None)
    widget = DockingResultsWidget(
        runtime=SimpleNamespace(molsuite=SimpleNamespace(project_db=db), docking=docking)
    )
    reloads = []
    widget._reload_protocols = lambda **kwargs: reloads.append(kwargs)
    widget._selected_receptor_id = None
    widget._on_receptors_loaded()

    assert isinstance(widget.receptor_table, SmartTableView)
    assert widget.receptor_table._model.loaded_count == 2
    assert widget._selected_receptor_id == 41
    assert reloads == [{"auto_select": True}]


def test_running_job_refresh_reaches_the_active_results_page():
    widget = _widget(hits=True, offtarget=True, redocking=True)
    widget._view_visible = True
    widget._on_monitor_snapshot(SimpleNamespace(jobs=[
        _job("amdock_docking_job", done=1, total=4),
    ]))
    calls = []
    widget._reload_availability = lambda: calls.append("availability")
    widget.current_page().refresh_counts = lambda: (calls.append("hits"), True)[1]

    assert widget.refresh_counts() is True
    assert calls == ["availability", "hits"]


def test_live_hits_refreshes_receptor_aggregates_and_only_the_ligand_count():
    QApplication.instance() or QApplication(["amdockvs-live-counts-test"])
    db = _Db()
    protocol = {"run_kind": "screening", "protocol": {"hash": "p", "label": "Vina"}}
    with db.get_session() as session:
        session.add_all([
            MoleculeRecord(id=41, name="4UWH", is_receptor=True),
            MoleculeRecord(id=101, name="A", is_ligand=True),
            MoleculeRecord(id=202, name="B", is_ligand=True),
            DockingResultRecord(id=11, receptor_molecule_id=41, ligand_molecule_id=101,
                                engine="vina", pose_rank=1, score=-8.0, metrics=protocol),
        ])
        session.commit()
    widget = DockingResultsWidget(runtime=SimpleNamespace(
        molsuite=SimpleNamespace(project_db=db),
        docking=SimpleNamespace(result_protocols=lambda **_kwargs: [], hit=lambda **_kwargs: None),
    ))
    widget._selected_receptor_id = 41
    widget._populate_protocol_combo([("p", "Vina")])
    widget._refresh_ligands()
    assert widget.ligand_table.record_total == 1

    with db.get_session() as session:
        session.add(DockingResultRecord(
            id=22, receptor_molecule_id=41, ligand_molecule_id=202,
            engine="vina", pose_rank=1, score=-7.0, metrics=protocol,
        ))
        session.commit()

    assert widget.refresh_counts() is True
    receptor = widget.receptor_table._model.get_row_data(0)
    assert receptor["docked"] == 2
    assert receptor["done"] == 2
    assert widget.ligand_table.record_total == 2
    assert "2 records" in widget.ligand_table._result_count_label.text()
    assert widget.ligand_table._model.loaded_count == 1


def test_live_ligand_reload_keeps_the_selected_ligand_context():
    QApplication.instance() or QApplication(["amdockvs-live-results-test"])
    db = _Db()
    protocol = {"run_kind": "screening", "protocol": {"hash": "p", "label": "Vina"}}
    with db.get_session() as session:
        session.add_all([
            MoleculeRecord(id=41, name="4UWH", is_receptor=True),
            MoleculeRecord(id=101, name="A", is_ligand=True),
            MoleculeRecord(id=202, name="B", is_ligand=True),
            DockingResultRecord(id=11, receptor_molecule_id=41, ligand_molecule_id=101,
                                engine="vina", pose_rank=1, score=-8.0, metrics=protocol),
            DockingResultRecord(id=22, receptor_molecule_id=41, ligand_molecule_id=202,
                                engine="vina", pose_rank=1, score=-7.0, metrics=protocol),
        ])
        session.commit()
    widget = DockingResultsWidget(runtime=SimpleNamespace(
        molsuite=SimpleNamespace(project_db=db),
        docking=SimpleNamespace(result_protocols=lambda **_kwargs: [], hit=lambda **_kwargs: None),
    ))
    widget._populate_protocol_combo([("p", "Vina")])
    widget._refresh_ligands(auto_select=False)
    widget._selected_ligand_id = 202
    widget._select_ligand_id(202, notify=False)
    pose_reloads = []
    widget.pose_table.set_filters = lambda filters: pose_reloads.append(filters)

    widget._refresh_ligands(preserve_context=True)

    selected = widget.ligand_table.get_selected_object()
    assert selected.ligand_molecule_id == 202
    assert pose_reloads == []


def test_result_tables_use_small_two_page_dynamic_windows():
    widget = _widget(hits=True, offtarget=False, redocking=False).current_page()
    assert all(isinstance(table, SmartTableView) for table in (
        widget.receptor_table, widget.ligand_table, widget.pose_table,
    ))
    assert widget.ligand_table._config.page_size == 100
    assert widget.pose_table._config.page_size == 20
    assert widget.ligand_table._config.infinite_cache_pages == 2
    assert not any(column.filterable or column.searchable for column in widget.ligand_table._config.columns)


def test_clear_filters_emits_one_scope_reload_and_keeps_protocol():
    widget = _widget(hits=True, offtarget=False, redocking=False).current_page()
    widget.protocol_combo.addItem("Vina", "p")
    widget.protocol_combo.setCurrentIndex(widget.protocol_combo.count() - 1)
    widget.hide_failed_check.setChecked(True)
    widget.metric_filters.add_condition("score", "lte", -8.0)
    calls = []
    widget._refresh_ligands = lambda **kwargs: calls.append(kwargs)

    widget._clear_filters()

    assert not widget.hide_failed_check.isChecked()
    assert widget.metric_filters.conditions() == []
    assert widget.protocol_combo.currentData() == "p"
    assert calls == [{"auto_select": True}]
