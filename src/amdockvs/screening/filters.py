"""High-throughput pre-docking filter pipeline (pure core).

Streams candidate molecules and applies a configurable chain of cheap->expensive filters
to shrink and enrich a library BEFORE the expensive docking step.

Pure: no runtime/DB here. It consumes RDKit mols and an optional QSAR predict callable.
Callers stream molecules past ``evaluate_mol`` and act on each verdict as it comes; nothing
here accumulates a result set, so the filter costs the same at 10 molecules and at 10 million.
Reuses the simple chemistry wrappers (PAINS, Ro5, descriptors) rather than re-deriving them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

from amdockvs.chemistry.descriptors import calculate_basic_descriptors
from amdockvs.chemistry.filtering import molecule_pains_matches, ro5_violations

# Descriptor keys a property-range filter may reference (subset of calculate_basic_descriptors).
PROPERTY_KEYS = (
    "mw", "logp", "hbd", "hba", "tpsa", "rotatable_bonds",
    "heavy_atom_count", "aromatic_ring_count", "fraction_csp3", "formal_charge",
)


@dataclass
class HTPFilterConfig:
    exclude_pains: bool = False
    max_ro5_violations: int | None = None  # keep molecules with <= this many Lipinski violations
    property_ranges: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    include_smarts: tuple[str, ...] = ()   # keep only molecules matching ALL of these
    exclude_smarts: tuple[str, ...] = ()   # drop molecules matching ANY of these
    qsar_op: str = ">="                    # ">=" or "<="
    qsar_threshold: float | None = None    # applied to the QSAR predict callable's value

    def needs_descriptors(self) -> bool:
        return self.max_ro5_violations is not None or bool(self.property_ranges)

    def needs_pains(self) -> bool:
        return bool(self.exclude_pains)


@lru_cache(maxsize=256)
def _smarts_query(pattern: str):
    from rdkit import Chem

    query = Chem.MolFromSmarts(str(pattern))
    if query is None:
        raise ValueError(f"Invalid SMARTS pattern: {pattern!r}")
    return query


def _in_range(value: float | int | None, bounds: tuple[float | None, float | None]) -> bool:
    if value is None:
        return False
    low, high = bounds
    if low is not None and float(value) < float(low):
        return False
    if high is not None and float(value) > float(high):
        return False
    return True


def evaluate_mol(
    mol: Any,
    config: HTPFilterConfig,
    *,
    qsar_predict: Callable[[dict[str, float | int | None]], float] | None = None,
) -> tuple[str | None, dict[str, float | int | None], float | None]:
    """Evaluate one molecule against the filter chain (cheap->expensive).

    Returns ``(reason, descriptors, qsar_value)`` — reason is None when the molecule passes,
    otherwise the name of the first rejecting filter. descriptors/qsar_value are computed only
    when a filter needs them, so PAINS-only screening never touches descriptors and vice versa.
    """
    if mol is None:
        return "invalid", {}, None
    want_qsar = config.qsar_threshold is not None and qsar_predict is not None
    want_descriptors = config.needs_descriptors() or want_qsar

    if config.exclude_smarts and any(mol.HasSubstructMatch(_smarts_query(p)) for p in config.exclude_smarts):
        return "exclude_smarts", {}, None
    if config.include_smarts and not all(mol.HasSubstructMatch(_smarts_query(p)) for p in config.include_smarts):
        return "include_smarts", {}, None
    if config.needs_pains() and molecule_pains_matches(mol):
        return "pains", {}, None

    descriptors: dict[str, float | int | None] = {}
    if want_descriptors:
        descriptors = calculate_basic_descriptors(mol)
        if config.max_ro5_violations is not None and len(ro5_violations(descriptors)) > config.max_ro5_violations:
            return "ro5", descriptors, None
        range_reject = next(
            (k for k, b in config.property_ranges.items() if not _in_range(descriptors.get(k), b)),
            None,
        )
        if range_reject is not None:
            return f"property:{range_reject}", descriptors, None

    qsar_value: float | None = None
    if want_qsar:
        qsar_value = float(qsar_predict(descriptors))
        keep = qsar_value >= config.qsar_threshold if config.qsar_op == ">=" else qsar_value <= config.qsar_threshold
        if not keep:
            return "qsar", descriptors, qsar_value
    return None, descriptors, qsar_value


__all__ = [
    "HTPFilterConfig",
    "PROPERTY_KEYS",
    "evaluate_mol",
]
