"""The import chunk travels as a byte range, not as a copy of the text.

What needs protecting: the range the feed emits yields, when re-read, exactly the same records
that used to be embedded in the payload.
"""
from amdockvs.io.loaders import stream_import_payload_batches
from amdockvs.io.parsers import iter_raw_records, iter_record_spans, read_record_span

SDF = "".join(
    f"mol{i}\n  fake\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n> <p>\n{i}\n\n$$$$\n"
    for i in range(1, 26)
)
SMI = "smiles name\n" + "".join(f"C{'C' * (i % 5)}\tlig{i}\n" for i in range(1, 26))


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def _round_trip(path, kind):
    _fmt, _cfg, raw_iter = iter_raw_records(kind=kind, file_path=path)
    expected = list(raw_iter)
    input_format, _cfg, span_iter = iter_record_spans(kind=kind, file_path=path)
    spans = list(span_iter)
    assert len(spans) == len(expected)
    rebuilt = read_record_span(
        file_path=path,
        input_format=input_format,
        offset=spans[0]["offset"],
        end=spans[-1]["end"],
        first_index=spans[0]["source_index"],
    )
    return expected, rebuilt


def test_sdf_spans_rebuild_the_same_records(tmp_path):
    expected, rebuilt = _round_trip(_write(tmp_path, "lib.sdf", SDF), "ligand")
    assert rebuilt == expected


def test_smiles_spans_skip_the_header_and_rebuild_the_same_records(tmp_path):
    expected, rebuilt = _round_trip(_write(tmp_path, "lib.smi", SMI), "ligand")
    assert rebuilt == expected
    assert all("smiles name" not in row["raw"] for row in rebuilt)


def test_a_middle_span_rebuilds_only_its_own_records(tmp_path):
    path = _write(tmp_path, "lib.sdf", SDF)
    input_format, _cfg, span_iter = iter_record_spans(kind="ligand", file_path=path)
    spans = list(span_iter)
    window = spans[7:13]
    rebuilt = read_record_span(
        file_path=path,
        input_format=input_format,
        offset=window[0]["offset"],
        end=window[-1]["end"],
        first_index=window[0]["source_index"],
    )
    assert [row["source_index"] for row in rebuilt] == [7, 8, 9, 10, 11, 12]
    assert rebuilt[0]["raw"].startswith("mol8")


def test_chunk_payloads_carry_the_span_and_not_the_library(tmp_path):
    path = _write(tmp_path, "lib.sdf", SDF)
    chunks = list(
        stream_import_payload_batches(
            kind="ligand",
            file_path=path,
            storage_dir=tmp_path / "storage",
            batch_size=10,
        )
    )
    assert [chunk["span_count"] for chunk in chunks] == [10, 10, 5]
    assert all(chunk["records"] == [] for chunk in chunks)
    # Contiguous and covering the whole file: no record is lost between two chunks.
    assert chunks[0]["span_offset"] == 0
    assert chunks[-1]["span_end"] == len(SDF)
    assert [chunk["span_first_index"] for chunk in chunks] == [0, 10, 20]


def _mol2_pose(name, x):
    # DOCK-style: the score header sits above the MOLECULE line it belongs to.
    return (
        f"##########                 Name:   {name}\n"
        f"##########         Total Energy:   -{x}.5\n\n"
        f"@<TRIPOS>MOLECULE\n   {name}      none\n    2     1     0     0     0\nSMALL\nUSER_CHARGES\n\n\n"
        "@<TRIPOS>ATOM\n"
        f"      1 C1      {x}.0000    0.0000    0.0000 C.3       1 LIG     0.0000\n"
        f"      2 O1      {x}.4000    0.0000    0.0000 O.3       1 LIG     0.0000\n"
        "@<TRIPOS>BOND\n     1    1    2 1\n\n"
    )


MOL2 = "".join(_mol2_pose(f"lig{i}", i) for i in range(1, 6))


def test_mol2_spans_keep_each_dock_header_with_its_own_molecule(tmp_path):
    from amdockvs.io.formats import read_mol, read_mols
    from amdockvs.io.parsers import count_import_records

    path = _write(tmp_path, "poses.mol2", MOL2)
    expected, rebuilt = _round_trip(path, "ligand")
    assert rebuilt == expected
    assert "".join(row["raw"] for row in expected) == MOL2
    assert count_import_records(path) == 5
    mols = list(read_mols(path))
    assert [m.GetProp("_Name") for m in mols] == [f"lig{i}" for i in range(1, 6)]
    assert [m.GetProp("Total Energy") for m in mols] == [f"-{i}.5" for i in range(1, 6)]
    assert read_mol(path, index=2).GetProp("_Name") == "lig3"
    # A receptor mol2 is still one structure, not a library.
    receptor_chunks = list(
        stream_import_payload_batches(kind="receptor", file_path=path, storage_dir=tmp_path / "s", batch_size=2)
    )
    assert len(receptor_chunks) == 1 and receptor_chunks[0]["span_count"] == 0


