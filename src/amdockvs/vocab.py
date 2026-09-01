"""
vocab.py
────────
Domain vocabulary: bounded values that populate text columns in the DB.

They are constant classes (not Enum) on purpose — the values travel as plain
`str`, so adding/renaming a value requires no SQLAlchemy type migration and
does not break when loading rows with legacy values. See the discussion in the
model.

Leaf module: it imports NOTHING from amdockvs (neither ORM nor constants). Any
layer —models, io, workflows, docking, UI, ms_table— can import it cheaply.
`choices_from_class()` from ms_table extracts the tuple of values for the
checkbox table filters.
"""

from __future__ import annotations


class MoleculeType:
    SMALL_MOLECULE = "small_molecule"
    PROTEIN = "protein"
    PEPTIDE = "peptide"
    NUCLEOTIDE = "nucleotide"
    POLYMER = "polymer"
    UNKNOWN = "unknown"
    # Note: workflows.py also handles "macrocycle"/"antibody"/"antigen" as
    # *workflow matching* vocabulary, not as types persisted here
    # (macrocycle collapses to small_molecule before touching the DB).


class FileFormat:
    # Values exactly as the importer writes them (io/parsers/readers.py):
    # .sdf → "sdf"; .smi/.smiles/.csv/.tsv/.txt → "smiles"; the rest use the
    # raw extension (pdb, mol2, pdbqt, pqr, cif, fasta, ...).
    PDB = "pdb"
    SDF = "sdf"
    MOL2 = "mol2"
    CIF = "cif"
    PDBQT = "pdbqt"
    PQR = "pqr"
    SMILES = "smiles"
    FASTA = "fasta"
    UNKNOWN = "unknown"


class BindingSiteSource:
    MANUAL = "manual"
    LIGAND = "ligand"    # centre of mass of a reference ligand
    FPOCKET = "fpocket"  # detected pocket (rank in source_ref)
    P2RANK = "p2rank"    # P2Rank pocket (rank in source_ref)
    PDB = "pdb"          # site annotation from the PDB


class ComplexPurpose:
    REDOCKING = "redocking"
    RESCORING = "rescoring"
    REFERENCE = "reference"


class MoleculeUsageClass:
    GENERAL = "general"
    REFERENCE = "reference"
    DERIVED = "derived"


class ReprType:
    SMILES_CANONICAL = "smiles_canonical"
    SMILES_ISOMERIC = "smiles_isomeric"
    INCHI = "inchi"
    INCHI_KEY = "inchikey"
    SEQUENCE_AA = "sequence_aa"
    SEQUENCE_NT = "sequence_nt"
    HELM = "helm"


class ModelSource:
    IMPORTED = "imported"  # loaded as-is from the source file
    RDKIT = "rdkit"  # generated with RDKit ETKDG
    ESMFOLD = "esmfold"  # predicted with ESMFold
    ETKDG = "etkdg"  # explicit RDKit conformer
    NMR = "nmr"  # NMR ensemble from the PDB


class SetPurpose:
    QSAR = "qsar"
    REDOCKING = "redocking"
    RESCORING = "rescoring"
    SUBSTRUCTURE = "substructure"
    ENRICHMENT = "enrichment"
    GRID_REF = "grid_reference"
    CUSTOM = "custom"


class FingerprintType:
    ECFP4 = "ecfp4"
    ECFP6 = "ecfp6"
    FCFP4 = "fcfp4"
    FCFP6 = "fcfp6"
    MACCS = "maccs"
    RDKIT = "rdkit"
    AVALON = "avalon"
    TORSION = "topological_torsion"
    ATOMPAIR = "atom_pair"


class SimilarityMethod:
    TANIMOTO = "tanimoto"
    DICE = "dice"
    COSINE = "cosine"
    TVERSKY = "tversky"


class ClusteringMethod:
    BITBIRCH_LEAN = "bitbirch_lean"
    BUTINA = "butina"
    KMEANS = "kmeans"
    HIERARCHICAL = "hierarchical"
    DBSCAN = "dbscan"


__all__ = [
    "MoleculeType",
    "FileFormat",
    "BindingSiteSource",
    "ComplexPurpose",
    "MoleculeUsageClass",
    "ReprType",
    "ModelSource",
    "SetPurpose",
    "FingerprintType",
    "SimilarityMethod",
    "ClusteringMethod",
]
