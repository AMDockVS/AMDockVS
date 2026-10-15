from __future__ import annotations

from typing import Any, Mapping

from amdockvs.chemistry.descriptors import canonical_smiles as _canonical_smiles

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


def largest_ligand_fragment(mol):
    """The fragment import keeps (see `ligand_fragments`); the input mol itself when it has a
    single fragment or none worth keeping."""
    from rdkit import Chem

    if len(Chem.GetMolFrags(mol, sanitizeFrags=False)) <= 1:
        return mol
    analysis = ligand_fragments(mol)
    index = analysis[0]["selected_fragment_index"] if analysis is not None else None
    return analysis[1][index] if index else mol


# A co-component enters as its own molecule only if it is comparable in size to the one kept.
# ponytail: one ratio, no absolute floor — a counterion (acetate, TFA, mesylate) is always a
# small fraction of the drug it salts. Raise it if borderline co-crystals slip through.
SPLIT_MIN_SIZE_RATIO = 0.5


def extra_fragment_molecules(mol) -> list[tuple[int, Any]]:
    """`(fragment_index, mol)` for each co-component worth importing on its own.

    `largest_ligand_fragment` keeps one component and the rest is lost; with `split_fragments`
    on, these come back as molecules. A record with two real fragments yields its second one;
    a salt yields nothing, because a counterion is small next to what it salts.

    Filtered out: non-organic and ionic components, duplicates of the kept fragment or of each
    other (canonical SMILES), and anything under `SPLIT_MIN_SIZE_RATIO` of the kept fragment.
    `fragment_index` matches the parent's `fragmentation.components`, so the row can point back.
    """
    from rdkit import Chem

    frags = Chem.GetMolFrags(Chem.Mol(mol), asMols=True, sanitizeFrags=False)
    if len(frags) <= 1:
        return []
    kept = largest_ligand_fragment(mol)
    kept_smiles = _canonical_smiles(kept)
    min_heavy_atoms = SPLIT_MIN_SIZE_RATIO * int(kept.GetNumHeavyAtoms() or 0)
    seen = {kept_smiles} if kept_smiles else set()
    extras: list[tuple[int, Any]] = []
    for index, frag in enumerate(frags, start=1):
        try:
            sanitized = _sanitize_fragment(frag)
        except Exception:
            continue
        if not _has_carbon(sanitized) or _component_role(sanitized) != "fragment":
            continue
        if int(sanitized.GetNumHeavyAtoms() or 0) < min_heavy_atoms:
            continue
        smiles = _canonical_smiles(sanitized)
        if not smiles or smiles in seen:
            continue
        seen.add(smiles)
        extras.append((index, sanitized))
    return extras


def ligand_fragments(mol) -> tuple[dict[str, Any], dict[int, Any]] | None:
    """Split a ligand into its components and pick the one to keep — in memory, writing nothing.

    Returns ``(fragmentation_metadata, {fragment_index: Mol})``. The metadata lists each component
    as ``{fragment_index, role, smiles}``; the components themselves are not stored: they come back
    from the imported original (``fragments_of``) when someone asks for one.
    """
    from rdkit import Chem

    fragments = Chem.GetMolFrags(Chem.Mol(mol), asMols=True, sanitizeFrags=False)
    if not fragments:
        return None
    components: list[dict[str, Any]] = []
    fragment_mols: dict[int, Any] = {}
    problem_flags: list[str] = []
    for index, fragment in enumerate(fragments, start=1):
        try:
            sanitized = _sanitize_fragment(fragment)
        except Exception:
            problem_flags.append(f"unsanitizable_fragment_{index}")
            continue
        fragment_mols[index] = sanitized
        components.append({"fragment_index": index, "role": _component_role(sanitized),
                           "smiles": _canonical_smiles(sanitized)})
    if not components:
        return None

    candidates = [c for c in components if c["role"] == "fragment"]
    organic = [c for c in candidates if _has_carbon(fragment_mols[c["fragment_index"]])]
    ranked = organic or candidates
    if not ranked:
        return (
            {"selected_fragment_index": None, "selection_strategy": "none",
             "problem_flags": sorted(set(problem_flags + ["no_valid_ligand_fragment"])), "components": components},
            fragment_mols,
        )
    selected = max(ranked, key=lambda c: (int(fragment_mols[c["fragment_index"]].GetNumHeavyAtoms() or 0),
                                          int(fragment_mols[c["fragment_index"]].GetNumAtoms() or 0), c["smiles"]))
    selected["role"] = "selected_fragment"

    roles = {c["role"] for c in components}
    problem_flags += [f"{role}s" for role in ("detached_proton", "associated_ion", "associated_component") if role in roles]
    if len({c["smiles"] for c in components if c is not selected and c["smiles"]}) > 1:
        problem_flags.append("multiple_distinct_nonselected_components")
    return (
        {"selected_fragment_index": selected["fragment_index"],
         "selection_strategy": "largest_organic_fragment" if organic else "largest_non_ion_fragment",
         "problem_flags": sorted(set(problem_flags)), "components": components},
        fragment_mols,
    )


def fragments_of(mol) -> dict[int, Any]:
    """``{fragment_index: Mol}`` of a stored original, numbered as its ``fragmentation`` metadata."""
    analysis = ligand_fragments(mol)
    return analysis[1] if analysis is not None else {}


def fragment_entries_from_metadata(raw_metadata: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    metadata = dict(raw_metadata or {})
    fragment_block = metadata.get("fragmentation")
    if not isinstance(fragment_block, Mapping):
        return []
    return [dict(item) for item in list(fragment_block.get("components") or []) if isinstance(item, Mapping)]


__all__ = [
    "fragment_entries_from_metadata",
    "fragments_of",
    "largest_ligand_fragment",
    "ligand_fragments",
]
