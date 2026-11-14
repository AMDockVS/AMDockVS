"""A quoted cell in a CSV/TSV ligand table may hold a newline, and the row stays one record.

Worth a test because the import read tables line by line: `CCO,"ethanol,\\nquoted",100` became a
molecule named `ethanol,` and a second line that parsed to nothing, so the name was cut and the
IC50 on that row was lost, through rows and shards alike.
"""
from __future__ import annotations

import io

import pytest

pytest.importorskip("rdkit")

from amdockvs.io.parsers.readers import _split_smiles_spans, count_import_records  # noqa: E402

MULTILINE = 'SMILES,name,IC50\nCCO,"ethanol,\nquoted",100\nCCN,amine,250\n'


def _rows(text: str, delimiter: str, *, header: bool = True) -> list[str]:
    lines = io.BytesIO(text.encode()).readlines()
    return [raw.decode() for raw, _start, _end in _split_smiles_spans(iter(lines), skip_header=header, delimiter=delimiter)]


def test_a_quoted_newline_keeps_the_row_whole():
    assert _rows(MULTILINE, ",") == ['CCO,"ethanol,\nquoted",100', "CCN,amine,250"]


def test_the_offsets_cover_each_row_exactly():
    data = MULTILINE.encode()
    spans = list(_split_smiles_spans(iter(io.BytesIO(data).readlines()), skip_header=True, delimiter=","))
    assert [data[start:end].rstrip(b"\n") for _raw, start, end in spans] == [raw for raw, _s, _e in spans]


@pytest.mark.parametrize(
    "text,rows",
    [
        ('CCO,"say ""hi""\nthere",1\n', ['CCO,"say ""hi""\nthere",1']),  # "" is a literal quote
        ('CCO,5"-x,1\nCCN,amine,2\n', ['CCO,5"-x,1', "CCN,amine,2"]),  # mid-cell quote: literal
        ('CCO, "spaced\nname",1\n', ['CCO, "spaced\nname",1']),  # a cell may start after spaces
    ],
    ids=["doubled-quote", "mid-cell-quote", "leading-space"],
)
def test_quotes_are_read_the_way_csv_reads_them(text, rows):
    assert _rows(text, ",", header=False) == rows


def test_a_tab_table_quotes_too_and_a_smi_does_not():
    assert _rows('CCO\t"two\nlines"\t1\n', "\t", header=False) == ['CCO\t"two\nlines"\t1']
    # Whitespace .smi files are not CSV: a quote in a name is a character, every line a row.
    assert _rows('CCO "odd\nCCN amine\n', " ", header=False) == ['CCO "odd', "CCN amine"]


def test_a_quote_that_never_closes_is_one_malformed_row():
    assert _rows('CCO,"open\nCCN,amine,2\n', ",", header=False) == ['CCO,"open\nCCN,amine,2']


def test_the_record_count_agrees_with_the_splitter(tmp_path):
    path = tmp_path / "multiline.csv"
    path.write_text(MULTILINE, encoding="utf-8")
    assert count_import_records(path) == 2


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from amdockvs.runtime import AMDockVSRuntime

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("AMDOCK_OFFLINE", "1")
    rt = AMDockVSRuntime()
    rt.create_project(name="tables", folder=tmp_path / "project")
    yield rt
    rt.shutdown()


def _completed(runtime, jobs):
    states = runtime.wait_for_jobs(list(jobs), timeout_s=60)
    assert all(str(state.status).endswith("completed") for state in states.values()), states


def test_a_row_import_keeps_the_name_and_the_activity(runtime, tmp_path):
    from sqlmodel import select

    from amdockvs.models import LigandActivity, MoleculeRecord

    path = tmp_path / "multiline.csv"
    path.write_text(MULTILINE, encoding="utf-8")
    _completed(runtime, runtime.loader.load_ligands(
        [str(path)], executor_name="thread", primary_context="reference", prefilter={"activity_property": "IC50"}
    ))
    with runtime.workspace.project_db.get_session() as session:
        names = [row.name for row in session.exec(select(MoleculeRecord).order_by(MoleculeRecord.id)).all()]
        values = sorted(activity.value for activity in session.exec(select(LigandActivity)).all())
    assert names == ["ethanol,\nquoted", "amine"]
    assert values == [100, 250]


def test_a_shard_import_keeps_the_name(runtime, tmp_path):
    from ms_flow.core.data.shard import Shard
    from sqlmodel import select

    from amdockvs.io.shards import mol_from_record
    from amdockvs.models import ScreeningShard

    path = tmp_path / "multiline.csv"
    path.write_text(MULTILINE, encoding="utf-8")
    _completed(runtime, runtime.loader.shard_ligands([str(path)], executor_name="thread"))
    with runtime.workspace.project_db.get_session() as session:
        shard_paths = [row.path for row in session.exec(select(ScreeningShard)).all()]
    names = []
    for shard_path in shard_paths:
        with Shard.open(shard_path) as shard:
            names += [mol_from_record(record.load(), "smiles").GetProp("_Name") for record in shard]
    assert names == ["ethanol,\nquoted", "amine"]


def test_the_sampled_estimate_counts_rows_too(tmp_path, monkeypatch):
    from amdockvs.io.parsers import readers

    path = tmp_path / "big.csv"
    path.write_text("SMILES,name\n" + 'CCO,"two\nlines"\n' * 2000, encoding="utf-8")
    monkeypatch.setattr(readers, "_APPROX_COUNT_MIN_BYTES", 0)  # sample it, as a large file would be
    estimate = count_import_records(path, approx=True)
    assert estimate <= 2000  # a floor: lines would claim ~4000 and the job would never complete
