"""Fetch receptors and ligands from public databases into files the importers already read.

Every source turns what the user typed (ids, names, links) into files the importer then takes
like any picked file. Kept files go to ``~/.amdockvs/downloads/<source>/`` and are reused, so a
second import of 1IEP never touches the network. Temporary ones go to a fresh folder under the
project's ``tmp/downloads/`` and the import job deletes them once it has completed.
No Qt here: the dialog runs :func:`download` on a worker thread.
"""
from __future__ import annotations

import zlib
import hashlib
import json
import re
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from amdockvs.core.home import amdock_home

try:
    _VERSION = version("AMDockVS")
except PackageNotFoundError:  # running from a checkout
    _VERSION = "dev"
# The AlphaFold DB answers 403 to a bare "AMDockVS"; a versioned agent with a contact URL passes.
USER_AGENT = f"AMDockVS/{_VERSION} (+https://github.com/AMDockVS/AMDockVS)"
TIMEOUT_S = 60
_CHUNK = 1 << 16

#: (bytes done, bytes total or 0 when unknown, what is being fetched)
Progress = Callable[[int, int, str], None]


class Cancelled(Exception):
    pass


@dataclass
class Outcome:
    files: list[tuple[str, str]]  # (path, source label)
    failures: list[str]  # "id: reason", one per id that could not be fetched


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    kind: str  # "receptor" | "ligand" | "any"
    hint: str  # placeholder of the ids box; empty = the source takes no ids
    fetch: Callable[..., Outcome]  # (ids, folder, progress, cancel)
    lines: bool = False  # ids are one per line (names with spaces), not split on whitespace


# ---- transport ---------------------------------------------------------------------------
def _open(url: str):
    return urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT_S)


def _read(url: str) -> bytes:
    with _open(url) as response:
        return response.read()


def _fetch_file(url: str, target: Path, progress: Progress, cancel: threading.Event) -> Path:
    """Stream ``url`` to ``target`` (gunzipped when the link is ``.gz`` and the target is not).

    Written to ``.part`` and renamed at the end, so a cancelled or broken download never poses
    as a cached file.
    """
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    gunzip = urlsplit(url).path.endswith(".gz") and target.suffix != ".gz"
    try:
        with _open(url) as response, part.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            inflate = zlib.decompressobj(16 + zlib.MAX_WBITS) if gunzip else None
            while chunk := response.read(_CHUNK):
                if cancel.is_set():
                    raise Cancelled
                done += len(chunk)
                out.write(inflate.decompress(chunk) if inflate else chunk)
                progress(done, total, target.name)
            if inflate:
                out.write(inflate.flush())
        part.replace(target)
    finally:
        part.unlink(missing_ok=True)
    return target


