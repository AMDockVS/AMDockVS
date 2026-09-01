from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from amdockvs.chemistry.filtering import (
    SmallMoleculeFilterCriteria,
    build_small_molecule_prefilter_rules,
)
from amdockvs.io._common import normalize_context, normalize_kind, normalize_molecule_kind, normalize_role

# Concentration units -> molar; pX = -log10(M). ponytail: mirrors qsar.api._to_pchem (4 lines,
# duplicated to keep io out of the qsar/sklearn import path).
_UNIT_TO_MOLAR = {"m": 1.0, "mm": 1e-3, "um": 1e-6, "µm": 1e-6, "nm": 1e-9, "pm": 1e-12, "": 1.0}


def _to_pchem(value: float, unit: str) -> float | None:
    factor = _UNIT_TO_MOLAR.get(str(unit or "").strip().lower())
    if factor is None or value <= 0:
        return None
    return round(-math.log10(value * factor), 4)


@lru_cache(maxsize=8)
def _load_qsar_fitted(artifact_path: str):
    from amdockvs.qsar.modeling import load_model

    return load_model(artifact_path)


class ImportPrefilterPolicy(BaseModel):
    target_molecule_kinds: tuple[str, ...] = ("small_molecule",)
    exclude_pains: bool = False
    require_ro5: bool = False
    max_rotatable_bonds: int | None = Field(default=None, ge=0)
    max_heavy_atoms: int | None = Field(default=None, ge=1)
    # Structure prep applied to the kept ligand fragment at import (each only if missing).
    add_hs: bool = False
    gen_3d: bool = False
    canonical_tautomer: bool = False  # standardize to ONE canonical tautomer (not enumeration)
    # HTP import-mode extensions: richer pre-materialization filters so huge libraries
    # (tens of millions) are culled while streaming, before any DB row or file is written.
    max_ro5_violations: int | None = Field(default=None, ge=0)
    property_ranges: dict[str, tuple[float | None, float | None]] = Field(default_factory=dict)
    include_smarts: tuple[str, ...] = ()
    exclude_smarts: tuple[str, ...] = ()
    qsar_model_path: str = ""   # absolute path to a joblib FittedModel artifact
    qsar_op: str = ">="
    qsar_threshold: float | None = None
    # Activity-from-SDF-tag: capture a ligand's activity straight from an SDF property at import.
    activity_property: str = ""   # SDF tag to read the value from (e.g. "IC50", "pIC50")
    activity_endpoint: str = ""   # endpoint name (defaults to the property name)
    activity_unit: str = ""       # unit of the raw value (used by the transform)
    activity_transform: str = ""  # "" | "pIC50" | "pKi" | … -> pX = -log10(M)
    # Multi-column activities: many CSV columns each become an endpoint (e.g. Tox21's 12 assays).
    # activity_kinds maps a column -> "categorical"|"continuous" (decided by the UI from the file).
    activity_columns: tuple[str, ...] = ()
    activity_kinds: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_mapping(cls, raw: Any) -> Any:
        if raw is None or isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return raw
        payload = dict(raw)
        criteria = payload.pop("criteria", None)
        if isinstance(criteria, dict):
            payload = {**criteria, **payload}
        if "max_atoms" in payload and "max_heavy_atoms" not in payload:
            payload["max_heavy_atoms"] = payload.pop("max_atoms")
        if "molecule_kinds" in payload and "target_molecule_kinds" not in payload:
            payload["target_molecule_kinds"] = payload.pop("molecule_kinds")
        if "applies_to_molecule_kinds" in payload and "target_molecule_kinds" not in payload:
            payload["target_molecule_kinds"] = payload.pop("applies_to_molecule_kinds")
        if "target_molecule_kinds" not in payload:
            payload["target_molecule_kinds"] = ("small_molecule",)
        return payload

    @field_validator("target_molecule_kinds", mode="before")
    @classmethod
    def _normalize_targets(cls, value: Any) -> tuple[str, ...]:
        items = value if isinstance(value, (list, tuple, set)) else (value,)
        normalized = tuple(
            str(item or "").strip().lower()
            for item in items
            if str(item or "").strip()
        )
        return normalized or ("small_molecule",)

    def is_active(self) -> bool:
        return self.to_criteria().is_active() or self._htp_active()

    def prep_active(self) -> bool:
        """Any structure-prep step is on — the policy must reach the materializer even when it
        filters nothing."""
        return bool(self.add_hs or self.gen_3d or self.canonical_tautomer)

    def activity_spec_from_properties(self, source_properties) -> dict[str, Any] | None:
        """Build an activity_spec from an SDF tag (source_properties = [{key, value_text}]).
        Returns None if no activity_property is set or the tag is missing/unparseable. Applies the
        pIC50/pKi transform when requested. Used by the materializer right after it reads the tags."""
        prop = str(self.activity_property or "").strip()
        if not prop:
            return None
        value_text = next(
            (sp.get("value_text") for sp in (source_properties or []) if str(sp.get("key")) == prop),
            None,
        )
        if value_text is None:
            return None
        try:
            raw = float(str(value_text).strip())
        except (TypeError, ValueError):
            return None
        unit = str(self.activity_unit or "").strip()
        if self.activity_transform:
            transformed = _to_pchem(raw, unit)
            if transformed is None:
                return None
            # blank endpoint + transform -> name the endpoint after the transform (pIC50), not the
            # raw tag, since the stored value is the transformed one.
            endpoint = str(self.activity_endpoint or self.activity_transform).strip()
            return {
                "value": transformed,
                "unit": self.activity_transform,
                "activity_type": endpoint,
                "description": f"raw={raw} {unit}".strip(),
                "source": f"sdf:{prop}",
            }
        endpoint = str(self.activity_endpoint or prop).strip()
        return {"value": raw, "unit": unit, "activity_type": endpoint, "source": f"sdf:{prop}"}

    def activity_specs_from_properties(self, source_properties) -> list[dict[str, Any]]:
        """All activity specs for one molecule: the single 'Activity from tag' (if set) plus one per
        column in activity_columns (each a separate endpoint). Reads each column's value from
        source_properties; skips missing/non-numeric cells so a sparse assay matrix loads cleanly.
        kind comes from activity_kinds (decided once by the UI over the whole file)."""
        specs: list[dict[str, Any]] = []
        single = self.activity_spec_from_properties(source_properties)
        if single is not None:
            specs.append(single)
        prop = str(self.activity_property or "").strip()
        by_key = {str(sp.get("key")): sp.get("value_text") for sp in (source_properties or [])}
        for column in self.activity_columns:
            col = str(column or "").strip()
            if not col or col == prop:   # the single-tag column is already handled above
                continue
            value_text = by_key.get(col)
            if value_text is None:
                continue
            try:
                raw = float(str(value_text).strip())
            except (TypeError, ValueError):
                continue
            specs.append({
                "value": raw,
                "unit": "",
                "activity_type": col,
                "kind": str(self.activity_kinds.get(col, "continuous")),
                "source": f"col:{col}",
            })
        return specs

    def _htp_active(self) -> bool:
        return bool(
            self.include_smarts
            or self.exclude_smarts
            or self.property_ranges
            or self.max_ro5_violations is not None
            or (self.qsar_model_path and self.qsar_threshold is not None)
        )

    def _htp_config(self):
        from amdockvs.htp.screening import HTPFilterConfig

        ranges = {k: tuple(v) for k, v in (self.property_ranges or {}).items()}
        if self.max_rotatable_bonds is not None:
            ranges.setdefault("rotatable_bonds", (None, float(self.max_rotatable_bonds)))
        if self.max_heavy_atoms is not None:
            ranges.setdefault("heavy_atom_count", (None, float(self.max_heavy_atoms)))
        max_ro5 = self.max_ro5_violations
        if max_ro5 is None and self.require_ro5:
            max_ro5 = 0
        return HTPFilterConfig(
            exclude_pains=bool(self.exclude_pains),
            max_ro5_violations=max_ro5,
            property_ranges=ranges,
            include_smarts=tuple(self.include_smarts),
            exclude_smarts=tuple(self.exclude_smarts),
            qsar_op=self.qsar_op,
            qsar_threshold=self.qsar_threshold,
        )

    def _qsar_predict(self):
        if not self.qsar_model_path or self.qsar_threshold is None:
            return None
        import numpy as np

        fitted = _load_qsar_fitted(str(self.qsar_model_path))
        features = tuple(fitted.feature_names)

        def predict(descriptors: dict) -> float:
            vector = [descriptors.get(name) for name in features]
            if any(value is None for value in vector):
                return float("nan")
            return float(fitted.predict(np.asarray([vector], dtype=float))[0])

        return predict

    def mol_filter(self):
        """Build a single ``passes(mol) -> bool`` predicate with config + QSAR model bound once,
        for the import stream to call per molecule (before any file/DB write)."""
        from amdockvs.htp.screening import evaluate_mol

        config = self._htp_config()
        qsar_predict = self._qsar_predict()

        def passes(mol) -> bool:
            return evaluate_mol(mol, config, qsar_predict=qsar_predict)[0] is None

        return passes

    def applies_to(self, molecule_kind: str) -> bool:
        normalized_kind = str(molecule_kind or "").strip().lower()
        return normalized_kind in set(self.target_molecule_kinds)

    def to_criteria(self) -> SmallMoleculeFilterCriteria:
        return SmallMoleculeFilterCriteria(
            rules=build_small_molecule_prefilter_rules(
                exclude_pains=bool(self.exclude_pains),
                require_ro5=bool(self.require_ro5),
                max_rotatable_bonds=self.max_rotatable_bonds,
                max_heavy_atoms=self.max_heavy_atoms,
            )
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "target_molecule_kinds": list(self.target_molecule_kinds),
            "criteria": self.to_criteria().to_mapping(),
        }


