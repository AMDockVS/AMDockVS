import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "//src")

from amdockvs.io.payloads import ImportBatchPayload, ImportPrefilterPolicy
from amdockvs.io.transformers import build_import_graph_payload, materialize_import_batch
from amdockvs.io.transformers.materializers import _active_binding_site_position


def test_import_prefilter_policy_uses_small_molecule_target_by_default():
    policy = ImportPrefilterPolicy.model_validate({"max_atoms": 12})

    assert policy.applies_to("small_molecule") is True
    assert policy.applies_to("protein") is False
    assert policy.to_mapping() == {
        "target_molecule_kinds": ["small_molecule"],
        "criteria": {"rules": [{"field": "heavy_atom_count", "operator": "lte", "value": 12}]},
    }


def test_materialize_import_batch_prefilter_depends_on_molecule_kind_not_role(tmp_path):
    pytest.importorskip("rdkit")

    from rdkit import Chem

    source_path = tmp_path / "lig_like_input.smi"
    source_path.write_text("CCO LigAsProtein\n", encoding="utf-8")
    storage_dir = tmp_path / "storage"

    mol = Chem.MolFromSmiles("CCO")
    assert mol is not None

    payload = ImportBatchPayload(
        kind="molecule",
        file_path=source_path,
        storage_dir=storage_dir,
        input_format="smiles",
        primary_role="ligand",
        primary_context="screening",
        molecule_kind="protein",
        prefilter={"max_atoms": 1},
        entries=[
            {
                "source_index": 0,
                "smiles": "CCO",
                "name": "LigAsProtein",
                "mol_block": Chem.MolToMolBlock(mol),
            }
        ],
    ).model_dump(mode="json")

    rows = materialize_import_batch(payload)

    assert len(rows) == 1
    assert rows[0]["primary_role"] == "ligand"
    assert rows[0]["molecule_kind"] == "protein"

    graph_payload = build_import_graph_payload(rows)

    # Ligands/receptors are no longer separate node lists: the graph is molecule-centric
    # with is_ligand/is_receptor flags. The point stands — role=ligand + kind=protein is
    # materialized as a ligand molecule carrying the protein kind (role independent of kind).
    assert len(graph_payload["molecules"]) == 1
    assert graph_payload["molecules"][0]["is_ligand"] is True
    assert graph_payload["molecules"][0]["is_receptor"] is False
    assert graph_payload["molecules"][0]["molecule_type"] == "protein"


def test_active_binding_site_defaults_to_single_selected_reference_ligand():
    specs = [
        {"source": "ligand", "source_ref": "A:LIG:1"},
        {"source": "ligand", "source_ref": "B:LIG:2"},
    ]

    assert _active_binding_site_position(specs, reference_ligands=["B:LIG:2"]) == 1


def test_active_binding_site_defaults_to_only_ligand_site():
    specs = [
        {"source": "ligand", "source_ref": "A:LIG:1"},
        {"source": "metal", "source_ref": "A:ZN:2"},
    ]

    assert _active_binding_site_position(specs) == 0


def test_active_binding_site_stays_unset_for_multiple_reference_ligands():
    specs = [
        {"source": "ligand", "source_ref": "A:LIG:1"},
        {"source": "ligand", "source_ref": "B:LIG:2"},
    ]

    assert _active_binding_site_position(specs, reference_ligands=["A:LIG:1", "B:LIG:2"]) is None
