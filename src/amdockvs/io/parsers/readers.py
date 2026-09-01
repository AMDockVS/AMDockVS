from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from amdockvs.io._common import normalize_kind
from amdockvs.vocab import FileFormat


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
    suffix = source_path.suffix.lower()
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
    if suffix in {".smi", ".smiles", ".txt", ".csv", ".tsv"}:
        has_header, _delimiter, _smiles_col, _name_col = _sniff_smiles_table(source_path)
        data_lines = 0
        with source_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    data_lines += 1
        return max(0, data_lines - (1 if has_header else 0))
    return 1


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
    suffix = source_path.suffix.lower()

    if suffix == ".sdf":
        return FileFormat.SDF, {}, _iter_raw_from_spans(source_path, FileFormat.SDF, skip_header=False)
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
        return FileFormat.SMILES, config, _iter_raw_from_spans(source_path, FileFormat.SMILES, skip_header=has_header)
    return suffix.lstrip(".") or "dat", {}, _iter_single_structure_entries(source_path)


def _iter_raw_from_spans(source_path: Path, input_format: str, *, skip_header: bool) -> Iterator[dict[str, Any]]:
    with source_path.open("rb") as handle:
        for index, (raw, _start, _end) in enumerate(
            _spans_for(input_format, handle, skip_header=skip_header)
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


def _split_smiles_spans(lines: Iterator[bytes], *, skip_header: bool) -> Iterator[tuple[bytes, int, int]]:
    pos = 0
    header_skipped = not skip_header
    for line in lines:
        start = pos
        pos += len(line)
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        if not header_skipped:
            header_skipped = True
            continue
        yield line.rstrip(b"\n"), start, pos


def _spans_for(input_format: str, lines: Iterator[bytes], *, skip_header: bool) -> Iterator[tuple[bytes, int, int]]:
    if input_format == FileFormat.SDF:
        return _split_sdf_spans(lines)
    if input_format == FileFormat.SMILES:
        return _split_smiles_spans(lines, skip_header=skip_header)
    raise ValueError(f"Record spans are not defined for input format '{input_format}'.")


SPAN_FORMATS = (FileFormat.SDF, FileFormat.SMILES)


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
    if input_format not in SPAN_FORMATS:
        return input_format, parse_config, fallback

    source_path = Path(file_path).expanduser().resolve()
    skip_header = bool(parse_config.get("has_header"))

    def spans() -> Iterator[dict[str, Any]]:
        with source_path.open("rb") as handle:
            for index, (_raw, start, end) in enumerate(
                _spans_for(input_format, handle, skip_header=skip_header)
            ):
                yield {"source_index": index, "offset": start, "end": end}

    return input_format, parse_config, spans()


def read_record_span(
    *,
    file_path: str | Path,
    input_format: str,
    offset: int,
    end: int,
    first_index: int = 0,
) -> list[dict[str, Any]]:
    """The records in a byte range, in the usual `{source_index, raw}` shape."""
    source_path = Path(file_path).expanduser().resolve()
    with source_path.open("rb") as handle:
        handle.seek(int(offset))
        blob = handle.read(max(0, int(end) - int(offset)))
    # The range starts at a complete record, so there is never a header to skip here.
    lines = iter(blob.splitlines(keepends=True))
    return [
        {"source_index": int(first_index) + position, "raw": raw.decode("utf-8", errors="ignore")}
        for position, (raw, _start, _end) in enumerate(
            _spans_for(input_format, lines, skip_header=False)
        )
    ]


def _smiles_header_names(file_path: Path, delimiter: str) -> list[str]:
    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                tokens = stripped.split("," if delimiter == "," else None)
                return [t.strip() for t in tokens]
    return []


def iter_import_entries(
    *,
    kind: str,
    file_path: str | Path,
) -> tuple[str, Iterator[dict[str, Any]]]:
    """Return the normalized input format plus a lazy iterator of parsed entries."""
    source_path = Path(file_path).expanduser().resolve()
    normalized_kind = normalize_kind(kind)
    suffix = source_path.suffix.lower()

    if suffix == ".sdf":
        return FileFormat.SDF, _iter_sdf_entries(source_path)
    if normalized_kind in {"ligand", "molecule"} and suffix in {".smi", ".smiles", ".txt", ".csv", ".tsv"}:
        return FileFormat.SMILES, _iter_smiles_entries(source_path)
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


def _sniff_smiles_table(file_path: Path) -> tuple[bool, str, int, int]:
    """(has_header, delimiter, smiles_col, name_col) for a .smi/.csv ligand file.
    Header = the first line's first token isn't a valid SMILES. # ponytail: header sniff only."""
    from rdkit import Chem

    first = ""
    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):  # skip blanks + RDKit-style comments
                first = line.rstrip("\n")
                break
    delimiter = "," if "," in first else ("\t" if "\t" in first else " ")
    tokens = [t.strip() for t in first.split("," if delimiter == "," else None) if t.strip()]
    has_header = bool(tokens) and Chem.MolFromSmiles(tokens[0]) is None
    smiles_col, name_col = 0, 1
    if has_header:
        lower = [t.lower() for t in tokens]
        smiles_col = next((i for i, t in enumerate(lower) if "smiles" in t), 0)
        name_col = next((i for i, t in enumerate(lower) if t in {"name", "id", "title", "compound", "molecule", "mol_id", "molecule_id"}), 1)
    return has_header, delimiter, smiles_col, name_col


def _iter_smiles_entries(file_path: Path) -> Iterator[dict[str, Any]]:
    from rdkit import Chem

    has_header, delimiter, smiles_col, name_col = _sniff_smiles_table(file_path)
    supplier = Chem.SmilesMolSupplier(
        str(file_path), delimiter=delimiter, smilesColumn=smiles_col, nameColumn=name_col,
        titleLine=has_header, sanitize=True,
    )
    for source_index, mol in enumerate(supplier):
        if mol is None:
            continue
        name = mol.GetProp("_Name") if mol.HasProp("_Name") else f"{file_path.stem}_{source_index}"
        # With a header, extra columns land as mol properties — surface them like SDF tags so the
        # activity-from-column hook can read them.
        source_properties = {
            str(key): str(mol.GetProp(key) or "")
            for key in mol.GetPropNames(includePrivate=False, includeComputed=False)
            if str(key or "").strip() and str(key) != "_Name" and str(mol.GetProp(key) or "").strip()
        }
        entry = {
            "source_index": source_index,
            "smiles": Chem.MolToSmiles(mol, canonical=False),
            "name": name,
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
    "read_record_span",
]
