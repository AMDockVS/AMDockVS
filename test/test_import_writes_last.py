"""Import writes molecule files last: a rejected molecule leaves nothing on disk."""
from __future__ import annotations

import pytest

pytest.importorskip("rdkit")

from amdockvs.io.transformers.materializers import materialize_import_batch

# Two salts: the big one is culled by max_heavy_atoms, the small one survives.
SMALL = "CC(=O)O.[Na+]"
BIG = "CCCCCCCCCCCCCCCCC(=O)O.[Na+]"


def _sdf_record(smiles: str, name: str) -> str:
    from rdkit import Chem

    mol = Chem.MolFromSmiles(smiles)
    mol.SetProp("_Name", name)
    return Chem.MolToMolBlock(mol) + "$$$$\n"


def _payload(tmp_path, records, prefilter):
    sdf = tmp_path / "ligands.sdf"
    sdf.write_text("".join(_sdf_record(s, n) for s, n in records), encoding="utf-8")
    return {
        "kind": "ligand",
        "file_path": str(sdf),
        "storage_dir": str(tmp_path / "data" / "ligands"),
        "input_format": "sdf",
        "primary_role": "ligand",
        "molecule_kind": "small_molecule",
        "prefilter": prefilter,
        "entries": [
            {"source_index": index, "raw": _sdf_record(smiles, name)}
            for index, (smiles, name) in enumerate(records)
        ],
    }


def test_rejected_molecule_writes_no_files(tmp_path):
    payload = _payload(
        tmp_path,
        [(SMALL, "small"), (BIG, "big")],
        {"target_molecule_kinds": ["small_molecule"], "max_heavy_atoms": 12},
    )
    rows = materialize_import_batch(payload)

    assert [row["name"] for row in rows] == ["small"]
    written = sorted(p.name for p in (tmp_path / "data" / "ligands").rglob("*.sdf"))
    # Only the survivor's files: original + current + its two fragments. Nothing for "big".
    assert len(written) == 4


def test_survivor_fragment_files_exist_where_metadata_says(tmp_path):
    rows = materialize_import_batch(_payload(tmp_path, [(SMALL, "small")], None))

    row = rows[0]
    fragments = row["extra_data"]["fragmentation"]["components"]
    assert fragments
    for fragment in fragments:
        assert (tmp_path / fragment["path"]).exists()
    assert (tmp_path / row["current_path"]).exists()
