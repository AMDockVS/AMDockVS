"""The preparation feed chunks lazily and filters out what is already prepared.

Replaces test_molecule_stream.py: MoleculeStream was deleted and its chunking/filtering are now
`itertools.batched` and the built-in `filter`, so what is left to protect is the consumer.
"""
from pathlib import Path

from amdockvs.docking import preparation_jobs
from amdockvs.docking.preparation_jobs import PreparationJobParams, _iter_preparation_batches


def _fake_rows(pulled: list[int], total: int, prepared_path: str = ""):
    def iter_entity_rows(project_db, **kwargs):
        for i in range(1, total + 1):
            pulled.append(i)
            yield {"id": i, "stored_path": f"/tmp/lig_{i}.sdf", "prepared_ad4_path": prepared_path}
    return iter_entity_rows


def _batches(monkeypatch, *, total, batch_size, force=False, prepared_path=""):
    pulled: list[int] = []
    monkeypatch.setattr(preparation_jobs, "iter_entity_rows", _fake_rows(pulled, total, prepared_path))
    chunks = _iter_preparation_batches(
        project_db=object(),
        db_path=Path("/tmp/project.db"),
        entity_kind="ligand",
        output_dir=Path("/tmp/out"),
        params=PreparationJobParams(batch_size=batch_size, force=force),
    )
    return chunks, pulled


def test_first_chunk_arrives_without_draining_the_library(monkeypatch):
    chunks, pulled = _batches(monkeypatch, total=1000, batch_size=3)
    first = next(chunks)
    assert [row["id"] for row in first["rows"]] == [1, 2, 3]
    assert pulled == [1, 2, 3]  # nothing beyond the first batch was read


def test_batches_are_bounded_by_element_count(monkeypatch):
    chunks, _ = _batches(monkeypatch, total=7, batch_size=3)
    assert [len(chunk["rows"]) for chunk in chunks] == [3, 3, 1]


def test_an_empty_scope_still_closes_the_job_with_one_chunk(monkeypatch):
    chunks, _ = _batches(monkeypatch, total=0, batch_size=3)
    assert [chunk["rows"] for chunk in chunks] == [[]]


def test_already_prepared_rows_are_skipped_unless_forced(monkeypatch, tmp_path):
    done = tmp_path / "done.pdbqt"
    done.write_text("x")
    chunks, _ = _batches(monkeypatch, total=5, batch_size=2, prepared_path=str(done))
    assert [chunk["rows"] for chunk in chunks] == [[]]

    chunks, _ = _batches(monkeypatch, total=5, batch_size=2, force=True, prepared_path=str(done))
    assert [len(chunk["rows"]) for chunk in chunks] == [2, 2, 1]
