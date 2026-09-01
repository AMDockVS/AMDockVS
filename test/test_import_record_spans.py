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
