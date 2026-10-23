"""The deferred indexes of `molecules`: a bulk import builds them at its end, opening a project
builds the ones an import that died left missing, and the retired ones are gone."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.models.molecules import DEFERRED_INDEXES, RETIRED_INDEXES, drop_deferred_indexes
from amdockvs.runtime import AMDockVSRuntime


def _indexes(rt) -> set[str]:
    with rt.workspace.project_db.engine.connect() as conn:
        return {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='molecules'")}


def test_import_and_open_build_the_deferred_indexes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    library = tmp_path / "lib.smi"
    library.write_text("CCO a\nc1ccccc1O b\nCC(=O)Nc1ccc(O)cc1 c\n")
    rt = AMDockVSRuntime()
    project = rt.create_project(name="indexes", folder=tmp_path / "project")
    try:
        rt.wait_for_jobs(rt.loader.load_ligands([library]), timeout_s=300)
        assert set(DEFERRED_INDEXES) <= _indexes(rt)
        assert not _indexes(rt) & set(RETIRED_INDEXES)

        drop_deferred_indexes(rt.workspace.project_db.engine)  # an import that died halfway
        rt.close_project()
        rt.open_project(project.id)
        assert set(DEFERRED_INDEXES) <= _indexes(rt)
    finally:
        rt.close_project()
