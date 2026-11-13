from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterator

from amdockvs.io._common import normalize_kind
from amdockvs.io.formats import normalized_suffix
from amdockvs.core.vocab import FileFormat


# Above this size an exact scan costs ~1s+ per file; callers that only need a progress
# hint (approx=True) extrapolate from the first chunk instead.
_APPROX_COUNT_MIN_BYTES = 32 * 1024 * 1024
_APPROX_COUNT_SAMPLE_BYTES = 4 * 1024 * 1024


def count_import_records(file_path: str | Path, *, approx: bool = False) -> int:
    """Count importable records for a supported source file.

    Raw scan (no RDKit parse): counts $$$$ terminators for SDF, data lines for
    SMILES tables. ~20-80x faster than iterating a supplier and constant memory,
    at the cost of counting a couple of malformed records the parser would drop.

    ``approx=True`` samples the head of a large file and extrapolates by size instead of
    reading it whole — for progress hints only (a 240MB SDF: ~1.5s -> ~25ms).
    """
    source_path = Path(file_path).expanduser().resolve()
    suffix = normalized_suffix(source_path)
    if approx:
        estimate = _sampled_record_count(source_path, suffix)
        if estimate is not None:
            return estimate
    if suffix == ".sdf":
        count = 0
        trailing = False
        with source_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                trailing = bool(line.strip())
                if line.startswith("$$$$"):
                    count += 1
                    trailing = False
        return count + (1 if trailing else 0)
    if suffix == ".mol2":
        with source_path.open("rb") as handle:
            return max(1, sum(1 for line in handle if line.startswith(_MOL2_RECORD)))
    if suffix in {".smi", ".smiles", ".txt", ".csv", ".tsv"}:
        # Rows, not lines: the same splitter the import walks, so a quoted cell holding a
        # newline is one record here too (an overcount declares chunks the feed never sends).
        has_header, delimiter, _smiles_col, _name_col = _sniff_smiles_table(source_path)
        with source_path.open("rb") as handle:
            return sum(1 for _ in _split_smiles_spans(handle, skip_header=has_header, delimiter=delimiter))
    if suffix == ".fasta":
        return len(parse_fasta(source_path.read_text(encoding="utf-8", errors="ignore"), stem=source_path.stem))
    return 1


def parse_fasta(text: str, *, stem: str = "sequence") -> list[tuple[str, str]]:
    """(name, sequence) per FASTA record. Headerless text is one sequence named `stem`.

    Whitespace and digits are dropped, `*` terminators too; `:` survives as the chain separator
    ESMFold uses for complexes (`AAA:BBB`). Empty records are skipped.
    """
    records: list[tuple[str, list[str]]] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if stripped.startswith(">"):
            records.append((stripped[1:].split()[0] if stripped[1:].strip() else "", []))
            continue
        if not records:
            records.append((stem, []))
        records[-1][1].append("".join(ch for ch in stripped.upper() if ch.isalpha() or ch == ":"))
    named: list[tuple[str, str]] = []
    for index, (name, chunks) in enumerate(records):
        sequence = "".join(chunks).strip(":")
        if sequence:
            named.append((name or f"{stem}_{index}", sequence))
    return named


def _iter_fasta_records(source_path: Path) -> Iterator[dict[str, Any]]:
    text = source_path.read_text(encoding="utf-8", errors="ignore")
    for index, (name, sequence) in enumerate(parse_fasta(text, stem=source_path.stem)):
        yield {"source_index": index, "name": name, "raw": sequence}


def _sampled_record_count(source_path: Path, suffix: str) -> int | None:
    """Records extrapolated from the first _APPROX_COUNT_SAMPLE_BYTES, or None when the file
    is small enough to just count exactly."""
    if suffix not in {".sdf", ".smi", ".smiles", ".txt", ".csv", ".tsv"}:
        return None
    try:
        size = source_path.stat().st_size
    except OSError:
        return None
    if size <= _APPROX_COUNT_MIN_BYTES:
        return None
    with source_path.open("rb") as handle:
        sample = handle.read(_APPROX_COUNT_SAMPLE_BYTES)
    # Drop the truncated tail record so the density isn't biased by a partial line.
    cut = sample.rfind(b"\n")
    if cut > 0:
        sample = sample[:cut + 1]
    if suffix == ".sdf":
        in_sample = sample.count(b"$$$$")
    else:
        in_sample = sum(1 for line in sample.splitlines() if line.strip() and not line.startswith(b"#"))
    if in_sample <= 0 or not sample:
        return None
    # ponytail: 5% down-scale. The estimate feeds declared chunk totals, and a job whose
    # declared total is above the chunks the feed really emits never reaches "completed"
    # (ms_flow: processed < total => not terminal). Under-counting only makes the
    # progress bar finish a touch early. Measured sampling error is ~0.2%.
    return max(1, round(0.95 * in_sample * size / len(sample)))


