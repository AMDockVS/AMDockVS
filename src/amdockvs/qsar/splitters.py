"""How a QSAR model's molecules are split into train/test and CV folds. No scikit-learn.

Every splitter gives the same answer: a test flag and a fold number per molecule. The grouped ones
(scaffold, Butina, MaxMin) first label each molecule with a group and never let a group straddle
train/test or two folds; random is the grouped case with one group per molecule; stratified keeps
each class (or, for regression, each value quantile) in proportion on every side.
"""
from __future__ import annotations

import math
import random as _random
from typing import Any, Sequence

import numpy as np

SPLITTERS: tuple[str, ...] = ("random", "stratified", "scaffold", "butina", "maxmin")
NEEDS_STRUCTURES = frozenset({"scaffold", "butina", "maxmin"})
BUTINA_CUTOFF = 0.6  # Tanimoto distance: molecules closer than this cluster together


def _fingerprints(mols):
    from rdkit.Chem import rdFingerprintGenerator

    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    return [None if mol is None else generator.GetFingerprint(mol) for mol in mols]


def scaffold_groups(mols: Sequence[Any]) -> list[str]:
    """Bemis-Murcko scaffold SMILES; acyclic molecules share the empty scaffold."""
    from rdkit.Chem.Scaffolds import MurckoScaffold

    return [f"unparsed:{i}" if mol is None else MurckoScaffold.MurckoScaffoldSmiles(mol=mol)
            for i, mol in enumerate(mols)]


def butina_groups(mols: Sequence[Any], cutoff: float = BUTINA_CUTOFF) -> list[int]:
    """Taylor-Butina cluster per molecule over Morgan r2/2048.

    ponytail: the full lower-triangle distance list, O(n²) memory (~400 MB at 10k molecules);
    BitBIRCH (bblean) when QSAR sets get bigger than that."""
    from rdkit import DataStructs
    from rdkit.ML.Cluster import Butina

    fps = _fingerprints(mols)
    valid = [i for i, fp in enumerate(fps) if fp is not None]
    distances: list[float] = []
    for k in range(1, len(valid)):
        similarities = DataStructs.BulkTanimotoSimilarity(fps[valid[k]], [fps[i] for i in valid[:k]])
        distances.extend(1.0 - s for s in similarities)
    groups = list(range(-1, -len(mols) - 1, -1))  # unparsed molecules stay alone
    for cluster, members in enumerate(Butina.ClusterData(distances, len(valid), cutoff, isDistData=True)):
        for k in members:
            groups[valid[k]] = cluster
    return groups


def maxmin_groups(mols: Sequence[Any], seed: int = 0) -> list[int]:
    """MaxMin picks ~sqrt(n) mutually distant seeds; each molecule joins its most similar seed."""
    from rdkit import DataStructs
    from rdkit.SimDivFilters import MaxMinPicker

    fps = _fingerprints(mols)
    valid = [i for i, fp in enumerate(fps) if fp is not None]
    groups = list(range(-1, -len(mols) - 1, -1))
    if not valid:
        return groups
    pool = [fps[i] for i in valid]
    k = min(len(pool), max(2, math.ceil(math.sqrt(len(pool)))))
    picks = list(MaxMinPicker().LazyBitVectorPick(pool, len(pool), k, seed=int(seed)))
    similarity = np.asarray([DataStructs.BulkTanimotoSimilarity(pool[p], pool) for p in picks])
    for position, nearest in enumerate(similarity.argmax(axis=0)):
        groups[valid[position]] = int(nearest)
    return groups


def molecule_groups(splitter: str, mols: Sequence[Any], *, seed: int = 0) -> list | None:
    """The group label of each molecule, or None for the ungrouped splitters."""
    if splitter == "scaffold":
        return scaffold_groups(mols)
    if splitter == "butina":
        return butina_groups(mols)
    if splitter == "maxmin":
        return maxmin_groups(mols, seed=seed)
    return None


def strata(y: np.ndarray, task: str, bins: int = 5) -> np.ndarray:
    """The class of each molecule, or its value quantile (``bins`` of them) for regression."""
    y = np.asarray(y, dtype=float)
    if task == "classification":
        return y.astype(int)
    edges = np.unique(np.quantile(y, np.linspace(0, 1, int(bins) + 1)[1:-1]))
    return np.searchsorted(edges, y, side="right")


