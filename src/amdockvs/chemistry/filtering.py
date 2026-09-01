from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from amdockvs.chemistry.descriptors import calculate_basic_descriptors
from amdockvs.constants import STATUS_FLAG_PAINS, STATUS_FLAG_RO5_VIOLATION
from amdockvs.molecule_paths import preferred_molecule_path

SMALL_MOLECULE_FILTER_TYPE = "small_molecule"


class SmallMoleculeFilterField:
    PAINS_MATCHES = "pains_matches"
    RO5_VIOLATIONS = "ro5_violations"
    MW = "mw"
    EXACT_MW = "exact_mw"
    LOGP = "logp"
    HBD = "hbd"
    HBA = "hba"
    TPSA = "tpsa"
    ROTATABLE_BONDS = "rotatable_bonds"
    FRAGMENT_COUNT = "fragment_count"
    RING_COUNT = "ring_count"
    AROMATIC_RING_COUNT = "aromatic_ring_count"
    HETERO_ATOM_COUNT = "hetero_atom_count"
    HEAVY_ATOM_COUNT = "heavy_atom_count"
    FORMAL_CHARGE = "formal_charge"
    FRACTION_CSP3 = "fraction_csp3"
    N_ATOMS = "n_atoms"


class SmallMoleculeFilterOperator:
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    EQ = "eq"
    HAS_ANY = "has_any"
    IS_EMPTY = "is_empty"


_NUMERIC_OPERATORS = (
    SmallMoleculeFilterOperator.LT,
    SmallMoleculeFilterOperator.LTE,
    SmallMoleculeFilterOperator.GT,
    SmallMoleculeFilterOperator.GTE,
    SmallMoleculeFilterOperator.EQ,
)

_NUMERIC_DESCRIPTOR_SPECS: dict[str, dict[str, Any]] = {
    SmallMoleculeFilterField.MW: {"label": "MW (Da)", "kind": "float"},
    SmallMoleculeFilterField.EXACT_MW: {"label": "Exact MW (Da)", "kind": "float"},
    SmallMoleculeFilterField.LOGP: {"label": "logP", "kind": "float"},
    SmallMoleculeFilterField.HBD: {"label": "HBD", "kind": "int"},
    SmallMoleculeFilterField.HBA: {"label": "HBA", "kind": "int"},
    SmallMoleculeFilterField.TPSA: {"label": "TPSA", "kind": "float"},
    SmallMoleculeFilterField.ROTATABLE_BONDS: {"label": "Rotatable Bonds", "kind": "int"},
    SmallMoleculeFilterField.FRAGMENT_COUNT: {"label": "Fragment Count", "kind": "int"},
    SmallMoleculeFilterField.RING_COUNT: {"label": "Ring Count", "kind": "int"},
    SmallMoleculeFilterField.AROMATIC_RING_COUNT: {"label": "Aromatic Ring Count", "kind": "int"},
    SmallMoleculeFilterField.HETERO_ATOM_COUNT: {"label": "Hetero Atom Count", "kind": "int"},
    SmallMoleculeFilterField.HEAVY_ATOM_COUNT: {"label": "Heavy Atom Count", "kind": "int"},
    SmallMoleculeFilterField.FORMAL_CHARGE: {"label": "Formal Charge", "kind": "int"},
    SmallMoleculeFilterField.FRACTION_CSP3: {"label": "Fraction Csp3", "kind": "float"},
    SmallMoleculeFilterField.N_ATOMS: {"label": "Atom Count", "kind": "int"},
}

SMALL_MOLECULE_FIELD_SPECS: dict[str, dict[str, Any]] = {
    SmallMoleculeFilterField.PAINS_MATCHES: {
        "label": "PAINS Matches",
        "kind": "sequence",
        "operators": (SmallMoleculeFilterOperator.IS_EMPTY, SmallMoleculeFilterOperator.HAS_ANY),
    },
    SmallMoleculeFilterField.RO5_VIOLATIONS: {
        "label": "Ro5 Violations",
        "kind": "sequence",
        "operators": (SmallMoleculeFilterOperator.IS_EMPTY, SmallMoleculeFilterOperator.HAS_ANY),
    },
    **{
        field_name: {**spec, "operators": _NUMERIC_OPERATORS}
        for field_name, spec in _NUMERIC_DESCRIPTOR_SPECS.items()
    },
}


