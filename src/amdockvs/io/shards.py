"""Split a raw library into shards, without materializing a single molecule row.

An `htpvs` import is the normal import minus the expensive half: the feed is the same cheap
byte-level record splitter (`loaders.stream_import_payload_batches`), but the records end up
in shard containers instead of being parsed into rows of the project database.

The work is split in two. Workers **filter**: they parse their span only where a prefilter
demands it, and hand back the survivors. A single parent-side queue (`ShardQueueWriter`)
**cuts**: it packs exactly `shard_size` survivors per shard, so shards are uniform whatever
the filter rejected, and ids run correlative across the whole import.

A shard is not a file in the source's format: it is a container (`ms_flow.core.data.shard`)
holding the raw record bytes, with the format in its header. Nothing downstream sniffs an
extension. The ordinal a record had in its source file does not survive as its id — it lives
in the file's `source_index` column, which is where provenance goes once the queue decides who
sits next to whom.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ms_flow.core.data.shard import MAX_RECORDS, PayloadKind, Serializer, ShardWriter

from amdockvs.io.parsers import iter_record_span
from amdockvs.io.payloads import ImportBatchPayload
from amdockvs.io.rows import cull_mol, htp_mol_filter
from amdockvs.core.constants import RESOURCE_SHARDS
from amdockvs.core.vocab import FileFormat

SHARD_SUFFIX = ".parquet"

# Records per shard. Not a setting: the shard is the unit of retry and of the smallest task an
# engine is handed (a GPU engine stacks several per task), and storage cost is flat across sizes
# (measured 2026-11-19, docs/pending_stack_2026-11-19.md). A user value of 10 once turned a 3.8M
# library into 385k shards. Callers that need another size (tests) pass `shard_size=`.
SHARD_RECORDS = 256

# What the container stamps in the header so a reader knows how to parse a record's bytes.
_PAYLOAD_KIND = {
    FileFormat.SDF: PayloadKind.SDF,
    FileFormat.SMILES: PayloadKind.SMILES,
    FileFormat.PDBQT: PayloadKind.PDBQT,
}


def payload_kind_for(input_format: str) -> int:
    return _PAYLOAD_KIND.get(str(input_format or ""), PayloadKind.GENERIC_BYTES)


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


def shard_ids(row: Any) -> tuple[int, int] | None:
    """A catalog shard's ids inside its file, as `Shard.open(ids=)` takes them; None = the whole
    file. Takes a catalog row (`CatalogShard`) or a row dict."""
    get = row.get if isinstance(row, Mapping) else (lambda key: getattr(row, key, None))
    base = get("base_id")
    return None if base is None else (int(base), int(base) + int(get("slot_count") or 0))


def shard_file_name(path: str | Path, ids: Any = None) -> str:
    """The name of a file made from one shard (its prepared twin, a step's output): the source's
    own for a whole-file shard, ``<stem>.<base_id><suffix>`` for one of the many in a file."""
    path = Path(path)
    return path.name if ids is None else f"{path.stem}.{int(ids[0])}{path.suffix}"


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


class ShardQueueWriter:
    """The parent-side queue: survivors in, uniform shards out.

    Replaces the job's output sink (a shard is a file plus a row, and only this side of the
    process boundary can decide where a record lands). Runs on the executor's result thread,
    one shard at a time, which is what buys exact record counts and correlative ids.

    Shards do not get a file each, nor a file per source: the whole import rolls through one
    open file per stored format, `MAX_RECORDS` ids each, every shard its own row group and its
    row `(scope=batch, path, base_id, slot_count)`. A library of 168 files is four files of 1M,
    not 168 (nor 168 open `.tmp`). A shard still never mixes two batches; a file may. The rows
    of a file are committed when it is closed (full, at `flush`, or when a stopped job closes
    the writer), so a row never points at a file that is not on disk yet.

    ponytail: not idempotent, by decision. A retried span is re-filtered and lands wherever
    the queue happens to be, so shard boundaries differ between runs and a re-import appends
    rather than upserting — a failed import is restarted from the beginning, not resumed.
    Resuming would need the queue to checkpoint (span -> shard) receipts. A crash loses the
    open file (its `.tmp` stays behind); what was closed before is the library.
    """

    def __init__(
        self,
        *,
        project_db: Any,
        shard_dir: str | Path,
        shard_size: int,
        generation_id: int | None = None,
    ) -> None:
        self._project_db = project_db
        self._shard_dir = Path(shard_dir).expanduser().resolve()
        self._shard_dir.mkdir(parents=True, exist_ok=True)
        # Opened by the caller (io/api.py), like the chemistry step's. An import *adds* to the
        # library, so it joins the active generation rather than forking one: only a rewrite
        # has a previous state worth being able to fall back to.
        self._generation_id = generation_id
        self._shard_size = max(1, int(shard_size))
        # One bucket per (batch, format): mixing two files in one shard would leave the
        # `import_batch_id` of its inventory row a lie. One open file per format: the header
        # says how to read every record in it.
        self._pending: dict[tuple[int | None, str], list[tuple[int, str]]] = {}
        self._files: dict[str, tuple[ShardWriter, list[dict]]] = {}
        self._next_id = 0
        self._shard_index = 0

    def handle(self, chunk_id: str, result: Any) -> None:
        for group in list(result or []):
            key = (group.get("import_batch_id"), str(group.get("input_format") or ""))
            bucket = self._pending.setdefault(key, [])
            bucket.extend((int(index), str(raw)) for index, raw in group.get("records") or [])
            while len(bucket) >= self._shard_size:
                self._write_shard(key, bucket[: self._shard_size])
                del bucket[: self._shard_size]

    def on_error(self, chunk_id: str, error: str) -> None:
        """A failed span contributes no records. Nothing to undo — see the class note."""

    def flush(self) -> None:
        """Called once the job is done: write what is left, one partial shard per source."""
        for key, bucket in list(self._pending.items()):
            if bucket:
                self._write_shard(key, bucket)
            self._pending.pop(key, None)
        self.close()

    def close(self) -> None:
        """Every open file closed and its rows committed: also what a stopped job keeps."""
        for key in list(self._files):
            self._close_file(key)

    def _write_shard(self, key: tuple[int | None, str], records: list[tuple[int, str]]) -> None:
        import_batch_id, input_format = key
        base_id = self._next_id
        self._next_id += len(records)
        open_file = self._files.get(input_format)
        # Ids run across the whole import (every format draws from one counter), so a file is
        # full when this shard's ids no longer fit its window, not after a count of shards.
        if open_file is not None and base_id + len(records) > open_file[0].base_id + open_file[0].slot_count:
            self._close_file(input_format)
        if input_format not in self._files:
            # Made unique: every import restarts its ids at 0 in the same directory, so a name
            # built from ids alone would overwrite the last import.
            path = self._shard_dir / f"library_{uuid4().hex[:12]}{SHARD_SUFFIX}"
            writer = ShardWriter(
                path,
                dataset_id=None,
                shard_id=0,  # a file of many shards; each is its catalog row
                base_id=base_id,
                kind=payload_kind_for(input_format),
                serializer=Serializer.UTF8,
                slot_count=MAX_RECORDS,
                metadata={"input_format": input_format},  # the batch of a shard is its row's scope
                # Ids are dense, so this column is the only place the source ordinal survives.
                source_index=True,
            )
            self._files[input_format] = (writer, [])
        writer, rows = self._files[input_format]
        for offset, (index, raw) in enumerate(records):
            writer.add(base_id + offset, raw.encode("utf-8"), source_index=index)
        writer.end_group()
        rows.append({"scope": import_batch_id, "shard_index": self._shard_index, "base_id": base_id,
                     "slot_count": len(records), "n_records": len(records)})
        self._shard_index += 1

    def _close_file(self, input_format: str) -> None:
        from ms_flow.core.data.catalog import add_shards

        writer, rows = self._files.pop(input_format)
        writer.close()  # fsynced and renamed: only now may a row point at it
        add_shards(self._project_db, [{**row, "path": str(writer.path)} for row in rows],
                   generation_id=self._generation_id)


def shard_queue_writer(params: dict, config: dict) -> ShardQueueWriter:
    """`result_handler_factory` of the import: shards of `batch_size` records each, in the
    project's shard directory, into `generation_id`."""
    return ShardQueueWriter(
        project_db=config["project_db"],
        shard_dir=config["project_resources"][RESOURCE_SHARDS]["path"],
        shard_size=params["batch_size"],
        generation_id=params.get("generation_id"),
    )


