import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import AliasChoices, BaseModel, Field  # noqa: F401 - preloaded before amdockvs (import cycle)
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

import amdockvs.ui.catalog  # noqa: F401 - imported first, it is what breaks the ui import cycle
from amdockvs.models import DockingResultRecord, DockingRun, MoleculeRecord
from amdockvs.ui.tools.docking import offtarget
from amdockvs.ui.tools.docking.offtarget import OffTargetResultsWidget


class _Db:
    def __init__(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)

    def get_session(self):
        return Session(self.engine)


def _result(receptor, ligand, rank, score, run=1):
    return DockingResultRecord(receptor_molecule_id=receptor, ligand_molecule_id=ligand,
                               docking_run_id=run, pose_rank=rank, score=score)


@pytest.fixture
def db():
    db = _Db()
    with db.get_session() as session:
        session.add_all([
            DockingRun(id=1, run_id="a", engine="vina", protocol_hash="p", protocol_label="Vina"),
            DockingRun(id=2, run_id="b", engine="vina", run_kind="redocking", protocol_hash="p"),
            MoleculeRecord(id=1, name="Target", is_receptor=True),
            MoleculeRecord(id=2, name="Off-target", is_receptor=True),
            MoleculeRecord(id=101, name="Ligand", is_ligand=True),
            _result(1, 101, 1, -10.0), _result(1, 101, 2, -8.0), _result(2, 101, 1, -5.0),
            _result(2, 101, 1, -20.0, run=2),  # a redocking pose never enters the matrix
        ])
        session.commit()
    return db


def _matrix(monkeypatch, db):
    QApplication.instance() or QApplication(["offtarget-colors-test"])
    monkeypatch.setattr(offtarget, "run_async", lambda work, done, *_a, **_k: done(work()))
    monkeypatch.setattr(OffTargetResultsWidget, "_pymol", lambda self: (None, None))
    receptors = [SimpleNamespace(receptor_id=1, receptor_name="Target"),
                 SimpleNamespace(receptor_id=2, receptor_name="Off-target")]
    docking = SimpleNamespace(
        receptor_summaries=lambda: receptors,
        result_protocols=lambda **_k: [("p", "Vina")],
        max_pose_rank=lambda **_k: 2,
    )
    return OffTargetResultsWidget(runtime=SimpleNamespace(workspace=SimpleNamespace(project_db=db), docking=docking))


@pytest.fixture
def matrix(monkeypatch, db):
    widget = _matrix(monkeypatch, db)
    yield widget
    widget.close()


def _set_score(db, receptor, rank, score):
    with db.get_session() as session:
        for row in session.exec(select(DockingResultRecord).where(
                DockingResultRecord.receptor_molecule_id == receptor, DockingResultRecord.pose_rank == rank,
                DockingResultRecord.docking_run_id == 1)):
            row.score = score
        session.commit()


def _cell(matrix, receptor=2, row=0):
    return matrix.cell(matrix.table._model.get_row_data(row), receptor)


@pytest.mark.parametrize("diff, color", [
    (-6, "#b2182b"), (-4, "#b2182b"), (-2, "#d88b95"),
    (0, "#ffffff"), (2, "#90b2d5"), (4, "#2166ac"), (6, "#2166ac"),
])
def test_cell_is_blue_when_the_target_is_preferred_and_saturates(matrix, db, diff, color):
    _set_score(db, 2, 1, -10 + diff)
    matrix.table.refresh()
    text, _rank, _detail, background = _cell(matrix)
    assert text == f"{diff:+.2f}"
    assert background.getRgbF() == pytest.approx(QColor(color).getRgbF(), abs=1 / 255)
    channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in background.getRgbF()[:3]]
    luminance = sum(c * w for c, w in zip(channels, (0.2126, 0.7152, 0.0722)))
    contrast = ((luminance + 0.05) / 0.05 if offtarget._contrast(background) == QColor("black")
                else 1.05 / (luminance + 0.05))
    assert contrast >= 4.5


def test_show_flips_the_number_but_never_the_colour(matrix):
    blue = _cell(matrix)[3]
    assert _cell(matrix)[0] == "+5.00"
    matrix.show_combo.setCurrentIndex(matrix.show_combo.findData("target-minus-off"))
    assert _cell(matrix)[0] == "-5.00"
    assert _cell(matrix)[3] == blue
    matrix.show_combo.setCurrentIndex(matrix.show_combo.findData("fold"))
    assert _cell(matrix)[0] == "×4,624"  # exp(5 / RT) at 298.15 K
    assert _cell(matrix)[3] == blue