def iter_raw_records(
    *,
    kind: str,
    file_path: str | Path,
) -> tuple[str, dict[str, Any], Iterator[dict[str, Any]]]:
    """Return (input_format, parse_config, iterator of RAW records).

    No molecule parsing here — the feed only slices the file by record delimiter
    (cheap, GIL-light). Each raw record is a ``{"source_index", "raw"}`` dict the
    worker parses with RDKit. `parse_config` carries what the worker needs to
    parse (SMILES delimiter/columns/header).
    """
    source_path = Path(file_path).expanduser().resolve()
    normalized_kind = normalize_kind(kind)
    suffix = normalized_suffix(source_path)

    if suffix == ".sdf":
        return FileFormat.SDF, {}, _iter_raw_from_spans(source_path, FileFormat.SDF, skip_header=False)
    if normalized_kind in {"ligand", "molecule"} and suffix == ".mol2":
        # A receptor mol2 is one structure; a ligand mol2 is a library (DOCK poses, ZINC sets).
        return FileFormat.MOL2, {}, _iter_raw_from_spans(source_path, FileFormat.MOL2, skip_header=False)
    if normalized_kind in {"ligand", "molecule"} and suffix in {".smi", ".smiles", ".txt", ".csv", ".tsv"}:
        has_header, delimiter, smiles_col, name_col = _sniff_smiles_table(source_path)
        header_names = _smiles_header_names(source_path, delimiter) if has_header else []
        config = {
            "has_header": has_header,
            "delimiter": delimiter,
            "smiles_col": smiles_col,
            "name_col": name_col,
            "header_names": header_names,
        }
        return FileFormat.SMILES, config, _iter_raw_from_spans(
            source_path, FileFormat.SMILES, skip_header=has_header, delimiter=delimiter
        )
    if suffix == ".fasta":
        # ponytail: whole-file read, fine for sequence sets; stream by '>' if genome-sized FASTAs show up.
        return FileFormat.FASTA, {}, _iter_fasta_records(source_path)
    return suffix.lstrip(".") or "dat", {}, _iter_single_structure_entries(source_path)


def _iter_raw_from_spans(
    source_path: Path, input_format: str, *, skip_header: bool, delimiter: str = " "
) -> Iterator[dict[str, Any]]:
    with source_path.open("rb") as handle:
        for index, (raw, _start, _end) in enumerate(
            _spans_for(input_format, handle, skip_header=skip_header, delimiter=delimiter)
        ):
            yield {"source_index": index, "raw": raw.decode("utf-8", errors="ignore")}


# --- record splitters ----------------------------------------------------------------------
# A single splitter serves both directions: the feed walks it over the file and keeps only the
# offsets (dropping the text), and the worker walks it over the byte range it was handed and
# recovers exactly the same records. They work in binary because `tell()` in text mode is not a
# usable byte offset.


def _split_sdf_spans(lines: Iterator[bytes]) -> Iterator[tuple[bytes, int, int]]:
    """(record bytes, start offset, end offset) for every molecule in an SDF."""
    buffer: list[bytes] = []
    start = 0
    pos = 0
    for line in lines:
        if not buffer:
            start = pos
        pos += len(line)
        buffer.append(line)
        if line.startswith(b"$$$$"):
            yield b"".join(buffer), start, pos
            buffer = []
    if buffer:
        blob = b"".join(buffer)
        if blob.strip():
            yield blob, start, pos


_MOL2_RECORD = b"@<TRIPOS>MOLECULE"


