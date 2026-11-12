"""B3/A1: a docking box with an edge <= 0 is refused where it is saved and never reaches an engine."""
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from amdockvs.binding_sites.api import BindingSiteAPI
from amdockvs.docking.api import DockingAPI
from amdockvs.docking.repository import _augment_molecule_rows, persist_receptor_grid
from amdockvs.models import BindingSite, MoleculeRecord
from amdockvs.models.docking import box_error

BAD_SIZES = [(-10.0, 22.0, 22.0), (0.0, 22.0, 22.0), (float("nan"), 22.0, 22.0), (float("inf"), 22.0, 22.0)]


class _Db:
    def __init__(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)

    def get_session(self):
        return Session(self.engine)


@pytest.fixture
def runtime():
    db = _Db()
    with db.get_session() as session:
        session.add(MoleculeRecord(id=1, name="4OYS", is_receptor=True))
        session.commit()
    runtime = SimpleNamespace(workspace=SimpleNamespace(project_db=db), _require_active_project=lambda: None)
    runtime.binding_sites = BindingSiteAPI(runtime)
    runtime.docking = DockingAPI(runtime)
    return runtime


def _sites(runtime):
    with runtime.workspace.project_db.get_session() as session:
        return list(session.exec(select(BindingSite)))


def test_box_error_accepts_a_real_box_only():
    assert box_error((0, 0, 0), (22, 22, 22), 0.375) is None
    for size in BAD_SIZES:
        assert "greater than 0" in box_error((0, 0, 0), size)
    assert "spacing" in box_error((0, 0, 0), (22, 22, 22), 0.0)
    assert "finite" in box_error((float("nan"), 0, 0), (22, 22, 22))
    assert "three values" in box_error((0, 0), (22, 22, 22))


@pytest.mark.parametrize("size", BAD_SIZES)
def test_set_grid_refuses_a_box_that_is_not_one_and_stores_nothing(runtime, size):
    with pytest.raises(ValueError, match="greater than 0"):
        runtime.docking.set_grid(receptor_id=1, center=(0, 0, 0), size=size)
    assert _sites(runtime) == []


def test_set_grid_refuses_a_spacing_of_zero(runtime):
    with pytest.raises(ValueError, match="spacing"):
        runtime.docking.set_grid(receptor_id=1, center=(0, 0, 0), size=(22, 22, 22), spacing=0.0)
    assert _sites(runtime) == []


def test_a_valid_grid_is_still_stored(runtime):
    grid = runtime.docking.set_grid(receptor_id=1, center=(1, 2, 3), size=(20, 22, 24))
    assert grid["size"] == [20.0, 22.0, 24.0]
    assert runtime.docking.get_grid(receptor_id=1) is not None


def test_save_site_and_the_job_writer_refuse_it_too(runtime):
    with pytest.raises(ValueError, match="greater than 0"):
        runtime.binding_sites.save_site(molecule_id=1, center=(0, 0, 0), size=(-10, 22, 22))
    with pytest.raises(ValueError, match="greater than 0"):
        persist_receptor_grid(runtime.workspace.project_db, receptor_id=1, metadata_json="{}",
                              grid_payload={"center": (0, 0, 0), "size": (-10, 22, 22)})
    assert _sites(runtime) == []


def test_a_bad_box_stored_earlier_counts_as_no_grid(runtime):
    # A1: a project saved before the check keeps its bad box; the run's readiness check must
    # report the receptor as having no grid instead of handing the box to the engine.
    db = runtime.workspace.project_db
    with db.get_session() as session:
        session.add(BindingSite(molecule_id=1, name="old", is_active=True, center_x=0, center_y=0, center_z=0,
                                size_x=-10, size_y=22, size_z=22))
        session.commit()
    assert not _sites(runtime)[0].is_defined
    assert runtime.docking.get_grid(receptor_id=1) is None
    row = _augment_molecule_rows(db, [{"id": 1}], role_type="receptor", engine="ad4")[0]
    assert row["grid_engine"] is False
