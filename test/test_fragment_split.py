"""`split_fragments`: the co-components of a record become molecules of their own.

Worth a test because the rule is "distinct organic fragments, minus the one already kept" —
getting it wrong either duplicates every ligand or silently imports chlorides as candidates.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

Chem = pytest.importorskip("rdkit.Chem")

from amdockvs.molecules.fragments import extra_fragment_molecules
from amdockvs.core.paths import molecule_storage_key


def _smiles(extras):
    return sorted(Chem.MolToSmiles(mol) for _index, mol in extras)


def test_a_single_fragment_record_yields_nothing_extra():
    assert extra_fragment_molecules(Chem.MolFromSmiles("c1ccccc1O")) == []


def test_two_real_fragments_yield_the_one_that_is_not_kept():
    pair = Chem.MolFromSmiles("c1ccccc1C(=O)Nc1ccccc1.c1ccc2ccccc2c1O")  # both drug-sized
    assert _smiles(extra_fragment_molecules(pair)) == ["Oc1cccc2ccccc12"]


def test_a_counterion_is_not_a_molecule_of_its_own():
    # Acetate next to a drug-sized fragment: organic, but a fraction of its size.
    salt = Chem.MolFromSmiles("c1ccccc1C(=O)Nc1ccccc1.CC(=O)[O-]")
    assert extra_fragment_molecules(salt) == []


def test_ions_and_repeats_are_skipped():
    mol = Chem.MolFromSmiles("c1ccccc1C(=O)Nc1ccccc1.[Cl-].[Cl-].c1ccc2ccccc2c1O.c1ccc2ccccc2c1O")
    assert _smiles(extra_fragment_molecules(mol)) == ["Oc1cccc2ccccc12"]


def test_each_extra_gets_its_own_storage_key():
    base = molecule_storage_key("ligand", Path("/tmp/lib.sdf"), 7)
    assert molecule_storage_key("ligand", Path("/tmp/lib.sdf"), 7, "frag01") != base
    assert molecule_storage_key("ligand", Path("/tmp/lib.sdf"), 7) == base  # unchanged without a variant


def test_split_fragments_materializes_the_co_component_as_its_own_row(tmp_path):
    """End-to-end through the materializer: one record in, two molecules out."""
    from amdockvs.io.transformers.materializers import materialize_import_batch

    mol = Chem.MolFromSmiles("c1ccccc1C(=O)Nc1ccccc1.c1ccc2ccccc2c1O")
    mol.SetProp("_Name", "pair")
    record = Chem.MolToMolBlock(mol) + "$$$$\n"
    sdf = tmp_path / "ligands.sdf"
    sdf.write_text(record, encoding="utf-8")
    payload = {
        "kind": "ligand",
        "file_path": str(sdf),
        "storage_dir": str(tmp_path / "data" / "ligands"),
        "input_format": "sdf",
        "primary_role": "ligand",
        "molecule_kind": "small_molecule",
        "prefilter": {"target_molecule_kinds": ["small_molecule"], "split_fragments": True},
        "entries": [{"source_index": 0, "raw": record}],
    }

    rows = materialize_import_batch(payload)
    assert [row["name"] for row in rows] == ["pair", "pair [frag02]"]
    # Separate payloads, not one shared: the extra is its own structure.
    structure = [row["current_payload_id"] or row["stored_payload_id"] for row in rows]
    assert structure[0].data != structure[1].data
    # The extra points back at the record's own molecule, by the index its parent lists it under.
    link = rows[1]["extra_data"]["split_from"]
    assert link["parent_name"] == "pair" and link["source_index"] == 0
    parent_components = rows[0]["extra_data"]["fragmentation"]["components"]
    assert link["fragment_index"] in {c["fragment_index"] for c in parent_components}

    payload["prefilter"] = {"target_molecule_kinds": ["small_molecule"]}
    assert [row["name"] for row in materialize_import_batch(payload)] == ["pair"]


def _smiles_batch(tmp_path, *, split: bool):
    smi = tmp_path / "pair.smi"
    smi.write_text("c1ccccc1C(=O)Nc1ccccc1.c1ccc2ccccc2c1O pair\n", encoding="utf-8")
    return {
        "kind": "ligand",
        "file_path": str(smi),
        "storage_dir": str(tmp_path / "data" / "ligands"),
        "input_format": "smiles",
        "primary_role": "ligand",
        "molecule_kind": "small_molecule",
        "prefilter": {"target_molecule_kinds": ["small_molecule"], "split_fragments": split},
        "entries": [{"source_index": 0, "raw": "c1ccccc1C(=O)Nc1ccccc1.c1ccc2ccccc2c1O pair"}],
    }


def test_the_graph_gives_the_fragment_a_ref_of_its_own(tmp_path):
    """The real failure mode: parent and fragment share source_file/source_index, so without the
    variant the graph carries two 'molecules' nodes under one $ref and the insert is rejected
    (DataContractError: duplicate ref) - the whole import job fails and nothing is persisted.
    """
    from amdockvs.io.transformers.materializers import build_import_graph_payload, materialize_import_batch

    rows = materialize_import_batch(_smiles_batch(tmp_path, split=True))
    assert [row["name"] for row in rows] == ["pair", "pair [frag02]"]
    assert [row["source_variant"] for row in rows] == ["", "frag02"]

    refs = [molecule["$ref"] for molecule in build_import_graph_payload(rows)["molecules"]]
    assert len(refs) == len(set(refs)) == 2
    # The record's own molecule keeps the plain ref; only the extra fragment is suffixed.
    assert refs[0].endswith("::0") and refs[1].endswith("::0::frag02")


def test_one_record_keeps_one_plain_ref_without_the_option(tmp_path):
    from amdockvs.io.transformers.materializers import build_import_graph_payload, materialize_import_batch

    rows = materialize_import_batch(_smiles_batch(tmp_path, split=False))
    refs = [molecule["$ref"] for molecule in build_import_graph_payload(rows)["molecules"]]
    assert len(refs) == 1 and refs[0].endswith("::0")


def test_sibling_rows_do_not_duplicate_the_records_sdf_tags(tmp_path):
    """Both rows read the batch's tag parquet by source_index, which they share: the record's
    tags belong in it once, not once per sibling."""
    import io

    import pyarrow.parquet as pq

    from amdockvs.io.transformers.materializers import offload_source_properties

    tags = [{"key": "IC50", "value_text": "12.5"}]
    rows = [
        {"source_index": 0, "source_properties": list(tags)},
        {"source_index": 0, "source_properties": list(tags)},  # the split fragment
    ]
    payload = offload_source_properties(rows)
    table = pq.read_table(io.BytesIO(payload.data)).to_pydict()
    assert list(zip(table["source_index"], table["key"], table["value"])) == [(0, "IC50", "12.5")]


def test_split_fragments_alone_is_enough_to_keep_the_gui_policy():
    """With every filter off and only Split fragments on, the policy still has to reach the
    importer - dropped to None, the option silently never happens."""
    from amdockvs.io.payloads import ImportPrefilterPolicy

    assert ImportPrefilterPolicy(split_fragments=True).changes_import()
    assert not ImportPrefilterPolicy().changes_import()
    # It is not an in-memory prep step: it adds rows, it does not change the kept fragment.
    assert not ImportPrefilterPolicy(split_fragments=True).prep_active()

    pytest.importorskip("PySide6")
    from amdockvs.ui.tools.importing.ligand_forms import finalize_import_prefilter_policy

    off = {"target_molecule_kinds": ["small_molecule"], "max_rotatable_bonds": None}
    assert finalize_import_prefilter_policy(dict(off)) is None
    assert finalize_import_prefilter_policy({**off, "split_fragments": True})["split_fragments"] is True
