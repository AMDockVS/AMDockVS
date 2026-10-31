import numpy as np
import pytest

from amdockvs.qsar.splitters import (
    SPLITTERS,
    assign_split,
    grouped_holdout,
    molecule_groups,
    strata,
)

SMILES = ["c1ccccc1C", "c1ccccc1CC", "c1ccccc1O", "c1ccncc1C", "c1ccncc1CC", "C1CCCCC1N",
          "C1CCCCC1O", "CCCCO", "CCCCN", "c1ccc2ccccc2c1", "c1ccc2ccccc2c1O", "C1CCOC1"]


def _mols():
    from rdkit import Chem

    return [Chem.MolFromSmiles(s) for s in SMILES]


def test_grouped_holdout_keeps_groups_whole_and_one_in_training():
    groups = ["s1", "s1", "s2", "s2", "s3", "s3", "s4", "s4", "s5", "s5"]
    is_test = grouped_holdout(groups, test_size=0.3, seed=7)
    assert is_test.any() and not is_test.all()
    assert {g for g, t in zip(groups, is_test) if t}.isdisjoint({g for g, t in zip(groups, is_test) if not t})
    with pytest.raises(ValueError, match="at least two groups"):
        grouped_holdout(["same"] * 10, test_size=0.3)
    assert grouped_holdout(["a"] * 5 + ["b"] * 5, test_size=0.99).sum() == 5


@pytest.mark.parametrize("splitter", SPLITTERS)
def test_every_splitter_keeps_groups_out_of_both_sides(splitter):
    y = np.arange(len(SMILES), dtype=float)
    groups = molecule_groups(splitter, _mols(), seed=3)
    is_test, folds = assign_split(splitter, y=y, task="regression", groups=groups,
                                  test_size=0.25, cv_folds=2, seed=3)
    assert is_test.any() and not is_test.all()
    assert (folds[is_test] == -1).all()  # folds cover training rows only
    assert set(folds[~is_test]) == {0, 1}
    if groups is not None:
        train_groups = {groups[i] for i in np.flatnonzero(~is_test)}
        assert train_groups.isdisjoint(groups[i] for i in np.flatnonzero(is_test))
        for fold in (0, 1):
            inside = {groups[i] for i in np.flatnonzero(folds == fold)}
            assert inside.isdisjoint(groups[i] for i in np.flatnonzero((folds >= 0) & (folds != fold)))


def test_stratified_keeps_each_class_on_both_sides_and_in_every_fold():
    y = np.array([0] * 12 + [1] * 6)
    is_test, folds = assign_split("stratified", y=y, task="classification", test_size=1 / 3, cv_folds=3, seed=0)
    assert y[is_test].sum() == 2 and (y[is_test] == 0).sum() == 4
    for fold in range(3):
        assert set(y[folds == fold]) == {0, 1}


def test_too_few_groups_for_the_folds_leaves_no_cv():
    groups = ["a"] * 6 + ["b"] * 6
    _, folds = assign_split("scaffold", y=np.arange(12.), task="regression", groups=groups, cv_folds=3)
    assert (folds == -1).all()


def test_regression_strata_are_value_quantiles():
    assert list(strata(np.arange(10.), "regression", bins=2)) == [0] * 5 + [1] * 5
