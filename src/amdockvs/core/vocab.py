"""
vocab.py
────────
Domain vocabulary: bounded values of the categorical columns in the DB.

They are constant classes (not Enum) on purpose — the values travel as plain
`str`. The DB stores each one as its integer in `CODES` (see the end of the
module), so a value is added by appending it there, never renamed.

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

class ImportKind:
    """Where an import batch put its records: `molecules` rows or `.mshard` shards."""

    ROWS = "rows"
    SHARDS = "shards"

class BindingSiteSource:
    MANUAL = "manual"
    LIGAND = "ligand"    # centre of mass of a reference ligand
    FPOCKET = "fpocket"  # detected pocket (rank in source_ref)
    P2RANK = "p2rank"    # P2Rank pocket (rank in source_ref)
    PDB = "pdb"          # site annotation from the PDB
    METAL = "metal"      # a coordinated metal ion of the structure

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
    FIXED = "fixed"  # receptor repaired with PDBFixer
    PROTONATED = "protonated"  # hydrogens added at a pH
    MINIMIZED = "minimized"  # energy-minimized structure

class SetPurpose:
    QSAR = "qsar"
    REDOCKING = "redocking"
    RESCORING = "rescoring"
    SUBSTRUCTURE = "substructure"
    ENRICHMENT = "enrichment"
    GRID_REF = "grid_reference"
    CUSTOM = "custom"
    SNAPSHOT = "snapshot"  # a saved selection (create_set default)
    FILTER = "filter"  # a set saved from the Filter tool
    MANUAL = "manual"  # molecules.create_set default

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
    BITBIRCH = "bitbirch"
    BITBIRCH_LEAN = "bitbirch_lean"
    BUTINA = "butina"
    KMEANS = "kmeans"
    HIERARCHICAL = "hierarchical"
    DBSCAN = "dbscan"

class ProjectMode:
    """Where a project's ligands live. Fixed when the project is created.

    This is a storage contract, not a preference: it decides whether ligands are rows in
    the project db or records inside shards on disk. Changing it would mean migrating (or
    discarding) the whole library, so there is no setter.
    """

    VS = "vs"          # ligands materialized as rows; the whole catalog UI applies
    HTPVS = "htpvs"    # ligands live in shards; only promoted hits ever become rows

class DispatchState:
    """What happened to a source file handed to a cluster. Lives as long as the campaign."""

    UPLOADED = "uploaded"
    RUNNING = "running"
    RETURNED = "returned"
    FAILED = "failed"

class TargetState:
    """A receptor inside one campaign: scheduled before anything is docked against it."""

    SCHEDULED = "scheduled"
    RUNNING = "running"
    DONE = "done"
    CANCELED = "canceled"
    FAILED = "failed"

class RunKind:
    DOCKING = "docking"
    SCREENING = "screening"
    REDOCKING = "redocking"

class ActivityKind:
    """Picks the QSAR task: regression or classification."""

    CONTINUOUS = "continuous"
    CATEGORICAL = "categorical"


def _domain(base: int, *names: str) -> dict[str, int]:
    return {name: base + offset for offset, name in enumerate(names, 1)}


# Categorical columns store `codes.id`, not text; `models.codes.CodeType` converts both ways, so
# Python only ever sees the names. Append-only: every project db holds these ids, so a value is
# never renumbered, reused or removed; a new one goes at the end of its domain. A value missing
# here can't be written — CodeType raises naming the domain.
CODES: dict[str, dict[str, int]] = {
    "molecule_type": _domain(100, "small_molecule", "protein", "peptide", "nucleotide", "polymer", "unknown"),
    "usage_class": _domain(200, "general", "reference", "derived"),
    "model_source": _domain(
        300, "imported", "rdkit", "esmfold", "etkdg", "nmr", "fixed", "protonated", "minimized"
    ),
    "repr_type": _domain(
        400, "smiles_canonical", "smiles_isomeric", "inchi", "inchikey", "sequence_aa", "sequence_nt", "helm"
    ),
    "binding_site_source": _domain(500, "manual", "ligand", "fpocket", "p2rank", "pdb", "metal"),
    "engine": _domain(600, "vina", "qvina", "gnina", "adgpu", "unidock"),
    "run_kind": _domain(700, "docking", "screening", "redocking"),
    "score_type": _domain(800, "vina_score", "ad4_score", "gnina_score"),
    "activity_kind": _domain(900, "continuous", "categorical"),
    "complex_purpose": _domain(1000, "redocking", "rescoring", "reference"),
    "run_state": _domain(1100, "scheduled", "running", "done", "canceled", "failed"),
    "dispatch_state": _domain(1200, "uploaded", "running", "returned", "failed"),
    "clustering_method": _domain(1300, "bitbirch", "bitbirch_lean", "butina", "kmeans", "hierarchical", "dbscan"),
    "qsar_algorithm": _domain(
        1400, "baseline", "linear_regression", "ridge", "lasso", "random_forest", "gradient_boosting",
        "svr", "knn", "logistic_regression", "svc",
    ),
    "feature_type": _domain(1500, "descriptors_2d", "fingerprints_ecfp4"),
    "set_purpose": _domain(
        1600, "qsar", "redocking", "rescoring", "substructure", "enrichment", "grid_reference", "custom",
        "snapshot", "filter", "manual",
    ),
    "file_format": _domain(1700, "pdb", "sdf", "mol2", "cif", "pdbqt", "pqr", "smiles", "fasta", "unknown"),
    "import_kind": _domain(1800, "rows", "shards"),
    "prep_family": _domain(1900, "ad4"),
}

__all__ = [
    "CODES",
    "RunKind",
    "ActivityKind",
    "MoleculeType",
    "FileFormat",
    "ImportKind",
    "BindingSiteSource",
    "ComplexPurpose",
    "MoleculeUsageClass",
    "ReprType",
    "ModelSource",
    "SetPurpose",
    "FingerprintType",
    "SimilarityMethod",
    "ClusteringMethod",
    "ProjectMode",
    "DispatchState",
    "TargetState",
]