class ImportBatchPayload(BaseModel):
    kind: str
    file_path: Path
    storage_dir: Path
    input_format: str
    primary_role: str = ""
    primary_context: str = "general"
    molecule_kind: str = "unknown"
    prefilter: ImportPrefilterPolicy | None = None
    extra_data_patch: dict[str, Any] = Field(default_factory=dict)
    binding_site_specs: list[dict[str, Any]] = Field(default_factory=list)
    # The chunk travels as a BYTE RANGE of `file_path` (`span_offset`..`span_end`): the worker
    # re-reads its records there and parses them with RDKit. It used to carry the raw text in
    # `records`, i.e. a whole second copy of the library inside the executor payloads (a 10 GB
    # SDF => 10 GB of payloads). `records` is kept for formats without a separator (a lone PDB)
    # and for old payloads; `entries` is the legacy pre-parsed shape. `parse_config` is
    # format-specific (SMILES delimiter/columns/header).
    span_offset: int = -1
    span_end: int = -1
    span_first_index: int = 0
    span_count: int = 0
    records: list[dict[str, Any]] = Field(default_factory=list)
    parse_config: dict[str, Any] = Field(default_factory=dict)
    entries: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: Any) -> str:
        return normalize_kind(value)

    @field_validator("primary_role", mode="before")
    @classmethod
    def _normalize_role(cls, value: Any) -> str:
        return normalize_role(value)

    @field_validator("primary_context", mode="before")
    @classmethod
    def _normalize_context(cls, value: Any) -> str:
        return normalize_context(value)

    @field_validator("input_format", mode="before")
    @classmethod
    def _normalize_input_format(cls, value: Any) -> str:
        return str(value or "").strip().lower()

    @model_validator(mode="after")
    def _normalize_molecule_kind(self) -> "ImportBatchPayload":
        self.molecule_kind = normalize_molecule_kind(self.molecule_kind, kind=self.kind)
        return self


class MultithreadedSDFImportPayload(BaseModel):
    kind: str
    file_path: Path
    storage_dir: Path
    num_threads: int = Field(default=4, ge=1)
    primary_role: str = ""
    primary_context: str = "general"
    molecule_kind: str = "unknown"
    prefilter: ImportPrefilterPolicy | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: Any) -> str:
        return normalize_kind(value)

    @field_validator("primary_role", mode="before")
    @classmethod
    def _normalize_role(cls, value: Any) -> str:
        return normalize_role(value)

    @field_validator("primary_context", mode="before")
    @classmethod
    def _normalize_context(cls, value: Any) -> str:
        return normalize_context(value)

    @model_validator(mode="after")
    def _normalize_molecule_kind(self) -> "MultithreadedSDFImportPayload":
        self.molecule_kind = normalize_molecule_kind(self.molecule_kind, kind=self.kind)
        return self


__all__ = [
    "ImportBatchPayload",
    "ImportPrefilterPolicy",
    "MultithreadedSDFImportPayload",
]