class ShardGenerationWriter:
    """The result sink of any step that rewrites the library: new shards, new generation.

    Replaces an upsert on `(source, shard_index)`. That upsert repointed the inventory one
    chunk at a time, so a job that died at shard 600 of 1000 left a library that was 60% the
    new step and 40% the old one, with no way to tell which was which.

    Here the rows land in a generation nothing reads yet, and `flush()` — which MolSuite
    calls once, when the job actually completes — is the only thing that makes them the
    library. A failed job leaves an inert generation behind, not a corrupted one.
    """

    def __init__(self, *, project_db: Any, generation_id: int) -> None:
        self._project_db = project_db
        self._generation_id = int(generation_id)

    def handle(self, chunk_id: str, result: Any) -> None:
        from ms_flow.core.data.catalog import add_shards

        add_shards(self._project_db, [{**row, "scope": row.get("import_batch_id")} for row in result or []],
                   generation_id=self._generation_id)

    def on_error(self, chunk_id: str, error: str) -> None:
        """A failed shard contributes no row, so it is simply absent from the generation.

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
        from ms_flow.core.data.catalog import compact, record_count, shard_spec

        from amdockvs.molecules.storage import activate_generation

        written = record_count(self._project_db, shard_spec(generation_id=self._generation_id), whole=True)
        if written <= 0:
            raise RuntimeError(
                f"Shard generation {self._generation_id} ended with 0 molecules: refusing to "
                "replace the library with an empty one. Every molecule failed the step; the "
                "previous generation is still the library."
            )
        # A worker wrote a file per shard; one big file per scope is what the library keeps.
        # Before activation: nothing (a prepared twin) is named after these paths yet.
        compact(self._project_db, self._generation_id)
        activate_generation(self._project_db, self._generation_id)


def shard_generation_writer(params: dict, config: dict) -> ShardGenerationWriter:
    """`result_handler_factory` of a step that rewrites the library into `generation_id`."""
    return ShardGenerationWriter(project_db=config["project_db"], generation_id=params["generation_id"])


__all__ = [
    "FORMAT_FOR_KIND",
    "SHARD_SUFFIX",
    "ShardGenerationWriter",
    "ShardQueueWriter",
    "shard_generation_writer",
    "filter_ligand_span",
    "is_shard_key",
    "mol_from_record",
    "payload_kind_for",
    "shard_key",
    "shard_queue_writer",
    "smiles_record",
    "split_shard_key",
]