@dataclass(frozen=True)
class SmallMoleculeFilterRule:
    field: str
    operator: str
    value: float | int | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SmallMoleculeFilterRule | None":
        field = str(raw.get("field") or "").strip()
        operator = str(raw.get("operator") or "").strip()
        if not field or not operator:
            return None
        spec = SMALL_MOLECULE_FIELD_SPECS.get(field)
        if spec is None:
            return None
        value = raw.get("value")
        if spec["kind"] == "int":
            value = None if value in (None, "", "No limit") else int(value)
        elif spec["kind"] == "float":
            value = None if value in (None, "", "No limit") else float(value)
        else:
            value = None
        return cls(field=field, operator=operator, value=value)

    def to_mapping(self) -> dict[str, Any]:
        return {"field": self.field, "operator": self.operator, "value": self.value}


@dataclass(frozen=True)
class SmallMoleculeFilterCriteria:
    rules: tuple[SmallMoleculeFilterRule, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "SmallMoleculeFilterCriteria":
        if raw is None:
            return cls()
        if isinstance(raw.get("rules"), list):
            rules: list[SmallMoleculeFilterRule] = []
            for item in raw.get("rules") or []:
                if isinstance(item, Mapping):
                    rule = SmallMoleculeFilterRule.from_mapping(item)
                    if rule is not None:
                        rules.append(rule)
            return cls(rules=tuple(rules))
        rules = list(build_small_molecule_prefilter_rules(
            exclude_pains=bool(raw.get("exclude_pains", False)),
            require_ro5=bool(raw.get("require_ro5", False)),
            max_rotatable_bonds=None if raw.get("max_rotatable_bonds") in (None, "", 0) else max(0, int(raw.get(
                "max_rotatable_bonds"))),
            max_heavy_atoms=None if (raw.get("max_heavy_atoms", raw.get("max_atoms")) in (None, "", 0)) else max(1,
                                                                                                                 int(raw.get(
                                                                                                                     "max_heavy_atoms",
                                                                                                                     raw.get(
                                                                                                                         "max_atoms")))),
        ))
        return cls(rules=tuple(rules))

    def to_mapping(self) -> dict[str, Any]:
        return {"rules": [rule.to_mapping() for rule in self.rules]}

    def is_active(self) -> bool:
        return bool(self.rules)


@dataclass(frozen=True)
class SmallMoleculeFilterValues:
    pains_matches: tuple[str, ...]
    ro5_violations: tuple[str, ...]
    descriptors: dict[str, float | int | None]
    n_atoms: int

    @property
    def status_flags(self) -> int:
        flags = 0
        if self.pains_matches:
            flags |= STATUS_FLAG_PAINS
        if self.ro5_violations:
            flags |= STATUS_FLAG_RO5_VIOLATION
        return flags

    def passes(self, criteria: SmallMoleculeFilterCriteria) -> bool:
        return passes_small_molecule_filter(self, criteria)


@lru_cache(maxsize=1)
def _pains_catalog():
    from rdkit.Chem.FilterCatalog import FilterCatalog, FilterCatalogParams

    params = FilterCatalogParams()
    params.AddCatalog(FilterCatalogParams.FilterCatalogs.PAINS_A)
    params.AddCatalog(FilterCatalogParams.FilterCatalogs.PAINS_B)
    params.AddCatalog(FilterCatalogParams.FilterCatalogs.PAINS_C)
    return FilterCatalog(params)


def molecule_pains_matches(mol) -> tuple[str, ...]:
    """PAINS A/B/C filter descriptions matched by ``mol`` (empty tuple = clean). Pure wrapper
    over the cached RDKit FilterCatalog — reusable standalone for HTP pre-filtering."""
    if mol is None:
        return ()
    catalog = _pains_catalog()
    return tuple(entry.GetDescription() for entry in catalog.GetMatches(mol))


def ro5_violations(descriptors: Mapping[str, float | int | None]) -> tuple[str, ...]:
    """Lipinski rule-of-five violations given a descriptor mapping (mw/logp/hbd/hba)."""
    return _ro5_violations(descriptors)


def _ro5_violations(descriptors: Mapping[str, float | int | None]) -> tuple[str, ...]:
    violations: list[str] = []
    if float(descriptors.get("mw") or 0.0) > 500.0:
        violations.append("mw>500")
    if float(descriptors.get("logp") or 0.0) > 5.0:
        violations.append("logp>5")
    if int(descriptors.get("hbd") or 0) > 5:
        violations.append("hbd>5")
    if int(descriptors.get("hba") or 0) > 10:
        violations.append("hba>10")
    return tuple(violations)


def passes_small_molecule_filter(
        values: SmallMoleculeFilterValues,
        criteria: SmallMoleculeFilterCriteria,
) -> bool:
    for rule in criteria.rules:
        if not _passes_small_molecule_rule(values, rule):
            return False
    return True


def _passes_small_molecule_rule(
        values: SmallMoleculeFilterValues,
        rule: SmallMoleculeFilterRule,
) -> bool:
    if rule.field == SmallMoleculeFilterField.PAINS_MATCHES:
        sequence_value = values.pains_matches
        if rule.operator == SmallMoleculeFilterOperator.IS_EMPTY:
            return len(sequence_value) == 0
        if rule.operator == SmallMoleculeFilterOperator.HAS_ANY:
            return len(sequence_value) > 0
        return False
    if rule.field == SmallMoleculeFilterField.RO5_VIOLATIONS:
        sequence_value = values.ro5_violations
        if rule.operator == SmallMoleculeFilterOperator.IS_EMPTY:
            return len(sequence_value) == 0
        if rule.operator == SmallMoleculeFilterOperator.HAS_ANY:
            return len(sequence_value) > 0
        return False

    if rule.field == SmallMoleculeFilterField.N_ATOMS:
        numeric_value = float(values.n_atoms or 0)
    else:
        numeric_value = float(values.descriptors.get(rule.field) or 0.0)
    return _compare_numeric(numeric_value, rule.operator, rule.value)


def _compare_numeric(numeric_value: float, operator: str, target: float | int | None) -> bool:
    if target is None:
        return True
    threshold = float(target)
    if operator == SmallMoleculeFilterOperator.LT:
        return numeric_value < threshold
    if operator == SmallMoleculeFilterOperator.LTE:
        return numeric_value <= threshold
    if operator == SmallMoleculeFilterOperator.GT:
        return numeric_value > threshold
    if operator == SmallMoleculeFilterOperator.GTE:
        return numeric_value >= threshold
    if operator == SmallMoleculeFilterOperator.EQ:
        return numeric_value == threshold
    return False


def build_small_molecule_prefilter_rules(
        *,
        exclude_pains: bool = False,
        require_ro5: bool = False,
        max_rotatable_bonds: int | None = None,
        max_heavy_atoms: int | None = None,
) -> tuple[SmallMoleculeFilterRule, ...]:
    rules: list[SmallMoleculeFilterRule] = []
    if exclude_pains:
        rules.append(
            SmallMoleculeFilterRule(
                field=SmallMoleculeFilterField.PAINS_MATCHES,
                operator=SmallMoleculeFilterOperator.IS_EMPTY,
            )
        )
    if require_ro5:
        rules.append(
            SmallMoleculeFilterRule(
                field=SmallMoleculeFilterField.RO5_VIOLATIONS,
                operator=SmallMoleculeFilterOperator.IS_EMPTY,
            )
        )
    if max_rotatable_bonds is not None:
        rules.append(
            SmallMoleculeFilterRule(
                field=SmallMoleculeFilterField.ROTATABLE_BONDS,
                operator=SmallMoleculeFilterOperator.LTE,
                value=int(max_rotatable_bonds),
            )
        )
    if max_heavy_atoms is not None:
        rules.append(
            SmallMoleculeFilterRule(
                field=SmallMoleculeFilterField.HEAVY_ATOM_COUNT,
                operator=SmallMoleculeFilterOperator.LTE,
                value=int(max_heavy_atoms),
            )
        )
    return tuple(rules)


def evaluate_small_molecule_filter_values(mol) -> SmallMoleculeFilterValues:
    matches = _pains_catalog().GetMatches(mol)
    pains_matches = tuple(
        sorted(
            {
                str(getattr(match, "GetDescription", lambda: "")() or "").strip()
                for match in matches
                if str(getattr(match, "GetDescription", lambda: "")() or "").strip()
            }
        )
    )
    descriptors = calculate_basic_descriptors(mol)
    return SmallMoleculeFilterValues(
        pains_matches=pains_matches,
        ro5_violations=_ro5_violations(descriptors),
        descriptors=descriptors,
        n_atoms=int(mol.GetNumAtoms()),
    )


def annotate_row_with_small_molecule_filter_values(
        row: dict[str, Any],
        values: SmallMoleculeFilterValues,
) -> dict[str, Any]:
    row["mw"] = values.descriptors.get("mw")
    row["exact_mw"] = values.descriptors.get("exact_mw")
    row["logp"] = values.descriptors.get("logp")
    row["hbd"] = values.descriptors.get("hbd")
    row["hba"] = values.descriptors.get("hba")
    row["tpsa"] = values.descriptors.get("tpsa")
    row["rotatable_bonds"] = values.descriptors.get("rotatable_bonds")
    row["fragment_count"] = values.descriptors.get("fragment_count")
    row["ring_count"] = values.descriptors.get("ring_count")
    row["aromatic_ring_count"] = values.descriptors.get("aromatic_ring_count")
    row["hetero_atom_count"] = values.descriptors.get("hetero_atom_count")
    row["heavy_atom_count"] = values.descriptors.get("heavy_atom_count")
    row["formal_charge"] = values.descriptors.get("formal_charge")
    row["fraction_csp3"] = values.descriptors.get("fraction_csp3")
    row["n_atoms"] = int(values.n_atoms)
    row["pains_matches"] = list(values.pains_matches)
    row["ro5_violations"] = list(values.ro5_violations)
    row["status_flags"] = int(row.get("status_flags") or 0) | int(values.status_flags)
    return row


def small_molecule_filter_values_from_record(record: Any) -> SmallMoleculeFilterValues | None:
    descriptors = {
        field_name: getattr(record, field_name, None)
        for field_name in _NUMERIC_DESCRIPTOR_SPECS
    }
    n_atoms = getattr(record, "n_atoms", 0)
    pains_matches = tuple(str(item) for item in (getattr(record, "pains_matches", None) or []))
    ro5_violations = tuple(str(item) for item in (getattr(record, "ro5_violations", None) or []))
    if all(value in (None, "", 0) for value in descriptors.values()) and int(
            n_atoms or 0) <= 0 and not pains_matches and not ro5_violations:
        return None
    return SmallMoleculeFilterValues(
        pains_matches=pains_matches,
        ro5_violations=ro5_violations,
        descriptors={str(key): value for key, value in descriptors.items()},
        n_atoms=int(n_atoms or 0),
    )


def small_molecule_filter_values_from_record_or_file(record: Any) -> SmallMoleculeFilterValues | None:
    cached = small_molecule_filter_values_from_record(record)
    if cached is not None:
        return cached
    try:
        from rdkit import Chem
    except Exception:
        return None
    path = preferred_molecule_path(record)
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return None
    mol = Chem.MolFromMolFile(str(resolved), sanitize=True, removeHs=False)
    if mol is None:
        return None
    return evaluate_small_molecule_filter_values(mol)


__all__ = [
    "SMALL_MOLECULE_FILTER_TYPE",
    "SmallMoleculeFilterCriteria",
    "SmallMoleculeFilterField",
    "SmallMoleculeFilterOperator",
    "SmallMoleculeFilterRule",
    "SmallMoleculeFilterValues",
    "SMALL_MOLECULE_FIELD_SPECS",
    "annotate_row_with_small_molecule_filter_values",
    "build_small_molecule_prefilter_rules",
    "evaluate_small_molecule_filter_values",
    "passes_small_molecule_filter",
    "small_molecule_filter_values_from_record",
    "small_molecule_filter_values_from_record_or_file",
]
