from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class WorkflowRule:
    key: str
    label: str
    role_kinds: Mapping[str, tuple[str, ...]]


_WORKFLOW_ALIASES: dict[str, str] = {
    "autodock": "vina",
    "autodock_like": "vina",
    "autodockgpu": "vina",
    "smina": "vina",
    "gnina": "vina",
    "vina": "vina",
    "vina_vs": "vina",
    "virtual_screening": "vina",
    "qsar": "qsar",
    "qsar_small_molecule": "qsar",
    "protein_peptide": "protein_peptide",
    "protein_peptide_docking": "protein_peptide",
    "protein_protein": "protein_protein",
    "protein_protein_docking": "protein_protein",
    "ab_ag": "antibody_antigen",
    "antibody_antigen": "antibody_antigen",
}

_WORKFLOW_RULES: dict[str, WorkflowRule] = {
    "vina": WorkflowRule(
        key="vina",
        label="Vina Protein-Ligand Docking",
        role_kinds={
            "ligand": ("small_molecule", "macrocycle"),
            "receptor": ("protein", "unknown"),
        },
    ),
    "qsar": WorkflowRule(
        key="qsar",
        label="Small-Molecule QSAR",
        role_kinds={
            "ligand": ("small_molecule", "macrocycle"),
        },
    ),
    "protein_peptide": WorkflowRule(
        key="protein_peptide",
        label="Protein-Peptide Docking",
        role_kinds={
            "ligand": ("peptide",),
            "receptor": ("protein", "unknown"),
        },
    ),
    "protein_protein": WorkflowRule(
        key="protein_protein",
        label="Protein-Protein Docking",
        role_kinds={
            "ligand": ("protein", "antibody", "antigen"),
            "receptor": ("protein", "antibody", "antigen", "unknown"),
        },
    ),
    "antibody_antigen": WorkflowRule(
        key="antibody_antigen",
        label="Antibody-Antigen Docking",
        role_kinds={
            "ligand": ("antigen", "protein", "peptide"),
            "receptor": ("antibody", "protein", "unknown"),
        },
    ),
}


def normalize_workflow(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if not key:
        raise ValueError("workflow must not be empty.")
    normalized = _WORKFLOW_ALIASES.get(key)
    if normalized is None:
        supported = ", ".join(sorted(_WORKFLOW_RULES))
        raise ValueError(f"Unsupported workflow '{value}'. Supported workflows: {supported}")
    return normalized


def workflow_rule(value: str | None) -> WorkflowRule:
    return _WORKFLOW_RULES[normalize_workflow(value)]


def allowed_molecule_kinds(workflow: str, role: str) -> tuple[str, ...]:
    normalized_role = str(role or "").strip().lower()
    rule = workflow_rule(workflow)
    allowed = tuple(rule.role_kinds.get(normalized_role) or ())
    if not allowed:
        supported_roles = ", ".join(sorted(rule.role_kinds))
        raise ValueError(
            f"Workflow '{rule.key}' does not define compatibility for role '{role}'. "
            f"Supported roles: {supported_roles}"
        )
    return allowed


def workflow_filters(workflow: str, role: str) -> dict[str, object]:
    normalized_role = str(role or "").strip().lower()
    allowed_types = []
    for value in allowed_molecule_kinds(workflow, normalized_role):
        normalized = str(value or "").strip().lower()
        if normalized in {"small_molecule", "macrocycle"}:
            normalized = "small_molecule"
        allowed_types.append(normalized)
    allowed_types = list(dict.fromkeys(item for item in allowed_types if item))
    filters: dict[str, object] = {}
    if normalized_role == "ligand":
        filters["is_ligand"] = True
    elif normalized_role == "receptor":
        filters["is_receptor"] = True
    if allowed_types:
        filters["molecule_type__in"] = allowed_types
    return filters


def apply_workflow_filters(
    filters: Mapping[str, object] | None,
    *,
    workflow: str,
    role: str,
) -> dict[str, object]:
    merged = dict(filters or {})
    merged.update(workflow_filters(workflow, role))
    return merged


__all__ = [
    "WorkflowRule",
    "allowed_molecule_kinds",
    "apply_workflow_filters",
    "normalize_workflow",
    "workflow_filters",
    "workflow_rule",
]
