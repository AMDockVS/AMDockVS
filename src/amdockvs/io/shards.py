"""Split a raw library into shards, without materializing a single molecule row.

An `htpvs` import is the normal import minus the expensive half: the feed is the same cheap
byte-level record splitter (`loaders.stream_import_payload_batches`), but the records end up
in shard containers instead of being parsed into rows of the project database.

Workers do the whole of it: each filters its span, encodes the survivors and writes them as a
part (`write_ligand_span`); the parent only commits the parts (`ShardImportWriter`). A record's
id is planned, not counted: its ordinal in the source file times F (`fan_out`), and a fragment
split off it the next ids. So the ids depend neither on which task finished first nor on what a
filter dropped, and a killed import resumes without duplicates. A filter leaves holes.

A shard is not a file in the source's format: it is a container (`ms_flow.core.data.shard`)
holding the raw record bytes, with the format in its header. Nothing downstream sniffs an
extension. The ordinal also stays in the `source_index` column.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ms_flow.core.data.shard import SUFFIX, Group, PartWriter
from amdockvs.core.constants import ShardKind

from amdockvs.io.parsers import iter_record_span
from amdockvs.io.payloads import ImportBatchPayload
from amdockvs.io.rows import cull_mol, htp_mol_filter
from amdockvs.core.vocab import FileFormat

SHARD_SUFFIX = SUFFIX  # an Arrow container: X.arrows with X.arrows.index beside it

# Records per shard. Not a setting: the shard is the unit of retry and of the smallest task an
# engine is handed (a GPU engine stacks several per task), and storage cost is flat across sizes
# (measured 2026-11-19, docs/pending_stack_2026-11-19.md). A user value of 10 once turned a 3.8M
# library into 385k shards. Callers that need another size (tests) pass `shard_size=`.
SHARD_RECORDS = 256

# What the container stamps in the header so a reader knows how to parse a record's bytes.
_PAYLOAD_KIND = {
    FileFormat.SDF: ShardKind.SDF,
    FileFormat.SMILES: ShardKind.SMILES,
    FileFormat.PDBQT: ShardKind.PDBQT,
}


def payload_kind_for(input_format: str) -> int:
    return _PAYLOAD_KIND.get(str(input_format or ""), ShardKind.BYTES)


FORMAT_FOR_KIND = {kind: fmt for fmt, kind in _PAYLOAD_KIND.items()}

# A library molecule is (import batch, record id), and a run over the library that needs one int
# per molecule (a fingerprint matrix, a prediction file) packs the pair into one int64.
# ponytail: record ids below 2**32 per batch; a molecule row id never reaches 2**32, which is
# what `is_shard_key` tells the two apart by.
_RECORD_BITS = 32


def shard_key(import_batch_id: int | None, record_id: int) -> int:
    return (int(import_batch_id or 0) << _RECORD_BITS) | int(record_id)


def split_shard_key(key: int) -> tuple[int, int]:
    """`(import_batch_id, record_id)`; the record id is the molecule's `source_index`."""
    return int(key) >> _RECORD_BITS, int(key) & ((1 << _RECORD_BITS) - 1)


def is_shard_key(key: int) -> bool:
    return int(key) >> _RECORD_BITS > 0


def shard_number(row: Any) -> int | None:
    """A catalog shard's number inside its file, as `Shard.open(shard=)` takes it (ids restart per
    import, so they are no address); None = the whole file. Takes a shard row
    (`ms_flow.core.data.catalog.shards`), a dict or an object."""
    get = row.get if isinstance(row, Mapping) else (lambda key: getattr(row, key, None))
    number = get("shard_index")
    return None if number is None else int(number)


def shard_file_name(path: str | Path, shard: int | None = None) -> str:
    """The name of a file made from one shard (its prepared twin, a step's output): the source's
    own for a whole-file shard, ``<stem>.<shard number><suffix>`` for one of the many in a file."""
    path = Path(path)
    return path.name if shard is None else f"{path.stem}.{int(shard)}{path.suffix}"