def grouped_holdout(groups: Sequence[Any], test_size: float, seed: int = 0) -> np.ndarray:
    """Test flag per molecule: whole groups, in shuffled order, go to test until ~test_size of the
    rows are there. One group always stays in training. # ponytail: greedy, no label balancing."""
    by_group: dict[Any, list[int]] = {}
    for index, key in enumerate(groups):
        by_group.setdefault(key, []).append(index)
    if len(by_group) < 2:
        raise ValueError(
            f"A grouped holdout needs at least two groups; found {len(by_group)}. Use more diverse "
            "molecules, another splitter, or a test fraction of 0 (no holdout)."
        )
    order = list(by_group)
    _random.Random(int(seed)).shuffle(order)
    target = max(1, round(float(test_size) * len(groups)))
    is_test = np.zeros(len(groups), dtype=bool)
    taken = 0
    for key in order[:-1]:
        if taken >= target:
            break
        is_test[by_group[key]] = True
        taken += len(by_group[key])
    return is_test


def stratified_holdout(labels: np.ndarray, test_size: float, seed: int = 0) -> np.ndarray:
    """Test flag per molecule: ~test_size of every stratum, at least one row of the whole set."""
    rng = np.random.default_rng(int(seed))
    is_test = np.zeros(len(labels), dtype=bool)
    for value in np.unique(labels):
        members = rng.permutation(np.flatnonzero(labels == value))
        is_test[members[: int(round(float(test_size) * len(members)))]] = True
    if not is_test.any():
        is_test[rng.integers(len(labels))] = True
    return is_test


def grouped_folds(groups: Sequence[Any], k: int, seed: int = 0) -> np.ndarray:
    """Fold per molecule: groups, biggest first (ties shuffled), each to the emptiest fold. All -1
    when there are fewer groups than folds: such a CV would not be one."""
    by_group: dict[Any, list[int]] = {}
    for index, key in enumerate(groups):
        by_group.setdefault(key, []).append(index)
    folds = np.full(len(groups), -1, dtype=int)
    if int(k) < 2 or len(by_group) < int(k):
        return folds
    order = list(by_group)
    _random.Random(int(seed)).shuffle(order)
    order.sort(key=lambda key: len(by_group[key]), reverse=True)  # stable: the shuffle breaks ties
    sizes = [0] * int(k)
    for key in order:
        fold = sizes.index(min(sizes))
        folds[by_group[key]] = fold
        sizes[fold] += len(by_group[key])
    return folds


def stratified_folds(labels: np.ndarray, k: int, seed: int = 0) -> np.ndarray:
    """Fold per molecule, dealing each shuffled stratum round-robin so every fold gets its share."""
    folds = np.full(len(labels), -1, dtype=int)
    if int(k) < 2 or len(labels) < int(k):
        return folds
    rng = np.random.default_rng(int(seed))
    offset = 0
    for value in np.unique(labels):
        members = rng.permutation(np.flatnonzero(labels == value))
        folds[members] = (np.arange(len(members)) + offset) % int(k)
        offset += len(members)
    return folds


def assign_split(
    splitter: str,
    *,
    y: np.ndarray,
    task: str,
    groups: Sequence[Any] | None = None,
    test_size: float = 0.0,
    cv_folds: int = 0,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """(is_test, fold) per molecule. Folds are drawn among the training molecules only; test rows
    and every row without CV get -1. ``groups`` comes from ``molecule_groups`` for the grouped
    splitters."""
    if splitter not in SPLITTERS:
        raise ValueError(f"Unknown splitter '{splitter}'. Choose one of {', '.join(SPLITTERS)}.")
    n = len(y)
    if splitter in NEEDS_STRUCTURES and groups is None:
        raise ValueError(f"The {splitter} splitter needs the molecules' group labels.")
    labels = strata(y, task) if splitter == "stratified" else None
    group_list = list(groups) if groups is not None else list(range(n))
    is_test = np.zeros(n, dtype=bool)
    if float(test_size) > 0:
        is_test = (stratified_holdout(labels, test_size, seed) if labels is not None
                   else grouped_holdout(group_list, test_size, seed))
    folds = np.full(n, -1, dtype=int)
    train = np.flatnonzero(~is_test)
    if int(cv_folds) >= 2:
        folds[train] = (stratified_folds(labels[train], cv_folds, seed) if labels is not None
                        else grouped_folds([group_list[i] for i in train], cv_folds, seed))
    return is_test, folds


__all__ = [
    "NEEDS_STRUCTURES",
    "SPLITTERS",
    "assign_split",
    "butina_groups",
    "grouped_folds",
    "grouped_holdout",
    "maxmin_groups",
    "molecule_groups",
    "scaffold_groups",
    "stratified_folds",
    "stratified_holdout",
    "strata",
]