def _split_mol2_spans(lines: Iterator[bytes]) -> Iterator[tuple[bytes, int, int]]:
    """(record bytes, start offset, end offset) for every molecule in a multi-molecule mol2.

    A record opens at `@<TRIPOS>MOLECULE`, but the comment block right above it (DOCK writes its
    `########## Name: / Total Energy:` scores there) belongs to the molecule below, not above.
    """
    buffer: list[bytes] = []
    start = 0
    pos = 0
    seen_molecule = False
    trailing = 0  # bytes of the comment/blank run at the end of the buffer
    trailing_lines = 0
    for line in lines:
        if line.startswith(_MOL2_RECORD) and seen_molecule:
            head = buffer[: len(buffer) - trailing_lines]
            yield b"".join(head), start, pos - trailing
            buffer = buffer[len(head):]
            start = pos - trailing
        if line.startswith(_MOL2_RECORD):
            seen_molecule = True
        buffer.append(line)
        pos += len(line)
        if not line.strip() or line.startswith(b"#"):
            trailing += len(line)
            trailing_lines += 1
        else:
            trailing = trailing_lines = 0
    if seen_molecule:
        yield b"".join(buffer), start, pos


def _split_smiles_spans(
    lines: Iterator[bytes], *, skip_header: bool, delimiter: str = " "
) -> Iterator[tuple[bytes, int, int]]:
    """(record bytes, start offset, end offset) for every row of a SMILES table.

    A row is a line, except in a comma or tab table, where a quoted cell may hold a newline
    (`CCO,"ethanol,\nquoted",100` is one row): the row runs on until its quotes close.
    """
    quoting = delimiter.encode() if delimiter in {",", "\t"} else b""
    pos = 0
    header_skipped = not skip_header
    buffer: list[bytes] = []
    start = 0
    in_quotes = False
    for line in lines:
        line_start = pos
        pos += len(line)
        if not buffer:
            stripped = line.strip()
            if not stripped or stripped.startswith(b"#"):
                continue
            start = line_start
        buffer.append(line)
        if quoting and (in_quotes or b'"' in line):
            in_quotes = _ends_in_quotes(line, quoting, in_quotes)
            if in_quotes:
                continue
        record, buffer = b"".join(buffer), []
        if not header_skipped:
            header_skipped = True
            continue
        yield record.rstrip(b"\n"), start, pos
    if buffer:  # a quote never closed: the rest is one malformed row, the parser's to refuse
        yield b"".join(buffer).rstrip(b"\n"), start, pos


def _ends_in_quotes(line: bytes, delimiter: bytes, in_quotes: bool) -> bool:
    """Whether a CSV line ends inside a quoted cell. As `csv` reads it: a quote opens a cell
    only at the cell's start (spaces aside), `""` inside one is a literal quote, and a quote
    anywhere else is just a character (`5"-x`)."""
    at_cell_start = not in_quotes
    index, size = 0, len(line)
    while index < size:
        char = line[index:index + 1]
        if in_quotes:
            if char == b'"':
                if line[index + 1:index + 2] == b'"':
                    index += 2
                    continue
                in_quotes = False
        elif char == delimiter:
            at_cell_start = True
        elif char == b'"' and at_cell_start:
            in_quotes, at_cell_start = True, False
        elif char != b" ":
            at_cell_start = False
        index += 1
    return in_quotes


def _spans_for(
    input_format: str, lines: Iterator[bytes], *, skip_header: bool, delimiter: str = " "
) -> Iterator[tuple[bytes, int, int]]:
    if input_format == FileFormat.SDF:
        return _split_sdf_spans(lines)
    if input_format == FileFormat.MOL2:
        return _split_mol2_spans(lines)
    if input_format == FileFormat.SMILES:
        return _split_smiles_spans(lines, skip_header=skip_header, delimiter=delimiter)
    raise ValueError(f"Record spans are not defined for input format '{input_format}'.")


SPAN_FORMATS = (FileFormat.SDF, FileFormat.MOL2, FileFormat.SMILES)


