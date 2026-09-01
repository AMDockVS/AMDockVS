from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from amdockvs.chemistry.filtering import evaluate_small_molecule_filter_values
from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.molecule_paths import artifact_storage_path, get_default_project_root

_ORGANIC_ATOMIC_NUMBERS = {
    1, 5, 6, 7, 8, 9, 14, 15, 16, 17, 34, 35, 53,
}


def _has_metal_atoms(mol) -> bool:
    return any(
        atom.GetAtomicNum() > 0 and atom.GetAtomicNum() not in _ORGANIC_ATOMIC_NUMBERS
        for atom in mol.GetAtoms()
    )


def _is_simple_ion(mol) -> bool:
    atom_count = int(mol.GetNumAtoms() or 0)
    formal_charge = int(sum(atom.GetFormalCharge() for atom in mol.GetAtoms()))
    heavy_atoms = int(sum(1 for atom in mol.GetAtoms() if atom.GetAtomicNum() > 1))
    carbon_atoms = int(sum(1 for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6))
    return atom_count <= 1 or (heavy_atoms <= 2 and carbon_atoms == 0 and formal_charge != 0)


def _has_carbon(mol) -> bool:
    return any(atom.GetAtomicNum() == 6 for atom in mol.GetAtoms())


def _sanitize_fragment(mol):
    from rdkit import Chem

    frag = Chem.Mol(mol)
    Chem.SanitizeMol(frag)
    return frag


def _component_role(mol) -> str:
    atom_count = int(mol.GetNumAtoms() or 0)
    if atom_count == 1 and all(atom.GetAtomicNum() == 1 for atom in mol.GetAtoms()):
        return "detached_proton"
    if _is_simple_ion(mol):
        return "associated_ion"
    if _has_metal_atoms(mol):
        return "associated_component"
    return "fragment"


def _formula(mol) -> str:
    from rdkit.Chem import rdMolDescriptors

    try:
        return str(rdMolDescriptors.CalcMolFormula(mol) or "")
    except Exception:
        return ""


def _canonical_smiles(mol) -> str:
    from rdkit import Chem

    try:
        return str(Chem.MolToSmiles(mol, canonical=True) or "")
    except Exception:
        return ""


def _component_record(
    *,
    mol,
    fragment_index: int,
    project_root: Path,
    storage_root: Path,
    role: str,
    storage_key: str,
    artifact_name: str,
) -> dict[str, Any]:
    path = artifact_storage_path(
        storage_root,
        role=role,
        key=storage_key,
        artifact_name=artifact_name,
        suffix=".sdf",
    )
    # The path is recorded now and written later (see write_fragment_files): a molecule rejected
    # by a downstream filter must not leave fragment files behind.
    values = evaluate_small_molecule_filter_values(mol)
    return {
        "fragment_index": int(fragment_index),
        "role": _component_role(mol),
        "path": str(path.relative_to(project_root)),
        "smiles": _canonical_smiles(mol),
        "formula": _formula(mol),
        "atom_count": int(mol.GetNumAtoms() or 0),
        "heavy_atom_count": int(mol.GetNumHeavyAtoms() or 0),
        "formal_charge": int(sum(atom.GetFormalCharge() for atom in mol.GetAtoms())),
        "contains_metal": bool(_has_metal_atoms(mol)),
        "is_simple_ion": bool(_is_simple_ion(mol)),
        "has_carbon": bool(_has_carbon(mol)),
        "state": dict(molecule_state_metadata(mol).get("state") or {}),
        "properties": {
            "n_atoms": int(mol.GetNumAtoms() or 0),
            "mw": values.descriptors.get("mw"),
            "exact_mw": values.descriptors.get("exact_mw"),
            "logp": values.descriptors.get("logp"),
            "hbd": values.descriptors.get("hbd"),
            "hba": values.descriptors.get("hba"),
            "tpsa": values.descriptors.get("tpsa"),
            "rotatable_bonds": values.descriptors.get("rotatable_bonds"),
            "fragment_count": values.descriptors.get("fragment_count"),
            "ring_count": values.descriptors.get("ring_count"),
            "aromatic_ring_count": values.descriptors.get("aromatic_ring_count"),
            "hetero_atom_count": values.descriptors.get("hetero_atom_count"),
            "heavy_atom_count": values.descriptors.get("heavy_atom_count"),
            "formal_charge": values.descriptors.get("formal_charge"),
            "fraction_csp3": values.descriptors.get("fraction_csp3"),
            "pains_matches": list(values.pains_matches),
            "ro5_violations": list(values.ro5_violations),
        },
    }


def largest_ligand_fragment(mol):
    """The fragment `analyze_ligand_fragments` would keep — largest organic, else largest
    non-ion — computed in memory (no files). Used to pre-filter on the fragment that survives
    import, not the raw multi-fragment record. Returns the input mol if it's a single fragment.
    """
    from rdkit import Chem

    frags = Chem.GetMolFrags(Chem.Mol(mol), asMols=True, sanitizeFrags=False)
    if len(frags) <= 1:
        return mol
    sanitized = []
    for frag in frags:
        try:
            sanitized.append(_sanitize_fragment(frag))
        except Exception:
            continue
    candidates = [f for f in sanitized if _component_role(f) == "fragment"] or sanitized
    if not candidates:
        return mol
    organic = [f for f in candidates if _has_carbon(f)] or candidates
    return max(
        organic,
        key=lambda f: (int(f.GetNumHeavyAtoms() or 0), int(f.GetNumAtoms() or 0), _canonical_smiles(f)),
    )


