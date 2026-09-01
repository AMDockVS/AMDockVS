from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "ChemistryAPI": ("amdockvs.chemistry.api", "ChemistryAPI"),
    "LigandChemistryJobParams": ("amdockvs.chemistry.jobs", "LigandChemistryJobParams"),
    "ReceptorChemistryJobParams": ("amdockvs.chemistry.jobs", "ReceptorChemistryJobParams"),
    "ligand_chemistry_job": ("amdockvs.chemistry.jobs", "ligand_chemistry_job"),
    "ligand_chemistry_task": ("amdockvs.chemistry.jobs", "ligand_chemistry_task"),
    "receptor_chemistry_job": ("amdockvs.chemistry.jobs", "receptor_chemistry_job"),
    "receptor_chemistry_task": ("amdockvs.chemistry.jobs", "receptor_chemistry_task"),
    "align_molecule_in_place": ("amdockvs.chemistry.alignment", "align_molecule_in_place"),
    "aligned_copy": ("amdockvs.chemistry.alignment", "aligned_copy"),
    "generate_3d_molecule": ("amdockvs.chemistry.conformers", "generate_3d_molecule"),
    "generate_conformer_ensemble": ("amdockvs.chemistry.conformers", "generate_conformer_ensemble"),
    "calculate_basic_descriptors": ("amdockvs.chemistry.descriptors", "calculate_basic_descriptors"),
    "calculate_descriptor_rows": ("amdockvs.chemistry.descriptors", "calculate_descriptor_rows"),
    "SMALL_MOLECULE_FILTER_TYPE": ("amdockvs.chemistry.filtering", "SMALL_MOLECULE_FILTER_TYPE"),
    "SmallMoleculeFilterCriteria": ("amdockvs.chemistry.filtering", "SmallMoleculeFilterCriteria"),
    "SmallMoleculeFilterValues": ("amdockvs.chemistry.filtering", "SmallMoleculeFilterValues"),
    "annotate_row_with_small_molecule_filter_values": ("amdockvs.chemistry.filtering", "annotate_row_with_small_molecule_filter_values"),
    "evaluate_small_molecule_filter_values": ("amdockvs.chemistry.filtering", "evaluate_small_molecule_filter_values"),
    "passes_small_molecule_filter": ("amdockvs.chemistry.filtering", "passes_small_molecule_filter"),
    "small_molecule_filter_values_from_record": ("amdockvs.chemistry.filtering", "small_molecule_filter_values_from_record"),
    "small_molecule_filter_values_from_record_or_file": ("amdockvs.chemistry.filtering", "small_molecule_filter_values_from_record_or_file"),
    "fingerprint_from_molecule": ("amdockvs.chemistry.fingerprints", "fingerprint_from_molecule"),
    "fingerprint_to_bitstring": ("amdockvs.chemistry.fingerprints", "fingerprint_to_bitstring"),
    "morgan_fingerprint": ("amdockvs.chemistry.fingerprints", "morgan_fingerprint"),
    "rdkit_fingerprint": ("amdockvs.chemistry.fingerprints", "rdkit_fingerprint"),
    "tanimoto_similarity": ("amdockvs.chemistry.fingerprints", "tanimoto_similarity"),
    "best_rmsd": ("amdockvs.chemistry.rmsd", "best_rmsd"),
    "conformer_rmsd_matrix": ("amdockvs.chemistry.rmsd", "conformer_rmsd_matrix"),
    "mol_has_3d": ("amdockvs.chemistry.state", "mol_has_3d"),
    "mol_has_explicit_hs": ("amdockvs.chemistry.state", "mol_has_explicit_hs"),
    "molecule_state_metadata": ("amdockvs.chemistry.state", "molecule_state_metadata"),
    "add_explicit_hydrogens": ("amdockvs.chemistry.standardization", "add_explicit_hydrogens"),
    "cleanup_molecule": ("amdockvs.chemistry.standardization", "cleanup_molecule"),
    "remove_explicit_hydrogens": ("amdockvs.chemistry.standardization", "remove_explicit_hydrogens"),
    "standardize_smiles": ("amdockvs.chemistry.standardization", "standardize_smiles"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


__all__ = list(_LAZY_EXPORTS)
