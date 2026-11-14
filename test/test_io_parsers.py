import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "//src")

from amdockvs.io.parsers import count_import_records, iter_import_entries


def test_count_import_records_counts_sdf_smiles_and_single_structure(tmp_path):
    pytest.importorskip("rdkit")
    from rdkit import Chem

    sdf_path = tmp_path / "molecules.sdf"
    writer = Chem.SDWriter(str(sdf_path))
    for name, smiles in (("mol1", "CCO"), ("mol2", "CCN")):
        mol = Chem.MolFromSmiles(smiles)
        assert mol is not None
        mol.SetProp("_Name", name)
        writer.write(mol)
    writer.close()
    smiles_path = tmp_path / "ligands.smi"
    smiles_path.write_text("# comment\nCCO lig1\n\nCCN lig2\n", encoding="utf-8")
    pdb_path = tmp_path / "receptor.pdb"
    pdb_path.write_text("ATOM      1  N   MET A   1\nEND\n", encoding="utf-8")

    assert count_import_records(sdf_path) == 2
    assert count_import_records(smiles_path) == 2
    assert count_import_records(pdb_path) == 1


def test_count_import_records_approx_matches_exact_within_a_few_percent(tmp_path):
    # A synthetic SDF above the sampling threshold: the extrapolated count must land close
    # to the exact one (it only feeds the progress bar) and small files stay exact.
    record = "mol\n  block\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n$$$$\n"
    big_path = tmp_path / "big.sdf"
    repeats = (40 * 1024 * 1024) // len(record) + 1
    big_path.write_text(record * repeats, encoding="utf-8")

    exact = count_import_records(big_path)
    approx = count_import_records(big_path, approx=True)
    assert exact == repeats
    # Deliberately a slight under-count (declared chunk totals must stay reachable), but close.
    assert 0.90 < approx / exact <= 1.0

    small_path = tmp_path / "small.sdf"
    small_path.write_text(record * 3, encoding="utf-8")
    assert count_import_records(small_path, approx=True) == 3


def test_iter_import_entries_returns_expected_format_and_payloads(tmp_path):
    pytest.importorskip("rdkit")
    from rdkit import Chem

    sdf_path = tmp_path / "molecules.sdf"
    writer = Chem.SDWriter(str(sdf_path))
    mol = Chem.MolFromSmiles("CCO")
    assert mol is not None
    mol.SetProp("_Name", "mol1")
    writer.write(mol)
    writer.close()
    smiles_path = tmp_path / "ligands.smi"
    smiles_path.write_text("CCO lig1\nCCN lig2\n", encoding="utf-8")
    pdb_path = tmp_path / "receptor.pdb"
    pdb_path.write_text("ATOM      1  N   MET A   1\nEND\n", encoding="utf-8")

    sdf_format, sdf_entries = iter_import_entries(kind="ligand", file_path=sdf_path)
    smiles_format, smiles_entries = iter_import_entries(kind="ligand", file_path=smiles_path)
    pdb_format, pdb_entries = iter_import_entries(kind="receptor", file_path=pdb_path)

    sdf_rows = list(sdf_entries)
    smiles_rows = list(smiles_entries)
    pdb_rows = list(pdb_entries)

    assert sdf_format == "sdf"
    assert sdf_rows[0]["source_index"] == 0
    assert sdf_rows[0]["name"] == "mol1"
    assert "mol_block" in sdf_rows[0]
    assert "M  END" in sdf_rows[0]["mol_block"]

    assert smiles_format == "smiles"
    assert smiles_rows[0] == {
        "source_index": 0,
        "smiles": "CCO",
        "name": "lig1",
        "mol_block": smiles_rows[0]["mol_block"],
    }
    assert smiles_rows[1] == {
        "source_index": 1,
        "smiles": "CCN",
        "name": "lig2",
        "mol_block": smiles_rows[1]["mol_block"],
    }
    assert "M  END" in smiles_rows[0]["mol_block"]
    assert "M  END" in smiles_rows[1]["mol_block"]

    assert pdb_format == "pdb"
    assert pdb_rows == [{"source_index": 0, "source_file": str(pdb_path.resolve())}]


