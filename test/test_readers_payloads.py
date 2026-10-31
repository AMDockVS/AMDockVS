"""Diversity, QSAR and pocket prediction read a molecule's structure from its payload."""
from __future__ import annotations

from types import SimpleNamespace

from ms_flow.core.database.project import ProjectStore
from payload_seed import seed_payload
from rdkit import Chem

from amdockvs.models import MoleculeRecord

PDB = (
    b"ATOM      1  N   GLY A   1       0.000   0.000   0.000  1.00  0.00           N\nEND\n"
)


def _molblock(smiles: str) -> bytes:
    return Chem.MolToMolBlock(Chem.MolFromSmiles(smiles)).encode()


def _project(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    stored = seed_payload(store, _molblock("CCO"), "sdf")
    current = seed_payload(store, _molblock("CCC"), "sdf")
    receptor = seed_payload(store, PDB, "pdb")
    with store.get_session() as session:
        session.add(MoleculeRecord(id=1, name="lig", is_ligand=True, molecule_type="small_molecule",
                                   stored_payload_id=stored, current_payload_id=current))
        session.add(MoleculeRecord(id=2, name="no structure", is_ligand=True, molecule_type="small_molecule"))
        session.add(MoleculeRecord(id=3, name="rec", is_receptor=True, has_3d=True, stored_payload_id=receptor))
        session.commit()
    return store


def test_diversity_scope_skips_molecules_without_a_structure_and_fingerprints_the_current_one(tmp_path):
    from amdockvs.chemistry.fingerprints import morgan_fingerprint
    from amdockvs.diversity.jobs import (
        SelectionClusterJobParams,
        _fingerprint_from_structure,
        scope_molecule_rows,
    )

    store = _project(tmp_path)
    rows = list(scope_molecule_rows(store, SelectionClusterJobParams()))
    assert [row["id"] for row in rows] == [1]  # id 2 has no payload

    fp = _fingerprint_from_structure(store, rows[0], radius=2, nbits=1024)
    assert fp is not None and len(fp) == 1024
    propane = morgan_fingerprint(Chem.MolFromSmiles("CCC"), radius=2, n_bits=1024).ToBitString().encode()
    assert fp == propane  # the current structure, not the imported ethanol
    store.dispose()


def test_qsar_readers_parse_the_payload_bytes(tmp_path):
    from amdockvs.qsar.activities import ActivitiesMixin
    from amdockvs.qsar.api import QSARAPI
    from amdockvs.qsar.splitters import scaffold_groups

    store = _project(tmp_path)
    runtime = SimpleNamespace(workspace=SimpleNamespace(project_db=store), _require_active_project=lambda: None)
    api = QSARAPI(runtime)
    with store.get_session() as session:
        records = [session.get(MoleculeRecord, 1)]
        mol = api._ligand_mol(session, records[0])
        inchikey_to_id, smiles_to_id = ActivitiesMixin._structural_ligand_index(api, session)
    assert scaffold_groups([mol]) == [""]  # propane has no ring: parsed, scaffold empty
    assert mol.GetNumHeavyAtoms() == 3  # the current structure (propane), not the imported ethanol
    assert 1 in inchikey_to_id.values() and 1 in smiles_to_id.values()  # keys recovered from the bytes
    store.dispose()


def test_p2rank_feed_hands_the_receptor_as_a_file_with_the_payload_bytes(tmp_path):
    from amdockvs.binding_sites.jobs import p2rank_prediction_job

    store = _project(tmp_path)
    chunk, = p2rank_prediction_job.build_chunks(
        {"receptor_ids": [3], "recalculate_receptor_ids": [3], "run_id": "r1", "p2rank_command": "prank", "java_command": "java", "output_dir": str(tmp_path / "out")},
        config={"project_db": store},
    )
    path = chunk["receptor_path"]
    assert chunk["receptor_id"] == 3 and str(path).endswith(".pdb")
    assert open(getattr(path, "path", path), "rb").read() == PDB
    store.dispose()


def test_box_from_a_reference_ligand_reads_its_payload(tmp_path):
    from amdockvs.binding_sites.api import BindingSiteAPI

    store = _project(tmp_path)
    runtime = SimpleNamespace(workspace=SimpleNamespace(project_db=store), _require_active_project=lambda: None)
    api = BindingSiteAPI(runtime)
    box = api.suggest_box_from_ligand(ligand_id=3)  # one atom at the origin: the box is centred there
    assert list(box["center"]) == [0.0, 0.0, 0.0]
    assert "path" not in api.list_receptors()[0]
    store.dispose()
