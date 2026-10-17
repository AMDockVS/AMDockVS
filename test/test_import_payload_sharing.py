"""D11: an input that is also the current form is one payload, not two; the original is kept as
SMILES or as the input as-is, never as a re-rendered copy."""
from __future__ import annotations

import pytest

pytest.importorskip("rdkit")

from rdkit import Chem
from rdkit.Chem import AllChem
from sqlmodel import select

from amdockvs.io.jobs import IMPORT_GRAPH_OUTPUT
from amdockvs.io.transformers.materializers import build_import_graph_payload, materialize_import_batch
from amdockvs.models import MoleculeRecord
from ms_flow.core.database import ProjectStore


def _batch(tmp_path, source: str, input_format: str, entries: list[dict], role="ligand", kind="small_molecule"):
    path = tmp_path / source
    path.write_text("", encoding="utf-8")
    return {
        "kind": role,
        "file_path": str(path),
        "storage_dir": str(tmp_path / "data"),
        "input_format": input_format,
        "primary_role": role,
        "molecule_kind": kind,
        "entries": entries,
    }


def _sdf_record(smiles: str, name: str) -> str:
    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    AllChem.EmbedMolecule(mol, randomSeed=7)  # 3D, so the import also writes a model row
    mol.SetProp("_Name", name)
    return Chem.MolToMolBlock(mol) + "$$$$\n"


def _sdf_row(tmp_path, smiles: str) -> dict:
    record = _sdf_record(smiles, "m")
    return materialize_import_batch(_batch(tmp_path, "l.sdf", "sdf", [{"source_index": 0, "raw": record}]))[0]


def test_an_input_equal_to_the_current_form_is_one_payload_object(tmp_path):
    row = _sdf_row(tmp_path, "CCO")
    assert row["stored_payload_id"] is row["current_payload_id"]
    models = build_import_graph_payload([row])["molecule_models"]
    assert models[0]["payload_id"] is row["stored_payload_id"]


def test_a_changed_current_form_keeps_the_original_apart(tmp_path):
    row = _sdf_row(tmp_path, "CCO.[Na+]")
    assert row["stored_payload_id"] is not row["current_payload_id"]
    assert b"Na" in row["stored_payload_id"].data and b"Na" not in row["current_payload_id"].data


def test_a_smiles_import_keeps_the_smiles_as_the_original(tmp_path):
    batch = _batch(tmp_path, "l.smi", "smiles", [{"source_index": 0, "raw": "CCO ethanol"}])
    batch["input_config"] = {}
    row = materialize_import_batch(batch)[0]
    assert row["stored_payload_id"].kind == "smi" and row["stored_payload_id"].data == b"CCO"
    assert row["current_payload_id"].kind == "sdf"


@pytest.mark.xfail(
    strict=False,
    reason="ms_flow's PayloadStore sink stores each Payload occurrence; it needs identity dedupe "
    "in project_store_ops._put_payloads before one object in two columns gets one id",
)
def test_persisted_equal_forms_share_one_id(tmp_path):
    graph = build_import_graph_payload([_sdf_row(tmp_path, "CCO")])
    store = ProjectStore.open_at(tmp_path / "project.db")
    try:
        store.persist_output_spec(IMPORT_GRAPH_OUTPUT, graph)
        with store.get_session() as session:
            molecule = session.exec(select(MoleculeRecord)).one()
        assert molecule.stored_payload_id == molecule.current_payload_id
    finally:
        ProjectStore.clear_cached_stores()
