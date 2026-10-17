"""Pure import core: decide whether a molecule gets in, and with what values. Writes nothing.

In goes a ``Mol`` (or a raw row) plus the import policy; out comes the decision and the row
fields. No file, no session: that is why a late rejection costs zero writes, and why the same
code serves a normal import, an HTP shard or a multi-step job.

Building the structure payloads of the row lives in ``io/transformers/rows.py`` and only
runs over what survives here.

    python -m amdockvs.io.rows   # runs the checks
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from amdockvs.chemistry.descriptors import canonical_smiles, inchikey
from amdockvs.chemistry.filtering import evaluate_small_molecule_filter_values
from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.io.import_stats import FILTERED_PROPERTY, NO_VALID_FRAGMENT
from amdockvs.io.payloads import ImportPrefilterPolicy
from amdockvs.core.rows import sanitize_molecule_extra_data
from amdockvs.core.vocab import MoleculeType
from amdockvs.molecules.fragments import largest_ligand_fragment, ligand_fragments

# Descriptor columns of a small molecule, computed on the structure it keeps.
FRAGMENT_PROPERTY_FIELDS = (
    "mw",
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
    """What survived every filter, still in memory and without a single payload built. Only the
    kept structure is ever stored; the other components stay listed in ``metadata``."""

    kept_mol: Any
    metadata: dict[str, Any]
    filter_values: Any = None  # the criteria's report, so the row does not compute it again


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
    mol BEFORE any payload or DB write so huge libraries are culled cheaply. None when inactive."""
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
    in memory, before any payload is built so a later rejection costs nothing."""
    if prefilter is None or not prefilter.prep_active():
        return mol

    from amdockvs.chemistry.standardization import prepare_import_structure

    prepped, _changed = prepare_import_structure(
        mol,
        add_hs=bool(prefilter.add_hs),
        gen_3d=bool(prefilter.gen_3d),
        canonical_tautomer=bool(prefilter.canonical_tautomer),
    )
    return prepped


def evaluate_ligand_mol(
    *,
    mol,
    metadata: dict[str, Any] | None,
    prefilter: ImportPrefilterPolicy | None = None,
    criteria=None,
    molecule_kind: str = "",
) -> tuple[LigandDecision | None, str | None]:
    """Everything that can reject the molecule, in memory: fragmentation, prep and criteria.

    Returns ``(decision, None)`` or ``(None, reason)``. Order matters: the optional prep runs on
    the fragment that will be stored and *before* the property gate, because the criteria have to
    judge the molecule that actually gets stored.
    """
    analysis = ligand_fragments(mol)
    if analysis is None or not analysis[0]["selected_fragment_index"]:
        return None, NO_VALID_FRAGMENT
    fragmentation, fragment_mols = analysis
    resolved_metadata = dict(metadata or {})
    resolved_metadata["fragmentation"] = fragmentation

    kept_mol = prep_ligand_mol(fragment_mols[fragmentation["selected_fragment_index"]], prefilter)
    report = None
    if criteria is not None:
        report = small_molecule_filter_values(kept_mol, molecule_kind=molecule_kind)
        if report is not None and not report.passes(criteria):
            return None, FILTERED_PROPERTY
    return LigandDecision(kept_mol=kept_mol, metadata=resolved_metadata, filter_values=report), None


def structure_fields(mol, values=None) -> dict[str, Any]:
    """The molecule columns that describe a small molecule's current structure."""
    values = values or evaluate_small_molecule_filter_values(mol)
    state = dict(molecule_state_metadata(mol).get("state") or {})
    return {
        "n_atoms": int(mol.GetNumAtoms() or 0),
        **{key: values.descriptors.get(key) for key in FRAGMENT_PROPERTY_FIELDS},
        "pains_matches": list(values.pains_matches or []),
        "ro5_violations": list(values.ro5_violations or []),
        "has_3d": bool(state.get("has_3d")),
        "has_hs": bool(state.get("has_hs")),
        "conformer_count": int(state.get("conformer_count") or 0),
        "canonical_smiles": canonical_smiles(mol) or None,
        "inchikey": inchikey(mol) or None,
    }


def ligand_row_fields(row: dict[str, Any], decision: LigandDecision) -> dict[str, Any]:
    """Dumps the decision onto the row. Mutates and returns `row`, as the original code did."""
    import json

    row.update(structure_fields(decision.kept_mol, decision.filter_values))
    row["extra_data"] = decision.metadata
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
    "prep_ligand_mol",
    "small_molecule_filter_values",
    "structure_fields",
]