def analyze_ligand_fragments(
    *,
    mol,
    storage_root: str | Path,
    role: str,
    storage_key: str,
    project_root: Path | None = None,
) -> tuple[dict[str, Any], dict[int, Any]] | None:
    """Split a ligand into fragments and pick the one to keep — in memory, writing nothing.

    Returns ``(fragmentation_metadata, {fragment_index: Mol})``. The metadata already carries the
    path each fragment *will* have; call ``write_fragment_files`` once the molecule has survived
    every filter. Splitting analysis from writing is what lets a late rejection cost zero files.
    """
    from rdkit import Chem

    effective_project_root = project_root or get_default_project_root()
    if effective_project_root is None:
        raise ValueError("Fragment analysis requires a project root.")
    resolved_storage_root = Path(storage_root).expanduser().resolve()
    fragments = Chem.GetMolFrags(Chem.Mol(mol), asMols=True, sanitizeFrags=False)
    if not fragments:
        return None

    component_rows: list[dict[str, Any]] = []
    fragment_mols: dict[int, Any] = {}
    problem_flags: list[str] = []
    for index, fragment in enumerate(fragments, start=1):
        try:
            sanitized = _sanitize_fragment(fragment)
        except Exception:
            problem_flags.append(f"unsanitizable_fragment_{index}")
            continue
        fragment_mols[index] = sanitized
        component_rows.append(
            _component_record(
                mol=sanitized,
                fragment_index=index,
                project_root=effective_project_root,
                storage_root=resolved_storage_root,
                role=role,
                storage_key=storage_key,
                artifact_name=f"fragment_{index:03d}",
            )
        )

    if not component_rows:
        return None

    candidate_rows = [row for row in component_rows if row.get("role") == "fragment"]
    organic_candidates = [row for row in candidate_rows if bool(row.get("has_carbon"))]
    ranked = organic_candidates or candidate_rows
    if not ranked:
        return (
            {
                "selected_fragment_index": None,
                "selection_strategy": "none",
                "problem_flags": sorted(set(problem_flags + ["no_valid_ligand_fragment"])),
                "components": component_rows,
            },
            fragment_mols,
        )

    selected = sorted(
        ranked,
        key=lambda row: (
            int(row.get("heavy_atom_count") or 0),
            int(row.get("atom_count") or 0),
            str(row.get("smiles") or ""),
        ),
        reverse=True,
    )[0]
    selected_index = int(selected.get("fragment_index") or 0)
    for row in component_rows:
        if int(row.get("fragment_index") or 0) == selected_index:
            row["role"] = "selected_fragment"
            break

    detached_protons = sum(1 for row in component_rows if str(row.get("role") or "") == "detached_proton")
    associated_ions = sum(1 for row in component_rows if str(row.get("role") or "") == "associated_ion")
    associated_components = sum(
        1 for row in component_rows if str(row.get("role") or "") == "associated_component"
    )
    unique_nonselected = {
        str(row.get("smiles") or "")
        for row in component_rows
        if int(row.get("fragment_index") or 0) != selected_index and str(row.get("smiles") or "").strip()
    }
    if detached_protons:
        problem_flags.append("detached_protons")
    if associated_ions:
        problem_flags.append("associated_ions")
    if associated_components:
        problem_flags.append("associated_components")
    if len(unique_nonselected) > 1:
        problem_flags.append("multiple_distinct_nonselected_components")

    return (
        {
            "selected_fragment_index": selected_index,
            "selection_strategy": "largest_organic_fragment" if organic_candidates else "largest_non_ion_fragment",
            "problem_flags": sorted(set(problem_flags)),
            "components": component_rows,
            "detached_proton_count": detached_protons,
            "associated_ion_count": associated_ions,
            "associated_component_count": associated_components,
        },
        fragment_mols,
    )


def write_fragment_files(
    metadata: Mapping[str, Any],
    fragment_mols: Mapping[int, Any],
    *,
    project_root: Path | None = None,
) -> None:
    """Write the .sdf each component record already points at. Call only for survivors."""
    from rdkit import Chem

    effective_project_root = project_root or get_default_project_root()
    if effective_project_root is None:
        raise ValueError("Writing fragment files requires a project root.")
    for record in list(metadata.get("components") or []):
        mol = fragment_mols.get(int(record.get("fragment_index") or 0))
        if mol is None:
            continue
        path = Path(effective_project_root) / str(record.get("path") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        Chem.MolToMolFile(mol, str(path))


def fragment_entries_from_metadata(raw_metadata: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    metadata = dict(raw_metadata or {})
    fragment_block = metadata.get("fragmentation")
    if not isinstance(fragment_block, Mapping):
        return []
    return [dict(item) for item in list(fragment_block.get("components") or []) if isinstance(item, Mapping)]


def selected_fragment_from_metadata(raw_metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    metadata = dict(raw_metadata or {})
    fragment_block = metadata.get("fragmentation")
    if not isinstance(fragment_block, Mapping):
        return None
    selected_index = int(fragment_block.get("selected_fragment_index") or 0)
    if selected_index <= 0:
        return None
    for item in fragment_entries_from_metadata(metadata):
        if int(item.get("fragment_index") or 0) == selected_index:
            return item
    return None


def fragment_entry_by_index(raw_metadata: Mapping[str, Any] | None, fragment_index: int) -> dict[str, Any] | None:
    target_index = int(fragment_index or 0)
    if target_index <= 0:
        return None
    for item in fragment_entries_from_metadata(raw_metadata):
        if int(item.get("fragment_index") or 0) == target_index:
            return item
    return None


__all__ = [
    "analyze_ligand_fragments",
    "largest_ligand_fragment",
    "fragment_entries_from_metadata",
    "fragment_entry_by_index",
    "selected_fragment_from_metadata",
    "write_fragment_files",
]
