from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Per-molecule import accounting. Workers run in loky processes and can't log to the monitor or
# emit job events, but they share the project filesystem — so each batch appends one JSON line
# to a single file and the GUI drains + sums them when the import job finishes. One file, not one
# per batch: a 700k import has hundreds of batches.
_STATS_FILE = "import_stats.jsonl"

# Reason keys (why a source record didn't become a molecule). "imported" is the success bucket.
IMPORTED = "imported"
UNREADABLE = "unreadable"            # RDKit could not parse the record
FILTERED_PREFILTER = "filtered_prefilter"   # HTP/streaming cull (SMARTS, ranges, PAINS, QSAR…)
NO_VALID_FRAGMENT = "no_valid_fragment"     # nothing usable after fragment selection
FILTERED_PROPERTY = "filtered_property"     # drug-likeness/property filter on the kept fragment


def bump(tally: dict[str, int], reason: str, count: int = 1) -> None:
    tally[reason] = int(tally.get(reason, 0)) + int(count)


def write_import_stats(storage_dir: str | Path | None, tally: dict[str, int]) -> None:
    """Append one batch's tally as a JSON line (no-op if empty or no dir). A line this short is
    written in one `write` on an append-mode file, so batches running at once never interleave."""
    if not storage_dir or not any(tally.values()):
        return
    stats_dir = Path(storage_dir).expanduser().resolve()
    stats_dir.mkdir(parents=True, exist_ok=True)
    with open(stats_dir / _STATS_FILE, "a", encoding="utf-8") as out:
        out.write(json.dumps(tally) + "\n")


def drain_import_stats(storage_dir: str | Path | None) -> dict[str, int]:
    """Sum and delete the pending tallies. Returns {reason: count} incl. 'imported'."""
    total: dict[str, int] = {}
    if not storage_dir:
        return total
    path = Path(storage_dir).expanduser().resolve() / _STATS_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return total
    path.unlink(missing_ok=True)
    for line in lines:
        try:
            data = json.loads(line)
        except Exception:  # noqa: BLE001 - a corrupt line must not sink the summary
            data = {}
        for key, value in dict(data).items():
            total[key] = int(total.get(key, 0)) + int(value or 0)
    return total


def summarize(tally: dict[str, Any]) -> str:
    """One human line: 'Imported 18 of 20 — 2 skipped (1 filtered, 1 unreadable).'"""
    imported = int(tally.get(IMPORTED, 0))
    skipped_by = {k: int(v or 0) for k, v in tally.items() if k != IMPORTED and int(v or 0) > 0}
    skipped = sum(skipped_by.values())
    total = imported + skipped
    if total == 0:
        return "No molecules were processed."
    labels = {
        UNREADABLE: "unreadable",
        FILTERED_PREFILTER: "filtered",
        NO_VALID_FRAGMENT: "no valid fragment",
        FILTERED_PROPERTY: "filtered",
    }
    # Merge the two filter buckets under one label for the summary.
    merged: dict[str, int] = {}
    for key, value in skipped_by.items():
        merged[labels.get(key, key)] = merged.get(labels.get(key, key), 0) + value
    detail = ", ".join(f"{count} {label}" for label, count in sorted(merged.items()))
    if not skipped:
        return f"Imported all {imported} molecule(s)."
    return f"Imported {imported} of {total} - {skipped} skipped ({detail})."


__all__ = [
    "IMPORTED",
    "UNREADABLE",
    "FILTERED_PREFILTER",
    "NO_VALID_FRAGMENT",
    "FILTERED_PROPERTY",
    "bump",
    "write_import_stats",
    "drain_import_stats",
    "summarize",
]
