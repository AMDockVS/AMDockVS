"""Import returns payloads and writes no molecule files: a rejected molecule leaves nothing behind."""
from __future__ import annotations

import pytest

pytest.importorskip("rdkit")

from ms_flow.payloads import Payload

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


def test_rejected_molecule_yields_no_row_and_no_file(tmp_path):
    payload = _payload(
        tmp_path,
        [(SMALL, "small"), (BIG, "big")],
        {"target_molecule_kinds": ["small_molecule"], "max_heavy_atoms": 12},
    )
    rows = materialize_import_batch(payload)

    assert [row["name"] for row in rows] == ["small"]
    # The survivor carries its bytes in the row; no molecule file is written for anyone.
    assert not list((tmp_path / "data").rglob("*.sdf"))


def test_fragments_are_listed_not_written_and_the_row_carries_its_payloads(tmp_path):
    rows = materialize_import_batch(_payload(tmp_path, [(SMALL, "small")], None))

    row = rows[0]
    fragments = row["extra_data"]["fragmentation"]["components"]
    assert len(fragments) == 2 and all(set(f) == {"fragment_index", "role", "smiles"} for f in fragments)
    assert "current_path" not in row and "stored_path" not in row
    assert isinstance(row["stored_payload_id"], Payload) and row["stored_payload_id"].kind == "sdf"
    assert isinstance(row["current_payload_id"], Payload) and row["current_payload_id"].kind == "sdf"
    assert not list((tmp_path / "data").rglob("*.sdf"))
    assert row["canonical_smiles"] is None and row["inchikey"] is None  # a library row: identity on demand
