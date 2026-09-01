from pathlib import Path

from ms_flow.api import FileInputSpec, ProjectOutputDirSpec

from amdockvs.docking import preparation_jobs
from amdockvs.docking.preparation_jobs import PreparationJobParams, _iter_preparation_batches


def test_preparation_chunk_declares_worker_input_and_project_output(monkeypatch, tmp_path):
    source = tmp_path / "project" / "data" / "molecules" / "ligand.sdf"
    source.parent.mkdir(parents=True)
    source.write_text("ligand", encoding="utf-8")

    monkeypatch.setattr(
        preparation_jobs,
        "iter_entity_rows",
        lambda *_args, **_kwargs: iter([{"id": 1, "stored_path": str(source), "current_path": ""}]),
    )
    chunk = next(
        _iter_preparation_batches(
            project_db=object(),
            db_path=tmp_path / "project" / "project.db",
            entity_kind="ligand",
            output_dir=source.parent,
            params=PreparationJobParams(batch_size=1, force=True),
        )
    )

    assert isinstance(chunk["rows"][0]["stored_path"], FileInputSpec)
    assert chunk["rows"][0]["stored_path"].delivery == "path"
    assert isinstance(chunk["output_dir"], ProjectOutputDirSpec)
