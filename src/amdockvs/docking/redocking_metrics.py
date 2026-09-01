"""Pure redocking-validation metrics: cumulative success curves, per-protocol summary stats,
funnel (score vs RMSD) points and rank composition. No Qt/DB deps so they self-test — the UI
panel (ui/tools/docking/redocking_charts.py) just renders what these return.

A "record" is a plain dict: {"protocol": str, "case": hashable, "rank": int,
"rmsd": float|None, "score": float|None}. A "case" is one redocked complex (its poses share it).
"""
from __future__ import annotations

import numpy as np


def _by_protocol(records):
    out: dict = {}
    for r in records:
        out.setdefault(r["protocol"], []).append(r)
    return out


def case_rmsds(records, *, mode="top1", n=5) -> dict:
    """Per-case RMSD per protocol. mode='top1' uses the rank-1 pose; mode='best' the minimum
    RMSD over the first n poses. Cases without a valid RMSD are dropped. Returns
    {protocol: [rmsd, ...] sorted}."""
    result: dict = {}
    for protocol, recs in _by_protocol(records).items():
        cases: dict = {}
        for r in recs:
            rmsd = r.get("rmsd")
            if rmsd is None:
                continue
            rank = int(r.get("rank", 1))
            if mode == "top1" and rank != 1:
                continue
            if mode == "best" and rank > int(n):
                continue
            key = r["case"]
            cases[key] = float(rmsd) if key not in cases else min(cases[key], float(rmsd))
        if cases:
            result[protocol] = sorted(cases.values())
    return result


def success_curve(rmsds, *, x_max, step=0.1) -> list:
    """Empirical cumulative success: fraction of cases with RMSD <= threshold, sampled 0..x_max.
    Returns [(threshold, fraction), ...]."""
    arr = np.asarray(list(rmsds), dtype=float)
    if arr.size == 0:
        return []
    xs = np.arange(0.0, float(x_max) + step, step)
    return [(float(x), float(np.mean(arr <= x))) for x in xs]


def summary_stats(rmsds, *, thresholds=(1.0, 2.0)) -> dict:
    """N, median, P90 and success rate at each threshold. sr key is f'sr{t:g}' -> 'sr1','sr2'."""
    arr = np.asarray(list(rmsds), dtype=float)
    if arr.size == 0:
        return {}
    stats = {
        "n": int(arr.size),
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
    }
    for t in thresholds:
        stats[f"sr{t:g}"] = float(np.mean(arr <= float(t)))
    return stats


def funnel_points(records) -> dict:
    """All poses that have both score and RMSD, grouped by protocol:
    {protocol: [(rmsd, score), ...]}. The 'funnel' shape (low RMSD -> low score) is the signal
    that the scoring function discriminates near-native poses."""
    out: dict = {}
    for r in records:
        if r.get("rmsd") is None or r.get("score") is None:
            continue
        out.setdefault(r["protocol"], []).append((float(r["rmsd"]), float(r["score"])))
    return out


def rank_composition(records, *, threshold=2.0, n=10) -> dict:
    """Per case, the rank of the first near-native pose (RMSD<=threshold, among the first n poses):
    counts of (rank-1, rank 2..n, none) per protocol. VS cares about the top pose being right."""
    out: dict = {}
    for protocol, recs in _by_protocol(records).items():
        best_rank: dict = {}  # case -> lowest near-native rank
        all_cases: set = set()
        for r in recs:
            key = r["case"]
            all_cases.add(key)
            rmsd, rank = r.get("rmsd"), int(r.get("rank", 1))
            if rmsd is None or rank > int(n):
                continue
            if float(rmsd) <= float(threshold):
                cur = best_rank.get(key)
                best_rank[key] = rank if cur is None else min(cur, rank)
        rank1 = sum(1 for k in all_cases if best_rank.get(k) == 1)
        rank2 = sum(1 for k in all_cases if (best_rank.get(k) or 0) > 1)
        fail = len(all_cases) - rank1 - rank2
        out[protocol] = (rank1, rank2, fail)
    return out


def _demo() -> None:
    recs = [
        {"protocol": "A", "case": 1, "rank": 1, "rmsd": 0.5, "score": -9.0},
        {"protocol": "A", "case": 1, "rank": 2, "rmsd": 3.0, "score": -8.0},
        {"protocol": "A", "case": 2, "rank": 1, "rmsd": 2.5, "score": -7.0},
        {"protocol": "A", "case": 2, "rank": 2, "rmsd": 1.2, "score": -7.5},
    ]
    assert case_rmsds(recs, mode="top1")["A"] == [0.5, 2.5]
    assert case_rmsds(recs, mode="best", n=5)["A"] == [0.5, 1.2]
    stats = summary_stats([0.5, 2.5], thresholds=(1.0, 2.0))
    assert stats["n"] == 2 and stats["sr1"] == 0.5 and stats["sr2"] == 0.5, stats
    curve = success_curve([0.5, 2.5], x_max=3.0, step=1.0)
    assert curve[0] == (0.0, 0.0) and curve[-1][1] == 1.0, curve
    # case1 near-native at rank1, case2 near-native first at rank2
    assert rank_composition(recs, threshold=2.0, n=10)["A"] == (1, 1, 0)
    assert funnel_points(recs)["A"] == [(0.5, -9.0), (3.0, -8.0), (2.5, -7.0), (1.2, -7.5)]
    print("ok")


if __name__ == "__main__":
    _demo()
