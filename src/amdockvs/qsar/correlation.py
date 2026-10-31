"""The correlation-pruning pipeline step. Apart from `modeling` because it subclasses scikit-learn,
which is optional: importing `modeling` must not need it."""
from __future__ import annotations

import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin


class CorrelationFilter(BaseEstimator, TransformerMixin):
    """Drop one of every pair of features with |Pearson r| > threshold (keeps the first seen).
    Greedy and O(n²) in features — fine for ≤~300 descriptors, not for fingerprints (skip it there).
    Zero-variance columns give NaN correlations, treated as 0 so they aren't dropped here (a prior
    VarianceThreshold removes them)."""

    def __init__(self, threshold: float = 0.95):
        self.threshold = threshold

    def fit(self, X, y=None):
        X = np.asarray(X, dtype=float)
        n = X.shape[1]
        keep = np.ones(n, dtype=bool)
        if n > 1:
            with np.errstate(invalid="ignore", divide="ignore"):
                corr = np.nan_to_num(np.corrcoef(X, rowvar=False), nan=0.0)
            for j in range(n):
                if not keep[j]:
                    continue
                for k in range(j + 1, n):
                    if keep[k] and abs(corr[j, k]) > float(self.threshold):
                        keep[k] = False
        self.support_ = keep
        return self

    def transform(self, X):
        return np.asarray(X, dtype=float)[:, self.support_]

    def get_support(self):
        return self.support_