def _write(target: Path, text: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.write_text(text)
    part.replace(target)
    return target


def _batch_name(prefix: str, ids: list[str], suffix: str) -> str:
    digest = hashlib.sha1("\n".join(sorted(ids)).encode()).hexdigest()[:10]
    return f"{prefix}_{len(ids)}_{digest}{suffix}"


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled


# ---- one file per id ---------------------------------------------------------------------
def _per_id(ids, folder, progress, cancel, *, label: str, file_for: Callable[[str, Path], Path], upper: bool = True):
    files, failures = [], []
    for identifier in (i.upper() if upper else i for i in ids):
        _check(cancel)
        progress(0, 0, identifier)
        try:
            files.append((str(file_for(identifier, folder)), f"{label}:{identifier}"))
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad id must not sink the batch
            failures.append(f"{identifier}: {exc}")
    return Outcome(files, failures)


def _pdb(ids, folder, progress, cancel):
    def one(pdb_id: str, folder: Path) -> Path:
        return _fetch_file(f"https://files.rcsb.org/download/{pdb_id}.cif", folder / f"{pdb_id}.cif", progress, cancel)

    return _per_id(ids, folder, progress, cancel, label="RCSB PDB", file_for=one)


def _alphafold(ids, folder, progress, cancel):
    def one(accession: str, folder: Path) -> Path:
        entries = json.loads(_read(f"https://alphafold.ebi.ac.uk/api/prediction/{quote(accession)}"))
        if not entries:
            raise LookupError("no AlphaFold model")
        url = entries[0]["cifUrl"]  # the API names the current model version; never guess "_v4"
        return _fetch_file(url, folder / Path(urlsplit(url).path).name, progress, cancel)

    return _per_id(ids, folder, progress, cancel, label="AlphaFold DB", file_for=one)


def _esm_atlas(ids, folder, progress, cancel):
    def one(mgnify_id: str, folder: Path) -> Path:
        url = f"https://api.esmatlas.com/fetchPredictedStructure/{quote(mgnify_id)}.pdb"
        return _fetch_file(url, folder / f"{mgnify_id}.pdb", progress, cancel)

    return _per_id(ids, folder, progress, cancel, label="ESM Atlas", file_for=one)


def _uniprot(ids, folder, progress, cancel):
    def one(accession: str, folder: Path) -> Path:
        target = folder / f"{accession}.fasta"
        if target.exists():
            return target
        text = _read(f"https://rest.uniprot.org/uniprotkb/{quote(accession)}.fasta").decode()
        if not text.startswith(">"):
            raise LookupError("not a UniProt accession")
        # ">sp|P00533|EGFR_HUMAN ..." → ">P00533 sp|P00533|EGFR_HUMAN ...": the record is named by its accession.
        return _write(target, f">{accession} {text[1:]}")

    return _per_id(ids, folder, progress, cancel, label="UniProt", file_for=one)


def _urls(ids, folder, progress, cancel):
    def one(url: str, folder: Path) -> Path:
        name = unquote(Path(urlsplit(url).path).name) or "download"
        # Keyed by the whole URL: two vendors may both serve "library.sdf".
        digest = hashlib.sha1(url.encode()).hexdigest()[:8]
        name = name[:-3] if name.endswith(".gz") and not name.endswith(".tar.gz") else name
        return _fetch_file(url, folder / digest / name, progress, cancel)

    return _per_id(ids, folder, progress, cancel, label="URL", file_for=one, upper=False)


# ---- many ids, one file ------------------------------------------------------------------
_PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound"


def _sdf_titled(sdf: str, title: str) -> str:
    """Every record of ``sdf`` renamed ``title`` (PubChem titles records by CID only)."""
    records = [r for r in sdf.split("$$$$") if r.strip()]
    return "".join(f"{title}\n" + r.lstrip("\n").split("\n", 1)[1] + "$$$$\n" for r in records)


def _pubchem(ids, folder, progress, cancel):
    target = folder / _batch_name("pubchem", ids, ".sdf")
    if target.exists():
        return Outcome([(str(target), "PubChem")], [])
    parts, failures = [], []
    cids = [i for i in ids if i.isdigit()]
    names = [i for i in ids if not i.isdigit()]
    for start in range(0, len(cids), 100):
        _check(cancel)
        batch = cids[start:start + 100]
        progress(start, len(ids), f"PubChem CIDs {start + 1}-{start + len(batch)}")
        try:
            parts.append(_read(f"{_PUBCHEM}/cid/{','.join(batch)}/SDF").decode())
        except Exception as exc:  # noqa: BLE001
            failures.append(f"CIDs {batch[0]}…{batch[-1]}: {exc}")
    for index, name in enumerate(names):
        _check(cancel)
        progress(len(cids) + index, len(ids), name)
        try:
            # First match only: a name can map to several CIDs (salts, stereo variants).
            sdf = _read(f"{_PUBCHEM}/name/{quote(name, safe='')}/SDF").decode()
            parts.append(_sdf_titled(sdf.split("$$$$")[0] + "$$$$", name))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {exc}")
    if not parts:
        return Outcome([], failures)
    return Outcome([(str(_write(target, "".join(parts))), "PubChem")], failures)


_CHEMBL = "https://www.ebi.ac.uk/chembl/api/data/molecule.json"


def _chembl_rows(query: str, progress, cancel) -> list[str]:
    """``SMILES name`` per molecule of a ChEMBL query, following its pages. Biologics (no
    structure) are skipped: the JSON route, unlike ``molecule.sdf``, keeps the ChEMBL id."""
    rows: list[str] = []
    url = f"{_CHEMBL}?{query}&limit=1000"
    while url:
        _check(cancel)
        page = json.loads(_read(url))
        meta = page["page_meta"]
        for molecule in page["molecules"]:
            smiles = (molecule.get("molecule_structures") or {}).get("canonical_smiles")
            if smiles:
                rows.append(f"{smiles} {molecule['molecule_chembl_id']}")
        progress(meta["offset"] + len(page["molecules"]), meta["total_count"], "ChEMBL")
        url = f"https://www.ebi.ac.uk{meta['next']}" if meta.get("next") else ""
    return rows


def _chembl(ids, folder, progress, cancel):
    ids = [i.upper() for i in ids]
    target = folder / _batch_name("chembl", ids, ".smi")
    if not target.exists():
        rows = []
        for start in range(0, len(ids), 100):
            rows += _chembl_rows(f"molecule_chembl_id__in={','.join(ids[start:start + 100])}", progress, cancel)
        if not rows:
            return Outcome([], [f"{', '.join(ids)}: no structure in ChEMBL"])
        _write(target, "\n".join(rows) + "\n")
    found = {line.rsplit(" ", 1)[1] for line in target.read_text().splitlines() if line}
    return Outcome([(str(target), "ChEMBL")], [f"{i}: not found" for i in ids if i not in found])


def _chembl_approved(_ids, folder, progress, cancel):
    # ponytail: re-fetched once a month; key on the ChEMBL release if monthly is ever too often.
    target = folder / f"chembl_approved_{date.today():%Y-%m}.smi"
    if not target.exists():
        _write(target, "\n".join(_chembl_rows("max_phase=4", progress, cancel)) + "\n")
    return Outcome([(str(target), "ChEMBL approved drugs")], [])


def _emolecules(_ids, folder, progress, cancel):
    """The free eMolecules dump of the latest month (one ~370 MB .smi.gz, ~1.5 GB unpacked)."""
    index = _read("https://downloads.emolecules.com/free/").decode()
    months = sorted(re.findall(r'href="(\d{4}-\d{2}-\d{2})/"', index))
    if not months:
        raise LookupError("no release listed at downloads.emolecules.com/free")
    url = f"https://downloads.emolecules.com/free/{months[-1]}/version.smi.gz"
    path = _fetch_file(url, folder / f"emolecules_{months[-1]}.smi", progress, cancel)
    return Outcome([(str(path), f"eMolecules {months[-1]}")], [])


_URL_HINT = (
    "One link per line (a pasted wget/curl script works too): ZINC22 tranche files from "
    "cartblanche22.docking.org, a vendor library link (Enamine, Cayman, MolPort…)"
)

SOURCES: dict[str, Source] = {
    source.key: source
    for source in (
        Source("pdb", "RCSB PDB (mmCIF)", "receptor", "PDB ids, e.g. 1IEP 4HJO", _pdb),
        Source("alphafold", "AlphaFold DB", "receptor", "UniProt accessions, e.g. P00533", _alphafold),
        Source("esm_atlas", "ESM Metagenomic Atlas", "receptor", "MGnify protein ids, e.g. MGYP000542242899", _esm_atlas),
        Source("uniprot", "UniProt sequence (FASTA)", "receptor", "UniProt accessions, e.g. P00533", _uniprot),
        Source("pubchem", "PubChem", "ligand", "CIDs or names, one per line, e.g. 2244 or imatinib", _pubchem, lines=True),
        Source("chembl", "ChEMBL", "ligand", "ChEMBL ids, e.g. CHEMBL941", _chembl),
        Source("chembl_approved", "ChEMBL approved drugs (max phase 4)", "ligand", "", _chembl_approved),
        Source("emolecules", "eMolecules free catalog (latest, ~370 MB)", "ligand", "", _emolecules),
        Source("url", "ZINC22 / vendor links (URLs)", "any", _URL_HINT, _urls),
    )
}


def sources_for(kind: str) -> list[Source]:
    return [source for source in SOURCES.values() if source.kind in (kind, "any")]


def parse_ids(source: Source, text: str) -> list[str]:
    if source.key == "url":
        found = re.findall(r"https?://[^\s'\"<>]+", text)
    else:
        found = re.split(r"[\n,;]+" if source.lines else r"[\s,;]+", text)
    return list(dict.fromkeys(item.strip() for item in found if item.strip()))


def kept_root() -> Path:
    return amdock_home() / "downloads"


def temporary_root(project_root) -> Path:
    return Path(project_root).expanduser().resolve() / "tmp" / "downloads"


def temporary_folder(project_root, source: Source) -> Path:
    """A fresh folder per download: nothing in it is reused, so deleting it after one import can
    never pull a file from under another."""
    return temporary_root(project_root) / uuid.uuid4().hex[:12] / source.key


def temporary_in(paths, project_root) -> list[str]:
    root = temporary_root(project_root)
    return [str(p) for p in (Path(path).expanduser().resolve() for path in paths) if p.is_relative_to(root)]


def _temporary_root_of(path: Path) -> Path | None:
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i:i + 2] == ("tmp", "downloads"):
            return Path(*parts[:i + 2])
    return None