def iter_record_spans(
    *,
    kind: str,
    file_path: str | Path,
) -> tuple[str, dict[str, Any], Iterator[dict[str, Any]]]:
    """Same as `iter_raw_records`, but locating the records instead of reading them.

    Each item is `{"source_index", "offset", "end"}`: the chunk travels as a byte range of the
    original file, not as a second copy of the library inside executor.db (a 10 GB SDF used to
    push 10 GB of `raw` text into the payloads).

    Formats without a record separator (a PDB, a mol2) have no spans: the `iter_raw_records`
    iterator is returned, which for them is a single record.
    """
    input_format, parse_config, fallback = iter_raw_records(kind=kind, file_path=file_path)
    receptor_mol2 = input_format == FileFormat.MOL2 and normalize_kind(kind) not in {"ligand", "molecule"}
    if input_format not in SPAN_FORMATS or receptor_mol2:
        return input_format, parse_config, fallback

    source_path = Path(file_path).expanduser().resolve()
    skip_header = bool(parse_config.get("has_header"))
    delimiter = str(parse_config.get("delimiter") or " ")

    def spans() -> Iterator[dict[str, Any]]:
        with source_path.open("rb") as handle:
            for index, (_raw, start, end) in enumerate(
                _spans_for(input_format, handle, skip_header=skip_header, delimiter=delimiter)
            ):
                yield {"source_index": index, "offset": start, "end": end}

    return input_format, parse_config, spans()


def iter_record_span(
    *,
    file_path: str | Path,
    input_format: str,
    offset: int,
    end: int,
    first_index: int = 0,
    delimiter: str = " ",
) -> Iterator[dict[str, Any]]:
    """The records in a byte range, one at a time, in the usual `{source_index, raw}` shape.

    Streamed because a shard writer hands each record straight to disk: the span never has to
    exist in memory as a list, which is what lets the shard size be counted in records without
    the record count dragging memory behind it. ``delimiter`` is the table's, as the feed
    sniffed it: the worker has to split the range into the same rows the feed counted.
    """
    source_path = Path(file_path).expanduser().resolve()
    with source_path.open("rb") as handle:
        handle.seek(int(offset))
        blob = handle.read(max(0, int(end) - int(offset)))
    # The range starts at a complete record, so there is never a header to skip here.
    lines = iter(blob.splitlines(keepends=True))
    for position, (raw, _start, _end) in enumerate(
        _spans_for(input_format, lines, skip_header=False, delimiter=delimiter)
    ):
        yield {"source_index": int(first_index) + position, "raw": raw.decode("utf-8", errors="ignore")}


def read_record_span(**kwargs) -> list[dict[str, Any]]:
    """`iter_record_span` as a list, for the callers that need to count before they parse."""
    return list(iter_record_span(**kwargs))


def split_table_line(line: str, delimiter: str) -> list[str]:
    """The cells of one SMILES-table line.

    Comma and tab lines are read as CSV, so a quoted cell keeps the delimiter inside it and an
    empty cell is still a column: the cells after it keep their header. Whitespace-delimited
    lines (.smi) split on runs of whitespace, as RDKit reads them.
    """
    line = line.rstrip("\r\n")
    if delimiter in {",", "\t"}:
        cells = next(csv.reader([line], delimiter=delimiter, skipinitialspace=True), [])
    else:
        cells = line.split()
    return [cell.strip() for cell in cells]


def _first_table_line(file_path: Path) -> str:
    """The first line that is not blank or an RDKit-style `#` comment."""
    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return line.rstrip("\r\n")
    return ""


def _table_delimiter(file_path: Path, first_line: str) -> str:
    """The delimiter of a SMILES table: the extension's when the line has it, else sniffed.

    A SMILES never contains a comma or whitespace, so a comma inside the first
    whitespace-separated token is a delimiter, while one later on is part of a name
    (`CCO ethanol, 95%` is a space-delimited line).
    """
    suffix = normalized_suffix(file_path)
    if suffix == ".tsv" and "\t" in first_line:
        return "\t"
    if suffix == ".csv" and "," in first_line:
        return ","
    if "\t" in first_line:
        return "\t"
    head = first_line.split(maxsplit=1)
    if head and "," in head[0]:
        return ","
    return " "


def _smiles_header_names(file_path: Path, delimiter: str) -> list[str]:
    first = _first_table_line(file_path)
    return split_table_line(first, delimiter) if first else []


