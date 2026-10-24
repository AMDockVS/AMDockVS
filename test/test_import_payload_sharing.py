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
    assert row["current_payload_id"] is row["stored_payload_id"]  # D6: no coordinates, no second form


def test_a_culled_smiles_keeps_its_current_form_as_smiles(tmp_path):
    batch = _batch(tmp_path, "l.smi", "smiles", [{"source_index": 0, "raw": "CCO.[Na+] salt"}])
    batch["input_config"] = {}
    row = materialize_import_batch(batch)[0]
    assert row["stored_payload_id"].data == b"CCO.[Na+]"
    assert row["current_payload_id"].kind == "smi" and row["current_payload_id"].data == b"CCO"


def test_a_receptor_import_lands_in_the_receptors_and_references_series(tmp_path):
    """The imported PDB is a payload of series `receptors`; the frozen cocrystal pair is
    `references`, the ligand row itself is an ordinary molecule."""
    from pathlib import Path

    from amdockvs.io.payloads import ImportBatchPayload
    from amdockvs.io.transformers.structures import _materialize_structure_rows

    het = [l for l in (Path(__file__).parent / "data" / "ccd_ligand.pdb").read_text().splitlines() if l.startswith("HETATM")]
    ca = [f"ATOM  {i:5d}  CA  ALA A{i:4d}    {i * 3.8:8.3f}{0:8.3f}{0:8.3f}  1.00 20.00           C" for i in range(1, 6)]
    source = tmp_path / "rec.pdb"
    source.write_text("\n".join(ca + het + ["END", ""]), encoding="utf-8")
    batch = ImportBatchPayload(
        kind="receptor", file_path=source, storage_dir=tmp_path, input_format="pdb",
        primary_role="receptor", molecule_kind="protein",
    )
    receptor, ligand = list(_materialize_structure_rows(batch=batch, entries=[]))

    assert receptor["stored_payload_id"].series == receptor["current_payload_id"].series == "receptors"
    assert receptor["stored_payload_id"].data == source.read_bytes()  # the input as-is
    assert ligand["stored_payload_id"] is ligand["current_payload_id"]
    assert ligand["stored_payload_id"].series == "molecules"
    spec = ligand["complex_spec"]
    assert spec["reference_receptor_payload_id"].series == spec["reference_ligand_payload_id"].series == "references"
    graph = build_import_graph_payload([receptor, ligand])
    assert graph["complexes"][0]["reference_ligand_payload_id"] is spec["reference_ligand_payload_id"]


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