def mol_from_record(raw: str, input_format: str):
    """One record's raw text -> an RDKit molecule, or None. Shared with the shard pipeline,
    which is the other place that has to turn a stored record back into chemistry."""
    from rdkit import Chem

    if input_format == FileFormat.SDF:
        supplier = Chem.ForwardSDMolSupplier(io.BytesIO(str(raw).encode()), sanitize=True, removeHs=False)
        return next(iter(supplier), None)
    if input_format == FileFormat.MOL2:
        from amdockvs.io.formats import mol_from_mol2_block

        return mol_from_mol2_block(raw)
    if input_format == FileFormat.SMILES:
        tokens = str(raw).strip().split(maxsplit=1)
        if not tokens:
            return None
        mol = Chem.MolFromSmiles(tokens[0])
        # The rest of the line is the record's name, and a shard has no row to keep it in:
        # identity has to travel inside the molecule or it is gone after the first transform.
        if mol is not None and len(tokens) > 1:
            mol.SetProp("_Name", tokens[1].strip())
        return mol
    return None


def smiles_record(raw: str, parse_config: dict[str, Any]) -> str:
    """One SMILES-table line -> `SMILES name`, which is what a shard record has to be.

    A shard header says SMILES and nothing else: the source's delimiter, column order and
    header do not travel with it. So the dialect is applied once, here, on the way in — and
    every reader downstream (`mol_from_record`, the preparation step, the cluster script) gets
    a line it can parse with no knowledge of where it came from.
    """
    from amdockvs.io.transformers.smiles import smiles_table_columns, smiles_tokens

    tokens = smiles_tokens(raw, parse_config or {})
    if not tokens:
        return ""
    smiles_col, name_col = smiles_table_columns(parse_config or {})
    smiles = tokens[smiles_col] if 0 <= smiles_col < len(tokens) else tokens[0]
    name = tokens[name_col] if name_col is not None and 0 <= name_col < len(tokens) else ""
    return f"{smiles} {name}".strip()


def _sdf_record(mol) -> str:
    from rdkit import Chem

    if mol is None:
        return ""
    buffer = io.StringIO()
    with Chem.SDWriter(buffer) as writer:
        writer.write(mol)
    return buffer.getvalue()


def stored_record_format(input_format: str) -> str:
    """The format a shard stores a record of `input_format` in.

    A container holds molecules, as SDF or as SMILES, and nothing else: everything that is not a
    SMILES table is stored as the SDF it parses to. This is not always the format the record was
    read in, which is what makes it worth naming - parsing a converted record back in its source
    format fails every time, and the header would promise a format the bytes are not in.
    """
    return FileFormat.SMILES if input_format == FileFormat.SMILES else FileFormat.SDF


def _span_candidates(batch: ImportBatchPayload) -> Iterator[tuple[int, str, Any]]:
    """The records of a byte range of a library file (SDF, mol2, SMILES table)."""
    for record in iter_record_span(
        file_path=batch.file_path,
        input_format=batch.input_format,
        offset=batch.span_offset,
        end=batch.span_end,
        first_index=batch.span_first_index,
        delimiter=str((batch.parse_config or {}).get("delimiter") or " "),
    ):
        raw = str(record.get("raw") or "")
        mol = None
        if batch.input_format == FileFormat.SMILES:
            raw = smiles_record(raw, dict(batch.parse_config or {}))
        if batch.input_format == FileFormat.MOL2:
            # Parsed once: the molecule the filter judges is the one that was written out, so
            # the filter decides on chemistry and a filtered import stores what an unfiltered
            # one would.
            mol = mol_from_record(raw, FileFormat.MOL2)
            raw = _sdf_record(mol)
        yield int(record["source_index"]), raw, mol


def _structure_candidates(batch: ImportBatchPayload) -> Iterator[tuple[int, str, Any]]:
    """The records of a feed that carries no byte range.

    A format with no record separator — one mol/mdl block, a PDB, a PDBQT, an mmCIF — is fed as
    entries rather than as a byte range, so `span_offset` is -1. There is no offset to seek to
    (seeking to -1 is the `OSError: [Errno 22]` this used to raise), and no span reader knows how
    to cut the file into records. It is read here with the reader the row import uses, and each
    molecule is stored as SDF, so the shard route accepts every format the ligand dialog offers.
    """
    from amdockvs.io.formats import is_readable, read_mols

    if not is_readable(batch.file_path):
        raise ValueError(
            f"{batch.file_path.name} cannot be stored as a screening-library shard: a shard holds "
            f"molecules and '{batch.input_format}' is not a molecular format this import reads. "
            "Import it into the project's molecules instead of as a library."
        )
    first_index = int((list(batch.records) or list(batch.entries) or [{}])[0].get("source_index") or 0)
    stored = 0
    for position, mol in enumerate(read_mols(batch.file_path)):
        record = _sdf_record(mol)
        if not record:
            continue
        stored += 1
        yield first_index + position, record, mol
    if not stored:
        # Silence here would be an import that completes with nothing in it: this file is the
        # whole record, so failing to read it is a failed import, not a filtered-out molecule.
        raise ValueError(
            f"No molecule could be read from {batch.file_path.name}: there is nothing to store "
            "as a shard record."
        )