def test_sdf_offsets_match_the_line_splitter(tmp_path):
    from amdockvs.io.parsers.readers import _sdf_offsets, _split_sdf_spans

    cases = [
        SDF,
        "",
        "$$$$\n" + SDF,
        SDF.replace("\n", "\r\n"),
        SDF + "tail\n  no end\n",
        SDF + "\n\n",
        SDF.rstrip("\n"),  # last $$$$ without newline
        "a\n$$$$x\nb\n $$$$\n$$$$\n",
        "a\n" + " " * 50 + "\n",  # whitespace-only tail
        "a\n$$$$\n" + " " * 50 + "b\n",  # text-bearing tail behind whitespace
    ]
    for i, text in enumerate(cases):
        path = tmp_path / f"c{i}.sdf"
        path.write_bytes(text.encode())
        with path.open("rb") as handle:
            expected = [(start, end) for _raw, start, end in _split_sdf_spans(handle)]
        for block in (1, 3, 7, 1 << 20):  # tiny blocks: every record and separator cut by an edge
            assert list(_sdf_offsets(path, block)) == expected, (block, repr(text[:40]))


def test_a_short_malformed_tail_is_still_a_record_whatever_the_block_size(tmp_path):
    """PR #111 review: after the 4 bytes kept across a block edge, `\\nbad\\n` after the last record
    was sliced with a negative index and lost, so the import reported no unreadable record."""
    import itertools

    from amdockvs.io.parsers.readers import _sdf_offsets, _split_sdf_spans

    record = "mol\n  fake\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n$$$$\n"
    tails = ("", "\n", "\nbad\n", "bad\n", "bad", "\n x \n", "\n\n\n\nbad\n", "  \n", "a\n$$$$")
    path = tmp_path / "lib.sdf"
    for count, tail, block in itertools.product((1, 32), tails, (1, 3, 5, 16, 1 << 20)):
        path.write_text(record * count + tail)
        with path.open("rb") as handle:
            expected = [(start, end) for _record, start, end in _split_sdf_spans(iter(handle))]
        assert list(_sdf_offsets(path, block=block)) == expected, (count, tail, block)


def _plan(path, **caps):
    return [(c["span_first_index"], c["span_count"], c["span_offset"], c["span_end"])
            for c in stream_import_payload_batches(kind="ligand", file_path=path, storage_dir=path.parent / "s",
                                                   **caps)]


def _reference_plan(path, *, batch_size, chunk_bytes, ramp):
    """The per-record planner as it was (#M7): the oracle the block planner must match."""
    from amdockvs.io.loaders import IMPORT_RAMP_UP_SIZES

    _fmt, _cfg, spans = iter_record_spans(kind="ligand", file_path=path)
    out, batch, size = [], [], 0
    for record in spans:
        n = record["end"] - record["offset"]
        cap = min(batch_size, IMPORT_RAMP_UP_SIZES[len(out)] if ramp and len(out) < len(IMPORT_RAMP_UP_SIZES)
                  else batch_size)
        if batch and (size + n > chunk_bytes or len(batch) >= cap):
            out.append((batch[0]["source_index"], len(batch), batch[0]["offset"], batch[-1]["end"]))
            batch, size = [], 0
        batch.append(record)
        size += n
    if batch:
        out.append((batch[0]["source_index"], len(batch), batch[0]["offset"], batch[-1]["end"]))
    return out


SMILES_CASES = {
    "lib.smi": SMI,
    "crlf.smi": SMI.replace("\n", "\r\n"),
    "no_end.smi": SMI.rstrip("\n"),
    "gaps.smi": "smiles name\n\n   \nCCO a\n# note\n  # indented note\n\tCCN b\n \n" + "C c\n" * 40 + "\n\n",
    "only_header.smi": "smiles name\n",
    "empty.smi": "",
    "quoted.csv": "smiles,name\n" + "CC,a\n" * 30 + 'CCO,"ethanol,\nquoted"\n' + "CN,b\n" * 30,
    "quote_in_a_space_table.smi": 'smiles name\n' + 'CC 5"-x\n' * 20,
}


def test_the_block_planner_gives_the_per_record_chunks(tmp_path, monkeypatch):
    """Same chunks as the per-record planner, whatever the caps and the block edges."""
    import itertools

    from amdockvs.io.parsers import readers

    for name, text in SMILES_CASES.items():
        path = tmp_path / name
        path.write_bytes(text.encode())
        for batch_size, chunk_bytes, ramp in itertools.product((1, 3, 1024), (1, 20, 4 << 20), (True, False)):
            expected = _reference_plan(path, batch_size=batch_size, chunk_bytes=chunk_bytes, ramp=ramp)
            for block in (1, 7, 64, 16 << 20):
                monkeypatch.setattr(readers, "_OFFSET_BLOCK", block)
                got = _plan(path, batch_size=batch_size, chunk_bytes=chunk_bytes, ramp=ramp)
                assert got == expected, (name, batch_size, chunk_bytes, ramp, block)


def test_a_smiles_plan_has_no_python_step_per_line(tmp_path, monkeypatch):
    """1M SMILES took the parent ~1 s line by line, ~0.05 s with numpy (M7 gate, 2026-11-25)."""
    from amdockvs.io.parsers import readers

    path = tmp_path / "lib.smi"
    path.write_text("smiles name\n" + "CCO x\n" * 5000)

    def per_line(*_a, **_k):
        raise AssertionError("the planner split the table line by line")

    monkeypatch.setattr(readers, "_split_smiles_spans", per_line)
    assert [count for _first, count, _o, _e in _plan(path, batch_size=1024, chunk_bytes=4 << 20,
                                                     ramp=False)] == [1024] * 4 + [904]