def test_declared_chunks_never_exceed_what_the_feed_emits(tmp_path):
    """A job only completes once processed >= declared total_chunks, so over-declaring hangs it."""
    from amdockvs.io.jobs import estimate_import_chunks
    from amdockvs.io.loaders import stream_import_payload_batches

    for records, batch_size in ((1, 1000), (3, 2), (40, 32), (500, 100), (1000, 1000)):
        path = tmp_path / f"lig_{records}_{batch_size}.smi"
        path.write_text("".join(f"CCO lig{i}\n" for i in range(records)), encoding="utf-8")
        emitted = sum(
            1
            for _ in stream_import_payload_batches(
                kind="ligand", file_path=path, storage_dir=tmp_path, batch_size=batch_size
            )
        )
        assert estimate_import_chunks(path, batch_size=batch_size) <= emitted


# --- SMILES tables: quoting, empty cells, column 0 -----------------------------------------


def _table_rows(path):
    _format, entries = iter_import_entries(kind="ligand", file_path=path)
    return [(row["name"], row.get("source_properties") or {}) for row in entries]


def _shard_records(path):
    from amdockvs.io.parsers.readers import iter_raw_records
    from amdockvs.io.shards import smiles_record

    _format, config, records = iter_raw_records(kind="ligand", file_path=path)
    return [smiles_record(record["raw"], config) for record in records]


def test_quoted_csv_cell_keeps_its_comma(tmp_path):
    pytest.importorskip("rdkit")
    path = tmp_path / "quoted.csv"
    path.write_text('SMILES,name,IC50\nCCO,"ethanol, quoted",100\nCCN,"amine, quoted",250\n')

    assert _table_rows(path) == [("ethanol, quoted", {"IC50": "100"}), ("amine, quoted", {"IC50": "250"})]
    assert _shard_records(path) == ["CCO ethanol, quoted", "CCN amine, quoted"]


def test_name_in_column_zero_is_the_name(tmp_path):
    pytest.importorskip("rdkit")
    path = tmp_path / "reordered.csv"
    path.write_text("name,IC50,SMILES\nethanol,100,CCO\namine,250,CCN\n")

    assert _table_rows(path) == [("ethanol", {"IC50": "100"}), ("amine", {"IC50": "250"})]
    assert _shard_records(path) == ["CCO ethanol", "CCN amine"]


def test_empty_tsv_cell_does_not_shift_the_next_column(tmp_path):
    pytest.importorskip("rdkit")
    path = tmp_path / "sparse_activity.tsv"
    path.write_text("SMILES\tname\tIC50\tactive\nCCO\tethanol\t\t1\nCCN\tamine\t250\t0\n")

    assert _table_rows(path) == [("ethanol", {"active": "1"}), ("amine", {"IC50": "250", "active": "0"})]


def test_header_without_a_name_column_keeps_every_value_column(tmp_path):
    pytest.importorskip("rdkit")
    path = tmp_path / "unnamed.csv"
    path.write_text("SMILES,IC50\nCCO,100\n")

    assert _table_rows(path) == [("unnamed_0", {"IC50": "100"})]
    assert _shard_records(path) == ["CCO"]


def test_comma_in_a_space_delimited_name_is_not_a_delimiter(tmp_path):
    pytest.importorskip("rdkit")
    path = tmp_path / "names.smi"
    path.write_text("CCO ethanol,95%\nCCN amine\n")

    assert _table_rows(path) == [("ethanol,95%", {}), ("amine", {})]


def test_parse_config_without_name_col_still_means_column_one():
    pytest.importorskip("rdkit")
    from amdockvs.io.shards import smiles_record
    from amdockvs.io.transformers.smiles import _parse_smiles_line

    legacy = {"delimiter": " ", "smiles_col": 0}
    assert _parse_smiles_line("CCO ethanol\n", legacy, source_index=0, stem="x")["name"] == "ethanol"
    assert smiles_record("CCO ethanol\n", legacy) == "CCO ethanol"
