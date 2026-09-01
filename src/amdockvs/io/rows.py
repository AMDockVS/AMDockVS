"""Pure import core: decide whether a molecule gets in, and with what values. Writes nothing.

In goes a ``Mol`` (or a raw row) plus the import policy; out comes the decision and the row
fields. No file, no session: that is why a late rejection costs zero writes, and why the same
code serves a normal import, an HTP shard or a multi-step job.

Materialisation (writing the .sdf, the fragments, the relative path) lives in
``io/transformers/materializers.py`` and only runs over what survives here.

    python -m amdockvs.io.rows   # runs the checks
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from amdockvs.chemistry.filtering import evaluate_small_molecule_filter_values
from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.io.import_stats import FILTERED_PROPERTY, NO_VALID_FRAGMENT
from amdockvs.io.payloads import ImportPrefilterPolicy
from amdockvs.models import MoleculeRecord
from amdockvs.models.molecules import MoleculeType, sanitize_molecule_extra_data
from amdockvs.molecules.fragments import (
    analyze_ligand_fragments,
    largest_ligand_fragment,
    selected_fragment_from_metadata,
)

# Properties the chosen fragment contributes to the row as-is.
FRAGMENT_PROPERTY_FIELDS = (
    "mw",
    "exact_mw",
    "logp",
    "hbd",
    "hba",
    "tpsa",
    "rotatable_bonds",
    "fragment_count",
    "ring_count",
    "aromatic_ring_count",
    "hetero_atom_count",
    "heavy_atom_count",
    "formal_charge",
    "fraction_csp3",
)


@dataclass(frozen=True)
class LigandDecision:
    """What survived every filter, still in memory and without a single file written.

    ``fragment_info`` already carries the path each fragment will have; writing them is the job
    of ``write_fragment_files``, which is only called for survivors.
    """

    kept_mol: Any
    metadata: dict[str, Any]
    fragment_info: dict[str, Any]
    fragment_mols: dict[int, Any]
    selected_fragment: dict[str, Any]
    prepped_state: dict[str, Any] | None


def molecule_columns() -> tuple[str, ...]:
    return tuple(column.name for column in MoleculeRecord.__table__.columns if not column.primary_key)


def metadata_map_from_row(row: dict[str, Any]) -> dict[str, Any]:
    metadata = sanitize_molecule_extra_data(row.get("extra_data"))

    for key in ("smiles", "sequence_1d"):
        value = row.get(key)
        if value not in (None, ""):
            metadata[key] = value
    return metadata


def active_small_molecule_criteria(prefilter: ImportPrefilterPolicy | None, molecule_kind: str):
    if prefilter is None or not prefilter.is_active():
        return None
    if not prefilter.applies_to(molecule_kind):
        return None
    return prefilter.to_criteria()


def small_molecule_filter_values(mol, *, molecule_kind: str):
    if str(molecule_kind or "").strip().lower() != MoleculeType.SMALL_MOLECULE:
        return None
    return evaluate_small_molecule_filter_values(mol)


def htp_mol_filter(prefilter: ImportPrefilterPolicy | None, molecule_kind: str):
    """A ``passes(mol) -> bool`` predicate for HTP import filtering, evaluated on the in-memory
    mol BEFORE any file/DB write so huge libraries are culled cheaply. None when inactive."""
    if prefilter is None or not prefilter.is_active() or not prefilter.applies_to(molecule_kind):
        return None
    return prefilter.mol_filter()


def cull_mol(mol, batch):
    """The mol the pre-materialization cull should judge. For a small-molecule ligand that's
    the fragment import will actually keep — filtering the raw multi-fragment record double-
    counts rotatable bonds / heavy atoms and wrongly rejects a molecule split across fragments.
    """
    is_ligand_sm = (
        str(batch.primary_role or batch.kind).strip().lower() == "ligand"
        and str(batch.molecule_kind or "").strip().lower() == MoleculeType.SMALL_MOLECULE
    )
    return largest_ligand_fragment(mol) if is_ligand_sm else mol


def prep_ligand_mol(mol, prefilter: ImportPrefilterPolicy | None):
    """Run the opt-in prep steps (add Hs / gen 3D / canonical tautomer) on the kept fragment,
    in memory. Returns ``(mol, state | None)`` — state is None when no step was requested or
    nothing changed. Prep happens before any write so a later rejection costs no file."""
    if prefilter is None or not prefilter.prep_active():
        return mol, None

    from amdockvs.chemistry.standardization import prepare_import_structure

    prepped, changed = prepare_import_structure(
        mol,
        add_hs=bool(prefilter.add_hs),
        gen_3d=bool(prefilter.gen_3d),
        canonical_tautomer=bool(prefilter.canonical_tautomer),
    )
    if not changed:
        return mol, None
    state = dict(molecule_state_metadata(prepped).get("state") or {})
    state["n_atoms"] = int(prepped.GetNumAtoms() or 0)
    return prepped, state


def evaluate_ligand_mol(
    *,
    mol,
    metadata: dict[str, Any] | None,
    storage_root: Path,
    role: str,
    storage_key: str,
    project_root: Path,
    prefilter: ImportPrefilterPolicy | None = None,
    criteria=None,
    molecule_kind: str = "",
) -> tuple[LigandDecision | None, str | None]:
    """Everything that can reject the molecule, in memory: fragmentation, prep and criteria.

    Returns ``(decision, None)`` or ``(None, reason)``. Order matters: the optional prep runs on
    the fragment that will be stored and *before* the property gate, because the criteria have to
    judge the molecule that actually gets stored.
    """
    analysis = analyze_ligand_fragments(
        mol=mol,
        storage_root=storage_root,
        role=role,
        storage_key=storage_key,
        project_root=project_root,
    )
    if analysis is None:
        return None, NO_VALID_FRAGMENT
    fragment_info, fragment_mols = analysis
    resolved_metadata = dict(metadata or {})
    resolved_metadata["fragmentation"] = fragment_info
    selected_fragment = selected_fragment_from_metadata(resolved_metadata)
    if selected_fragment is None:
        return None, NO_VALID_FRAGMENT
    selected_mol = fragment_mols.get(int(selected_fragment.get("fragment_index") or 0))
    if selected_mol is None:
        return None, NO_VALID_FRAGMENT

    kept_mol, prepped_state = prep_ligand_mol(selected_mol, prefilter)
    if criteria is not None:
        report = small_molecule_filter_values(kept_mol, molecule_kind=molecule_kind)
        if report is not None and not report.passes(criteria):
            return None, FILTERED_PROPERTY

    return (
        LigandDecision(
            kept_mol=kept_mol,
            metadata=resolved_metadata,
            fragment_info=fragment_info,
            fragment_mols=fragment_mols,
            selected_fragment=selected_fragment,
            prepped_state=prepped_state,
        ),
        None,
    )


def ligand_row_fields(
    row: dict[str, Any], decision: LigandDecision, *, current_path_rel: str
) -> dict[str, Any]:
    """Dumps the decision onto the row. Mutates and returns `row`, as the original code did."""
    import json

    properties = dict(decision.selected_fragment.get("properties") or {})
    state = dict(decision.selected_fragment.get("state") or {})

    row["extra_data"] = decision.metadata
    row["current_path"] = current_path_rel
    row["n_atoms"] = int(properties.get("n_atoms") or row.get("n_atoms") or 0)
    for key in FRAGMENT_PROPERTY_FIELDS:
        row[key] = properties.get(key)
    row["pains_matches"] = list(properties.get("pains_matches") or [])
    row["ro5_violations"] = list(properties.get("ro5_violations") or [])
    row["has_3d"] = bool(state.get("has_3d", row.get("has_3d")))
    row["has_hs"] = bool(state.get("has_hs", row.get("has_hs")))
    row["conformer_count"] = int(state.get("conformer_count", row.get("conformer_count") or 0) or 0)
    if decision.prepped_state is not None:  # prep's state overrides what the raw fragment reported
        row["has_3d"] = bool(decision.prepped_state.get("has_3d", row["has_3d"]))
        row["has_hs"] = bool(decision.prepped_state.get("has_hs", row["has_hs"]))
        row["conformer_count"] = int(decision.prepped_state.get("conformer_count") or 0)
        row["n_atoms"] = int(decision.prepped_state.get("n_atoms") or row["n_atoms"])
    row["metadata_json"] = json.dumps(decision.metadata, ensure_ascii=True)
    return row


__all__ = [
    "FRAGMENT_PROPERTY_FIELDS",
    "LigandDecision",
    "active_small_molecule_criteria",
    "cull_mol",
    "evaluate_ligand_mol",
    "htp_mol_filter",
    "ligand_row_fields",
    "metadata_map_from_row",
    "molecule_columns",
    "prep_ligand_mol",
    "small_molecule_filter_values",
]
