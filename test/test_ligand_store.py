"""The ligand feeds read through the store, not the database.

§2 of `docs/plan_modos_vs_htpvs_2026-09-03.md`: `vs` and `htpvs` differ only in where the
ligands live, so every feed asks a `LigandStore`. This is the check that they actually do —
a fake store with no database behind it has to be enough to feed them.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.chemistry.repository import iter_ligand_rows
from amdockvs.molecules.storage import DbStore, as_store, store_from_config
from amdockvs.qsar.jobs import DescriptorJobParams, _iter_descriptor_batches
from amdockvs.diversity.jobs import SelectionClusterJobParams, scope_molecule_count, scope_molecule_rows


class FakeStore:
    """Holds two ligands and nothing else. No session, no engine, no db_path."""

    def __init__(self):
        self.specs = []

    def iter_rows(self, spec, *, batch_size=128):
        self.specs.append(spec)
        yield {"id": 1, "stored_path": "/data/a.sdf", "current_path": "", "current_model_index": None,
               "input_format": "sdf", "extra_data": {}, "has_3d": True}
        yield {"id": 2, "stored_path": "/data/b.sdf", "current_path": "", "current_model_index": None,
               "input_format": "sdf", "extra_data": {}, "has_3d": True}

    def count(self, spec):
        self.specs.append(spec)
        return 2

    def materialize(self, rows):
        return [dict(row) for row in rows]


def test_chemistry_feed_reads_the_store():
    store = FakeStore()
    assert [row["id"] for row in iter_ligand_rows(store, batch_size=2)] == [1, 2]
    assert store.specs[0].filters["is_ligand"] is True  # the scope is still the tool's


def test_qsar_feed_reads_the_store():
    store = FakeStore()
    params = DescriptorJobParams(batch_size=2, only_missing=False, compute_fingerprints=False)
    items = [item for chunk in _iter_descriptor_batches(store, params) for item in chunk["items"]]
    assert [item["molecule_id"] for item in items] == [1, 2]


def test_selection_feed_and_count_read_the_store():
    store = FakeStore()
    params = SelectionClusterJobParams()
    assert [row["id"] for row in scope_molecule_rows(store, params)] == [1, 2]
    assert scope_molecule_count(store, params) == 2


def test_a_bare_project_db_still_works():
    """Every UI call site passes `project_db` and must keep working untouched."""
    project_db = object()
    wrapped = as_store(project_db)
    assert isinstance(wrapped, DbStore) and wrapped.project_db is project_db
    assert as_store(wrapped) is wrapped  # a store is not wrapped twice


def test_store_from_config_is_always_rows():
    """The row tools resolve their store from the job config; shard tools build a ShardStore
    explicitly, so nothing here dispatches on a mode."""
    project_db = object()
    assert store_from_config({"project_db": project_db}).project_db is project_db
    assert isinstance(store_from_config({"project_db": project_db}), DbStore)