def filter_ligand_span(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One span of a source file -> the records that survive the prefilter.

    The worker half of the import: parsing (and therefore the filter) is where the CPU goes,
    so it runs in parallel; the surviving bytes travel back to the parent, which is the only
    process that writes shards.
    """
    batch = ImportBatchPayload.model_validate(payload)
    passes = htp_mol_filter(batch.prefilter, batch.molecule_kind)
    record_format = stored_record_format(batch.input_format)  # what each candidate's bytes are in
    stored_format = record_format
    prepare = _preparation(batch.prefilter)
    if prepare is not None and _needs_sdf(batch.prefilter):
        stored_format = FileFormat.SDF  # explicit Hs and 3D coordinates don't survive a SMILES
    spanned = batch.span_count > 0 and batch.span_offset >= 0
    records: list[tuple[int, str]] = []
    for source_index, raw, mol in (_span_candidates(batch) if spanned else _structure_candidates(batch)):
        if not raw:
            continue
        # Every record is parsed, filter or not: one that does not read is not a molecule, and
        # a library that looks clean but isn't costs more at docking time than the parse here.
        mol = mol if mol is not None else mol_from_record(raw, record_format)
        if mol is None:
            continue
        kept = passes is None or passes(cull_mol(mol, batch))
        if prepare is None:
            if kept:
                records.append((source_index, raw))
            continue
        # Each molecule of the record is judged on its own, as a row import judges it: the
        # record's own may be filtered out while a fragment split off it is kept.
        texts = prepare(mol, batch.molecule_kind, stored_format, keep_record=kept, passes=passes)
        records.extend((source_index, text) for text in texts)
    return [
        {
            "import_batch_id": batch.import_batch_id,
            "input_format": stored_format,
            "records": records,
        }
    ]


def _preparation(prefilter):
    """The record -> prepared records step when the import asked for any preparation, else None.

    It is what a row import does to a small molecule (`io/rows.evaluate_ligand_mol` and
    `transformers/rows._split_fragment_rows`): keep the largest ligand fragment, add Hs /
    3D / canonical tautomer to it, and with `split_fragments` add each co-component worth
    keeping as its own record, named `<name> [fragNN]` and prepared the same way. Without
    any of these the shard keeps the raw bytes, which is the cheap path a library import is for.
    """
    if prefilter is None or not (prefilter.prep_active() or prefilter.split_fragments):
        return None

    def prepare(mol, molecule_kind: str, stored_format: str, *, keep_record: bool = True, passes=None) -> list[str]:
        """``keep_record``: the record's own molecule passed the filter. ``passes`` is that
        filter, which every split-off fragment has to pass on its own too."""
        from amdockvs.io.rows import evaluate_ligand_mol, prep_ligand_mol
        from amdockvs.molecules.fragments import extra_fragment_molecules

        name = mol.GetProp("_Name") if mol.HasProp("_Name") else ""
        out = []
        if keep_record:
            decision, _reason = evaluate_ligand_mol(mol=mol, metadata=None, prefilter=prefilter, molecule_kind=molecule_kind)
            if decision is not None:
                kept = decision.kept_mol
                for key in mol.GetPropNames():  # the record's SD tags (activity, ids) stay on what it keeps
                    if not kept.HasProp(key):
                        kept.SetProp(key, mol.GetProp(key))
                kept.SetProp("_Name", name)
                out.append(kept)
        if prefilter.split_fragments:
            for fragment_index, fragment in extra_fragment_molecules(mol):
                if passes is not None and not passes(fragment):
                    continue
                fragment = prep_ligand_mol(fragment, prefilter)
                fragment.SetProp("_Name", f"{name} [frag{fragment_index:02d}]")
                out.append(fragment)
        return [text for text in (_stored_record(m, stored_format) for m in out) if text]

    return prepare


def _needs_sdf(prefilter) -> bool:
    return bool(prefilter.add_hs or prefilter.gen_3d)


def _stored_record(mol, stored_format: str) -> str:
    if stored_format == FileFormat.SMILES:
        from rdkit import Chem

        name = mol.GetProp("_Name") if mol.HasProp("_Name") else ""
        return f"{Chem.MolToSmiles(mol)} {name}".strip()
    return _sdf_record(mol)


# Ids a record may take: its own and its fragments' (ordinal * F + k). F = 1 without fan-out.
FAN_OUT = 8
_ID_LIMIT = 1 << _RECORD_BITS  # a record id fits the low half of `shard_key`


def fan_out(prefilter) -> int:
    return FAN_OUT if prefilter is not None and getattr(prefilter, "split_fragments", False) else 1


def write_ligand_span(payload: dict[str, Any], progress_cb=None) -> list[dict[str, Any]]:
    """One span of a source file -> one part of shards, written here; its receipt goes back.

    ``payload["part"]``: where (``dir``), as what (``id``, ``generation_id``) and how big
    (``shard_records`` source records a shard). A shard covers ``shard_records`` ordinals from
    the span's start, so its range never depends on the filter. More than F molecules from one
    record: the record is a drop with its reason, the import goes on.
    """
    from ms_flow.core.data.catalog import SOURCE, file_row

    payload = dict(payload)
    part = dict(payload.pop("part"))
    batch = ImportBatchPayload.model_validate(payload)
    width = fan_out(batch.prefilter)
    spanned = batch.span_count > 0 and batch.span_offset >= 0
    first = batch.span_first_index if spanned else None
    end = batch.span_first_index + batch.span_count if spanned else None
    if end is not None and end * width > _ID_LIMIT:
        raise ValueError(_over_the_bits(batch.file_path, end, width))
    (group,) = filter_ligand_span(payload)
    records = group["records"]
    if not records:
        return []
    if first is None:
        first, end = records[0][0], records[-1][0] + 1
        if end * width > _ID_LIMIT:
            raise ValueError(_over_the_bits(batch.file_path, end, width))
    size = max(1, int(part["shard_records"]))
    path = Path(part["dir"]) / f".part-{uuid4().hex[:16]}{SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    span = Group(base_id=first * width, slot_count=max(1, (end - first) * width), n_records=0, scope=None)
    with PartWriter(path, kind=payload_kind_for(group["input_format"]), like=span, group_size=size * width,
                    meta={"input_format": group["input_format"]}, scope=batch.import_batch_id) as writer:
        at = 0
        while at < len(records):
            ordinal = records[at][0]
            texts = []
            while at < len(records) and records[at][0] == ordinal:
                texts.append(records[at][1])
                at += 1
            if len(texts) > width:
                writer.drop(ordinal * width, f"{len(texts)} molecules from one record, at most {width}")
                continue
            for k, text in enumerate(texts):
                writer.add(ordinal * width + k, text.encode("utf-8"), source_index=ordinal)
    return [file_row(path, part=str(part["id"]), role=SOURCE, generation_id=part["generation_id"])
            | {"import_batch_id": batch.import_batch_id}]


def _over_the_bits(path, records: int, width: int) -> str:
    return (f"{Path(path).name}: {records} records do not fit the record ids of one source "
            f"(at most {_ID_LIMIT // width} with {width} id(s) a record). Split the file.")


class ShardImportWriter:
    """The import's sink: the parts the workers wrote, committed into the generation's containers
    (`ms_flow.sinks.CatalogWriter`), idempotent by part id. Holds no record.

    A batch's `n_imported` is what its committed shards hold, read from the catalog when the job
    ends or stops: set, never added to, so a replay or a resume counts nothing twice.
    """

    def __init__(self, *, project_db: Any, generation_id: int | None) -> None:
        from ms_flow.sinks import CatalogWriter

        self._project_db, self._generation_id = project_db, generation_id
        self._sink = CatalogWriter(project_db)
        self._batches: set[int] = set()

    def write(self, results: list) -> None:
        receipts = [list(result or []) for result in results]
        self._batches.update(int(r["import_batch_id"]) for group in receipts for r in group
                             if r.get("import_batch_id") is not None)
        self._sink.write([group for group in receipts if group])

    def handle(self, chunk_id: str, result: Any) -> None:
        self.write([result])

    def on_error(self, chunk_id: str, error: str) -> None:
        """A failed span wrote no part: nothing to undo, a resume runs it again."""

    def flush(self) -> None:
        try:
            self._sink.finish()
        finally:
            self._count()

    def close(self) -> None:
        """What a stopped job keeps: what committed, counted."""
        self._sink.close()
        self._count()

    def _count(self) -> None:
        from amdockvs.io.batches import set_imported

        set_imported(self._project_db, self._batches, generation_id=self._generation_id)


def shard_import_writer(params: dict, config: dict) -> ShardImportWriter:
    """`result_handler_factory` of the import."""
    return ShardImportWriter(project_db=config["project_db"], generation_id=params.get("generation_id"))


class ShardGenerationWriter:
    """The result sink of any step that rewrites the library: new files, new generation.

    Replaces an upsert on `(source, shard_index)`. That upsert repointed the inventory one
    chunk at a time, so a job that died at shard 600 of 1000 left a library that was 60% the
    new step and 40% the old one, with no way to tell which was which.

    Here each worker's file is a part, appended to the containers of a generation nothing reads
    yet as its group arrives (`ms_flow.sinks.CatalogWriter`), and `flush()` — which MolSuite
    calls once, when the job actually completes — makes it the library. A failed job leaves an
    inert generation behind, not a corrupted one.
    """

    def __init__(self, *, project_db: Any, generation_id: int) -> None:
        from ms_flow.sinks import CatalogWriter

        self._project_db = project_db
        self._generation_id = int(generation_id)
        self._sink = CatalogWriter(project_db)

    def write(self, results: list) -> None:
        from ms_flow.core.data.catalog import SOURCE

        # A part per source shard: its file's name is that shard's, once per generation.
        self._sink.write([[{"part": f"{self._generation_id}:{Path(row['path']).name}", "path": row["path"],
                            "role": SOURCE, "generation_id": self._generation_id} for row in result or []]
                          for result in results])

    def handle(self, chunk_id: str, result: Any) -> None:
        self.write([result])

    def on_error(self, chunk_id: str, error: str) -> None:
        """A failed shard contributes no file, so it is simply absent from the generation.

        ponytail: the generation is still activated with a hole in it. Whether a partial
        rewrite should be rejected outright is a policy question — today a failed shard is
        reported per chunk and the user re-runs the step, which refills it.
        """

    def flush(self) -> None:
        """The switch. One UPDATE, and the grandparent is forgotten.

        Refuses an empty generation. A step where every molecule failed reports a clean chunk
        per shard — `n_records=0` is a valid shard — so without this check the job completes
        and the switch silently replaces the library with nothing. That is the one outcome a
        rewrite must never be allowed to reach.
        """
        from ms_flow.core.data.catalog import activate_generation, record_count, shard_spec

        self._sink.finish()  # a part that could not be committed fails the rewrite: the library stays
        written = record_count(self._project_db, shard_spec(generation_id=self._generation_id), whole=True)
        if written <= 0:
            raise RuntimeError(
                f"Shard generation {self._generation_id} ended with 0 molecules: refusing to "
                "replace the library with an empty one. Every molecule failed the step; the "
                "previous generation is still the library."
            )
        activate_generation(self._project_db, self._generation_id)

    def close(self) -> None:
        self._sink.close()


def shard_generation_writer(params: dict, config: dict) -> ShardGenerationWriter:
    """`result_handler_factory` of a step that rewrites the library into `generation_id`."""
    return ShardGenerationWriter(project_db=config["project_db"], generation_id=params["generation_id"])


__all__ = [
    "FORMAT_FOR_KIND",
    "SHARD_SUFFIX",
    "ShardGenerationWriter",
    "ShardImportWriter",
    "fan_out",
    "shard_generation_writer",
    "filter_ligand_span",
    "is_shard_key",
    "mol_from_record",
    "payload_kind_for",
    "shard_key",
    "shard_import_writer",
    "write_ligand_span",
    "smiles_record",
    "split_shard_key",
]
