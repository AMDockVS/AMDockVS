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

        drop_deferred_indexes(rt.workspace.project_db)  # an import that died halfway
        rt.close_project()
        rt.open_project(project.id)
        assert set(DEFERRED_INDEXES) <= _indexes(rt)
    finally:
        rt.close_project()


def test_role_scopes_never_scan_the_table(tmp_path, monkeypatch):
    """With no table stats SQLite picks the index matching the most `=` terms: a receptor
    query or a ligand count must find its own index, and a page in id order must not sort
    (a sort is the whole table before the first row, ~5 s for a descending page of 4M)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rt = AMDockVSRuntime()
    rt.create_project(name="plans", folder=tmp_path / "project")
    try:
        engine = rt.workspace.project_db.engine
        from amdockvs.models.molecules import ensure_molecule_indexes
        ensure_molecule_indexes(rt.workspace.project_db)
        scope = "molecule_type = ? AND usage_class = ? AND excluded = ?"
        plans = [
            (f"SELECT COUNT(*) FROM molecules WHERE is_receptor = ? AND {scope}",
             (True, "protein", "general", False), "idx_mol_receptor"),
            ("SELECT id FROM molecules WHERE is_receptor = 1 AND usage_class = ? ORDER BY id LIMIT 21",
             ("general",), "idx_mol_receptor"),
            ("SELECT id FROM molecules WHERE is_receptor = 1 ORDER BY id", (), "idx_mol_receptor_id"),
            (f"SELECT COUNT(*) FROM molecules WHERE is_ligand = ? AND {scope}",
             (True, "small_molecule", "general", False), "COVERING INDEX idx_mol_role_scope"),
            ("SELECT * FROM molecules WHERE is_ligand = 1 AND excluded = 0 AND usage_class = ? ORDER BY id DESC LIMIT 21",
             ("general",), "ix_molecules_usage_class"),
        ]
        with engine.connect() as conn:
            for sql, params, index in plans:
                plan = " ".join(row[-1] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}", params))
                assert index in plan and "TEMP B-TREE" not in plan, (sql, plan)
    finally:
        rt.close_project()