def is_temporary(path) -> bool:
    return _temporary_root_of(Path(path)) is not None


def discard_temporary(paths) -> None:
    """Delete the temporary downloads among ``paths``; every other path is left alone."""
    for path in (Path(item).expanduser().resolve() for item in paths):
        root = _temporary_root_of(path)
        if root is not None:
            delete_files([path], root)


def delete_files(paths, root: Path) -> None:
    """Delete downloaded ``paths`` and the folders they leave empty. Nothing outside ``root``."""
    root = Path(root).expanduser().resolve()
    for path in (Path(item).expanduser().resolve() for item in paths):
        if not path.is_relative_to(root) or path == root:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        for parent in path.parents:
            if parent == root or not parent.is_relative_to(root) or any(parent.iterdir()):
                break
            parent.rmdir()


def stored_files(root: Path) -> list[tuple[Path, str, int]]:
    """(file, source key, size) of every download under ``root``, kept or temporary."""
    root = Path(root)
    found = []
    for path in sorted(root.rglob("*")) if root.is_dir() else ():
        if path.is_file() and not path.name.endswith(".part"):
            key = next((part for part in path.relative_to(root).parts[:-1] if part in SOURCES), "")
            found.append((path, key, path.stat().st_size))
    return found


def _bar() -> tuple[Progress, Callable[[], None]]:
    """A tqdm bar for scripts and notebooks (``tqdm.auto`` picks the widget in Jupyter)."""
    from tqdm.auto import tqdm

    bar = tqdm(unit="B", unit_scale=True, unit_divisor=1024)

    def report(done: int, total: int, what: str) -> None:
        if what != bar.desc:
            bar.reset(total=total or None)
            bar.set_description_str(what, refresh=False)
        elif total and bar.total != total:
            bar.total = total
        bar.update(done - bar.n)

    return report, bar.close


def download(source: Source | str, ids: list[str], *, folder: Path | None = None,
             progress: Progress | None = None, cancel: threading.Event | None = None) -> Outcome:
    """Fetch ``ids`` from ``source`` into ``folder`` (the kept downloads by default). Without a
    ``progress`` callback a tqdm bar shows it; the UI always passes its own."""
    source = SOURCES[source] if isinstance(source, str) else source
    folder = Path(folder) if folder is not None else kept_root() / source.key
    close = None
    if progress is None:
        progress, close = _bar()
    try:
        return source.fetch(ids, folder, progress, cancel or threading.Event())
    finally:
        if close is not None:
            close()
