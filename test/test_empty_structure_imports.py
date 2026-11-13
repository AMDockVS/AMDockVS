"""A PDB/CIF with no atoms is unreadable, not a 0-atom molecule with has_3d=True, whichever
role or molecule type imports it; the valid files of the same batch still import."""
from __future__ import annotations

import pytest

pytest.importorskip("rdkit")

RICH_PDB = """\
ATOM      1  N   GLY A   1       8.000  40.000   0.000  1.00 20.00           N
ATOM      2  CA  GLY A   1       9.500  40.000   0.000  1.00 20.00           C
ATOM      3  C   GLY A   1      11.000  40.000   0.000  1.00 20.00           C
ATOM      4  O   GLY A   1      12.000  40.000   0.000  1.00 20.00           O
END
"""
BAD = {
    "empty.pdb": "",
    "invalid.pdb": "This is not a structure\n",
    "invalid.cif": "data_wrong\nloop_\n_atom_site.id\n1 nonsense broken\n",
}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from amdockvs.runtime import AMDockVSRuntime

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("AMDOCK_OFFLINE", "1")
    rt = AMDockVSRuntime()
    rt.create_project(name="empty", folder=tmp_path / "project")
    yield rt
    rt.shutdown()


def _rows(runtime, role, paths, **kwargs):
    from sqlmodel import select

    from amdockvs.models import MoleculeRecord

    load = getattr(runtime.loader, f"load_{role}s")
    runtime.wait_for_jobs(list(load([str(p) for p in paths], executor_name="thread", **kwargs)), timeout_s=60)
    with runtime.workspace.project_db.get_session() as session:
        return [(row.name, row.n_atoms) for row in session.exec(select(MoleculeRecord)).all()]


@pytest.mark.parametrize("name", sorted(BAD))
@pytest.mark.parametrize("role,kwargs", [
    ("receptor", {}),
    ("ligand", {}),
    ("ligand", {"molecule_kind": "peptide"}),  # not a small molecule: the copy path
])
def test_a_file_with_no_atoms_is_not_a_molecule(runtime, tmp_path, name, role, kwargs):
    path = tmp_path / name
    path.write_text(BAD[name])
    assert _rows(runtime, role, [path], **kwargs) == []


def test_a_mixed_receptor_batch_keeps_only_the_real_structures(runtime, tmp_path):
    good_a, bad, good_b = tmp_path / "a.pdb", tmp_path / "invalid.pdb", tmp_path / "b.pdb"
    good_a.write_text(RICH_PDB)
    bad.write_text(BAD["invalid.pdb"])
    good_b.write_text(RICH_PDB)
    rows = _rows(runtime, "receptor", [good_a, bad, good_b], batch_size=1)
    assert sorted(rows) == [("a", 4), ("b", 4)]
