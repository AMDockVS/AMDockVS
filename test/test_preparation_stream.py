"""The preparation feed chunks lazily and filters out what is already prepared.

Replaces test_molecule_stream.py: MoleculeStream was deleted and its chunking/filtering are now
`itertools.batched` and the built-in `filter`, so what is left to protect is the consumer.
"""
import pytest
from ms_flow.core.database.project import ProjectStore

from amdockvs.docking.preparation import jobs as preparation_jobs
from amdockvs.docking.preparation.jobs import PreparationJobParams, _iter_preparation_batches
from payload_seed import seed_payload


@pytest.fixture
def store(tmp_path):
    return ProjectStore.open_at(tmp_path / "project.db")


def _fake_rows(pulled: list[int], total: int, structure: int, prepared: int | None):
    def iter_entity_rows(project_db, **kwargs):
        for i in range(1, total + 1):
            pulled.append(i)
            yield {"id": i, "current_payload_id": structure, "prepared_payload_id": prepared}
    return iter_entity_rows


def _batches(monkeypatch, store, *, total, batch_size, force=False, prepared=None):
    pulled: list[int] = []
    structure = seed_payload(store, "CCO\n", "smi")
    monkeypatch.setattr(preparation_jobs, "iter_entity_rows", _fake_rows(pulled, total, structure, prepared))
    chunks = _iter_preparation_batches(
        project_db=store,
        entity_kind="ligand",
        params=PreparationJobParams(batch_size=batch_size, force=force),
    )
    return chunks, pulled


def test_first_chunk_arrives_without_draining_the_library(monkeypatch, store):
    chunks, pulled = _batches(monkeypatch, store, total=1000, batch_size=3)
    first = next(chunks)
    assert [row["id"] for row in first["rows"]] == [1, 2, 3]
    assert pulled == [1, 2, 3]  # nothing beyond the first batch was read
    assert {(row["structure"], row["structure_kind"]) for row in first["rows"]} == {(b"CCO\n", "smi")}


def test_batches_are_bounded_by_element_count(monkeypatch, store):
    chunks, _ = _batches(monkeypatch, store, total=7, batch_size=3)
    assert [len(chunk["rows"]) for chunk in chunks] == [3, 3, 1]


def test_an_empty_scope_still_closes_the_job_with_one_chunk(monkeypatch, store):
    chunks, _ = _batches(monkeypatch, store, total=0, batch_size=3)
    assert [chunk["rows"] for chunk in chunks] == [[]]


def test_already_prepared_rows_are_skipped_unless_forced(monkeypatch, store):
    done = seed_payload(store, "REMARK prepared\n", "pdbqt")
    chunks, _ = _batches(monkeypatch, store, total=5, batch_size=2, prepared=done)
    assert [chunk["rows"] for chunk in chunks] == [[]]

    chunks, _ = _batches(monkeypatch, store, total=5, batch_size=2, force=True, prepared=done)
    assert [len(chunk["rows"]) for chunk in chunks] == [2, 2, 1]


def test_a_ligand_is_prepared_from_its_bytes_and_a_pdbqt_is_not_copied():
    pytest.importorskip("meeko")
    from rdkit import Chem
    from rdkit.Chem import AllChem

    from amdockvs.docking.preparation.tasks import prepare_entities

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(mol, randomSeed=1)
    rows = [
        {"id": 1, "structure": Chem.MolToMolBlock(mol).encode(), "structure_kind": "sdf", "structure_payload_id": 10},
        {"id": 2, "structure": b"REMARK\n", "structure_kind": "pdbqt", "structure_payload_id": 20},
        {"id": 3, "structure": None, "structure_kind": "", "structure_payload_id": None},
    ]
    out = prepare_entities({"entity_kind": "ligand", "rows": rows})
    states = {row["molecule_id"]: row for row in out["engines"]}

    assert states[1]["is_ready"] and states[1]["prepared_payload_id"].kind == "pdbqt"
    assert b"ROOT" in states[1]["prepared_payload_id"].data
    assert states[2]["prepared_payload_id"] == 20  # already the engine's format: the same payload
    assert not states[3]["is_ready"] and "no structure" in states[3]["files"]["error"]
    # the one that failed is excluded with the engine and the error, so docking runs without it
    [failed] = out["molecule_failures"]
    assert failed["id"] == 3 and failed["excluded"]
    assert failed["exclusion_reason"].startswith("Docking preparation (ad4):") and "no structure" in failed["exclusion_reason"]


def test_a_blocked_docking_says_how_many_of_what_and_which():
    from amdockvs.docking.api import _requirements_message

    message = _requirements_message("qvina", {
        "counts": {"ligands_total": 4083, "ligands_missing": 3, "receptors_total": 1, "receptors_missing": 0,
                   "receptor_grids_missing": 0},
        "missing": {"ligands_prepared": [1454, 1518, 3320], "receptors_prepared": [], "receptor_binding_sites": []},
    })
    assert message.startswith("Docking with qvina can't start: 3 of 4083 ligands are not prepared (ids 1454, 1518, 3320)")
    assert "receptor" not in message.split(".")[0]
