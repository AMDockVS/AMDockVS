"""Import accounting sidecar: workers tally per-molecule outcomes on the shared filesystem
(loky can't log to the monitor); the GUI drains + sums them when the import job finishes."""

import tempfile

from amdockvs.io.import_stats import (
    FILTERED_PREFILTER,
    IMPORTED,
    UNREADABLE,
    bump,
    drain_import_stats,
    summarize,
    write_import_stats,
)


def test_drain_sums_and_clears_shards():
    d = tempfile.mkdtemp()
    write_import_stats(d, {IMPORTED: 3, FILTERED_PREFILTER: 1})
    write_import_stats(d, {IMPORTED: 2, UNREADABLE: 1})
    write_import_stats(d, {})  # empty tally writes no shard
    assert drain_import_stats(d) == {IMPORTED: 5, FILTERED_PREFILTER: 1, UNREADABLE: 1}
    assert drain_import_stats(d) == {}  # shards deleted on drain


def test_bump_and_summaries():
    tally: dict[str, int] = {}
    bump(tally, IMPORTED)
    bump(tally, IMPORTED)
    bump(tally, UNREADABLE)
    assert tally == {IMPORTED: 2, UNREADABLE: 1}
    assert "Imported 2 of 3" in summarize(tally)
    assert summarize({IMPORTED: 5}) == "Imported all 5 molecule(s)."
    assert summarize({}) == "No molecules were processed."


def test_drain_missing_dir_is_empty():
    assert drain_import_stats(tempfile.mkdtemp() + "/nope") == {}
