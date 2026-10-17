"""Structures go in as payloads and come back with their format; a pre-payload project is refused."""
import sqlite3

import pytest
from ms_flow.core.database.project import ProjectStore

from amdockvs.core.payloads import molecule_payload_id, read_structure
from amdockvs.models.molecules import MoleculeRecord
from payload_seed import seed_payload


def test_a_molecule_reads_back_its_current_structure_and_format(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    stored = seed_payload(store, "CCO\n", "smi")
    current = seed_payload(store, "mol\n\n\nM  END\n$$$$\n", ".SDF")
    with store.get_session() as session:
        session.add(MoleculeRecord(name="lig", stored_payload_id=stored, current_payload_id=current))
        session.add(MoleculeRecord(name="raw", stored_payload_id=stored))
        session.commit()
    with sqlite3.connect(tmp_path / "project.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM molecules ORDER BY id")]
    assert [molecule_payload_id(r) for r in rows] == [current, stored]
    assert read_structure(store, current) == ("mol\n\n\nM  END\n$$$$\n", "sdf")
    assert read_structure(store, stored) == ("CCO\n", "smi")


def test_a_project_with_molecule_paths_is_refused(tmp_path):
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    summary = runtime.create_project(name="old", folder=tmp_path)
    db = runtime.workspace.project_db.db_path
    runtime.close_project()
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE molecules ADD COLUMN stored_path VARCHAR")
    with pytest.raises(ValueError, match="older AMDockVS"):
        runtime.open_project(summary.id)
    assert runtime.workspace.project_db is None
