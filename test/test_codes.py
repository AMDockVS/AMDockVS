"""Categorical columns store `codes.id`: every value the app writes has a code, a name round-trips
through the ORM and QuerySpec, the raw column holds the integer, and `codes` is seeded."""
import pytest
from ms_flow.core.database import ProjectStore
from ms_flow.query import QuerySpec, db_count, db_rows
from sqlalchemy import text

from amdockvs.core import vocab
from amdockvs.core.vocab import CODES
from amdockvs.diversity.clustering import CLUSTERING_METHODS
from amdockvs.docking.engines.programs import list_docking_programs
from amdockvs.models import MoleculeRecord, ensure_codes
from amdockvs.qsar.modeling import SUPPORTED_QSAR_ALGORITHMS


def _values(cls) -> set[str]:
    return {v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str)}


def test_every_written_value_has_a_code():
    domains = {
        vocab.MoleculeType: "molecule_type", vocab.MoleculeUsageClass: "usage_class",
        vocab.ModelSource: "model_source", vocab.ReprType: "repr_type",
        vocab.BindingSiteSource: "binding_site_source", vocab.RunKind: "run_kind",
        vocab.ActivityKind: "activity_kind", vocab.ComplexPurpose: "complex_purpose",
        vocab.TargetState: "run_state", vocab.DispatchState: "dispatch_state",
        vocab.ClusteringMethod: "clustering_method", vocab.SetPurpose: "set_purpose",
    }
    for cls, domain in domains.items():
        assert _values(cls) <= set(CODES[domain]), domain
    programs = list_docking_programs()
    assert {p.docking_engine for p in programs} | {p.key for p in programs if p.key != "autodock"} <= set(CODES["engine"])
    assert set(SUPPORTED_QSAR_ALGORITHMS) <= set(CODES["qsar_algorithm"])
    assert set(CLUSTERING_METHODS) <= set(CODES["clustering_method"])
    ids = [i for names in CODES.values() for i in names.values()]
    assert len(ids) == len(set(ids)) and 0 not in ids


def test_a_code_round_trips_and_the_table_is_seeded(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    try:
        with store.get_session() as session:
            session.add_all([MoleculeRecord(usage_class="reference"), MoleculeRecord(usage_class="general")])
            session.commit()
            raw = [r[0] for r in session.connection().execute(text("SELECT usage_class FROM molecules ORDER BY id"))]
            seeded = session.connection().execute(text("SELECT COUNT(*) FROM codes")).scalar()
        assert raw == [CODES["usage_class"]["reference"], CODES["usage_class"]["general"]]
        assert seeded == sum(map(len, CODES.values()))
        ensure_codes(store)  # idempotent at every open
        rows = db_rows(store, QuerySpec(table="molecules", fields=("usage_class",), order=("id",)))
        assert [r["usage_class"] for r in rows] == ["reference", "general"]
        assert db_count(store, QuerySpec(table="molecules", filters={"usage_class": "reference"})) == 1
        with pytest.raises(Exception, match="usage_class code"), store.get_session() as session:
            session.add(MoleculeRecord(usage_class="nonsense"))
            session.commit()
    finally:
        ProjectStore.clear_cached_stores()