def iter_import_entries(
    *,
    kind: str,
    file_path: str | Path,
) -> tuple[str, Iterator[dict[str, Any]]]:
    """Return the normalized input format plus a lazy iterator of parsed entries."""
    source_path = Path(file_path).expanduser().resolve()
    normalized_kind = normalize_kind(kind)
    suffix = normalized_suffix(source_path)

    if suffix == ".sdf":
        return FileFormat.SDF, _iter_sdf_entries(source_path)
    if normalized_kind in {"ligand", "molecule"} and suffix in {".smi", ".smiles", ".txt", ".csv", ".tsv"}:
        return FileFormat.SMILES, _iter_smiles_entries(source_path)
    if suffix == ".fasta":
        return FileFormat.FASTA, _iter_fasta_records(source_path)
    return suffix.lstrip(".") or "dat", _iter_single_structure_entries(source_path)


def _iter_sdf_entries(file_path: Path) -> Iterator[dict[str, Any]]:
    from rdkit import Chem

    supplier = Chem.SDMolSupplier(str(file_path), sanitize=True, removeHs=False, strictParsing=True)
    for source_index, mol in enumerate(supplier):
        if mol is None:
            continue
        name = mol.GetProp("_Name") if mol.HasProp("_Name") else f"{file_path.stem}_{source_index}"
        yield {
            "source_index": source_index,
            "mol_block": Chem.MolToMolBlock(mol),
            "name": name,
            "source_properties": {
                str(key): str(mol.GetProp(key) or "")
                for key in mol.GetPropNames(includePrivate=False, includeComputed=False)
                if str(key or "").strip() and str(key) != "_Name" and str(mol.GetProp(key) or "").strip()
            },
        }


_NAME_HEADERS = {"name", "id", "title", "compound", "molecule", "mol_id", "molecule_id"}


def _sniff_smiles_table(file_path: Path) -> tuple[bool, str, int, int | None]:
    """(has_header, delimiter, smiles_col, name_col) for a .smi/.csv/.tsv ligand file.

    Header = the first line's first non-empty cell isn't a valid SMILES. `name_col` is None
    when a header has no name column: falling back to column 1 would store whatever sits
    there (an IC50, say) as the molecule's name. # ponytail: header sniff only."""
    from rdkit import Chem

    first = _first_table_line(file_path)
    delimiter = _table_delimiter(file_path, first)
    tokens = split_table_line(first, delimiter)
    leading = next((t for t in tokens if t), "")
    has_header = bool(leading) and Chem.MolFromSmiles(leading) is None
    smiles_col: int = 0
    name_col: int | None = 1
    if has_header:
        lower = [t.lower() for t in tokens]
        smiles_col = next((i for i, t in enumerate(lower) if "smiles" in t), 0)
        name_col = next((i for i, t in enumerate(lower) if t in _NAME_HEADERS), None)
        if name_col is None:  # "Compound name", "molecule_name", ...
            name_col = next((i for i, t in enumerate(lower) if "name" in t and i != smiles_col), None)
    return has_header, delimiter, smiles_col, name_col


def _iter_smiles_entries(file_path: Path) -> Iterator[dict[str, Any]]:
    # The same line parse the import worker runs, so this view of a table can't disagree with it.
    from rdkit import Chem
    from amdockvs.io.transformers.smiles import _parse_smiles_line

    _format, config, records = iter_raw_records(kind="ligand", file_path=file_path)
    for record in records:
        source_index = int(record["source_index"])
        parsed = _parse_smiles_line(record["raw"], config, source_index=source_index, stem=file_path.stem)
        mol = parsed["mol"]
        if mol is None:
            continue
        source_properties = parsed["source_properties"]
        entry = {
            "source_index": source_index,
            "smiles": Chem.MolToSmiles(mol, canonical=False),
            "name": parsed["name"],
            "mol_block": Chem.MolToMolBlock(mol),
        }
        if source_properties:  # only when a header surfaced extra columns (keeps plain .smi rows lean)
            entry["source_properties"] = source_properties
        yield entry


def _iter_single_structure_entries(file_path: Path) -> Iterator[dict[str, Any]]:
    yield {
        "source_index": 0,
        "source_file": str(file_path.resolve()),
    }


__all__ = [
    "count_import_records",
    "iter_import_entries",
    "iter_raw_records",
    "iter_record_spans",
    "iter_record_span",
    "parse_fasta",
    "read_record_span",
]