def test_a_cell_pose_pick_and_palette_change_keep_the_selection(matrix):
    matrix._poses[101] = {1: [{"pose_rank": 1, "score": -10.0}, {"pose_rank": 2, "score": -8.0}],
                          2: [{"pose_rank": 1, "score": -5.0}]}
    matrix._select_pose(101, 1, 2)
    assert _cell(matrix, 1)[1] == 2
    assert _cell(matrix)[0] == "+3.00"
    old_color = _cell(matrix)[3]
    matrix.palette_combo.setCurrentIndex(1)
    assert _cell(matrix, 1)[1] == 2
    assert _cell(matrix)[0] == "+3.00"
    assert _cell(matrix)[3] != old_color
    assert "#762a83" in matrix.color_bar.styleSheet()


def test_apply_moves_the_column_to_that_pose_in_sql(matrix):
    matrix.pose_combo.setCurrentIndex(matrix.pose_combo.findData(2))
    matrix.pose_column_combo.setCurrentIndex(matrix.pose_column_combo.findData(1))
    matrix._apply_pose()
    assert _cell(matrix, 1)[:2] == ("-8.00", 2)
    assert _cell(matrix)[0] == "+3.00"
    matrix.pose_column_combo.setCurrentIndex(0)  # every column: the off-target has no P2
    matrix._apply_pose()
    assert _cell(matrix)[0] == "—"


@pytest.mark.parametrize("score", [None, float("nan"), float("inf")])
def test_missing_or_nonfinite_values_have_no_color(matrix, db, score):
    _set_score(db, 2, 1, score)
    matrix.table.refresh()
    text, _rank, _detail, background = _cell(matrix)
    assert text == "—"
    assert background is None


def _wait_for(done, timeout_s: float = 10.0) -> None:
    """Process events until `done()`: the table counts its rows in the background."""
    deadline = time.monotonic() + timeout_s
    while not done() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert done()


def test_every_ligand_is_reachable_but_only_a_window_is_read(monkeypatch, db):
    with db.get_session() as session:
        for ligand in range(1000, 2500):
            session.add(MoleculeRecord(id=ligand, name=f"L{ligand}", is_ligand=True))
            session.add(_result(1, ligand, 1, -float(ligand) / 100))
            session.add(_result(2, ligand, 1, -float(ligand) / 100 + 1))
        session.commit()
    widget = _matrix(monkeypatch, db)
    try:
        _wait_for(lambda: widget.table.record_total == 1501)
        assert widget.table._model.loaded_count <= 200
        first = widget.table._model.get_row_data(0)
        assert first["ligand_name"] == "L2499"  # best target score first
        assert widget.cell(first, 2)[0] == "+1.00"
    finally:
        widget.close()


def test_the_delegate_paints_the_cell_colour(matrix):
    matrix.resize(900, 500)
    matrix.show()
    QApplication.processEvents()
    view = matrix.table.view
    index = next(view.model().index(0, c) for c in range(view.model().columnCount())
                 if view.model().headerData(c, Qt.Horizontal) == "Off-target")
    rect = view.visualRect(index)
    pixel = view.viewport().grab().toImage().pixelColor(rect.left() + 4, rect.bottom() - 4)
    assert pixel.getRgbF()[:3] == pytest.approx(QColor("#2166ac").getRgbF()[:3], abs=2 / 255)


def test_rows_are_the_ligands_an_off_target_docked_and_stay_put_across_poses(monkeypatch, db):
    """Target-only ligands (beyond the funnel's top-N) never show; a target pose change
    swaps values, not rows: a ligand without that pose keeps its row with —."""
    with db.get_session() as session:
        session.add_all([MoleculeRecord(id=102, name="Target only", is_ligand=True),
                         MoleculeRecord(id=103, name="No P2", is_ligand=True),
                         _result(1, 102, 1, -12.0), _result(1, 103, 1, -11.0), _result(2, 103, 1, -6.0)])
        session.commit()
    widget = _matrix(monkeypatch, db)
    try:
        names = lambda: [widget.table._model.get_row_data(r)["ligand_name"] for r in range(widget.table.record_total)]
        assert names() == ["No P2", "Ligand"]
        widget.pose_combo.setCurrentIndex(widget.pose_combo.findData(2))
        widget.pose_column_combo.setCurrentIndex(widget.pose_column_combo.findData(1))
        widget._apply_pose()
        assert sorted(names()) == ["Ligand", "No P2"]
        no_p2 = next(widget.table._model.get_row_data(r) for r in range(2)
                     if widget.table._model.get_row_data(r)["ligand_name"] == "No P2")
        assert widget.cell(no_p2, 1)[0] == "—" and widget.cell(no_p2, 2)[0] == "—"
    finally:
        widget.close()
