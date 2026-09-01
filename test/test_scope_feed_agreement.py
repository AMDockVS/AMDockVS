"""Each tool's count and feed come from the same `scope_spec`.

This is the §1 guardrail: if `db_count(scope_spec(p))` and the number of rows the feed emits
diverge, `total_chunks` is mis-declared — and over-declaring leaves the job never completing.
Each tool exposes ONE function (`scope_spec`); counting and iterating are ms_flow's `db_count` /
`db_pages`, not per-tool wrappers.
"""
from pathlib import Path

from ms_flow.core.database.project import ProjectStore
from ms_flow.query import db_count

from amdockvs.chemistry import repository as chemistry_repo
from amdockvs.chemistry.jobs import LigandChemistryJobParams, _iter_ligand_chemistry_batches
from amdockvs.docking.preparation_jobs import PreparationJobParams
from amdockvs.docking.preparation_jobs import scope_spec as preparation_scope_spec
from amdockvs.models.molecules import MoleculeRecord
from amdockvs.pockets.jobs import P2RankPredictionParams
from amdockvs.pockets.jobs import scope_spec as pockets_scope_spec
from amdockvs.qsar.jobs import DescriptorJobParams, _iter_descriptor_batches
from amdockvs.qsar.jobs import scope_spec as qsar_scope_spec


def _project(tmp_path, *, ligands=5, receptors=2):
    store = ProjectStore.open_at(tmp_path / "project.db")
    with store.get_session() as session:
        for i in range(ligands):
            session.add(MoleculeRecord(
                name=f"lig_{i}", is_ligand=True, source="/data/libs.sdf", source_index=i,
                input_format="sdf", stored_path=f"/data/lig_{i}.sdf", has_3d=True,
                excluded=(i == 0),  # one excluded: the scope has to discount it in SQL
            ))
        for i in range(receptors):
            session.add(MoleculeRecord(
                name=f"rec_{i}", is_receptor=True, source="/data/rec.pdb",
                input_format="pdb", stored_path=f"/data/rec_{i}.pdb", has_3d=True,
            ))
        session.commit()
    return store


def test_chemistry_count_matches_its_feed(tmp_path):
    store = _project(tmp_path)
    params = LigandChemistryJobParams(operation="protonate", batch_size=2)
    spec = chemistry_repo.scope_spec(role_flag="is_ligand", filters=params.ligand_filters)
    fed = sum(
        len(chunk["rows"])
        for chunk in _iter_ligand_chemistry_batches(
            project_db=store, db_path=Path(store.db_path), output_dir=tmp_path, params=params
        )
    )
    assert db_count(store, spec) == fed == 4  # 5 ligands - 1 excluded
    store.dispose()


def test_qsar_count_matches_its_feed(tmp_path):
    store = _project(tmp_path)
    params = DescriptorJobParams(batch_size=2, only_missing=False, compute_fingerprints=False)
    fed = sum(len(chunk["items"]) for chunk in _iter_descriptor_batches(store, params))
    assert db_count(store, qsar_scope_spec(params)) == fed == 4
    store.dispose()


def test_preparation_count_is_a_ceiling_not_a_total(tmp_path):
    # The feed also drops in Python whatever is already prepared *on disk*, so the count is a
    # ceiling. That is why this job does not declare total_chunks: over-declaring would hang it.
    store = _project(tmp_path)
    spec = preparation_scope_spec(PreparationJobParams(engine="ad4"), entity_kind="ligand")
    assert db_count(store, spec) == 4
    store.dispose()


def test_pockets_count_matches_its_scope(tmp_path):
    store = _project(tmp_path)
    params = P2RankPredictionParams(run_id="r", p2rank_command="p2rank", java_command="java")
    assert db_count(store, pockets_scope_spec(params)) == 2
    store.dispose()
