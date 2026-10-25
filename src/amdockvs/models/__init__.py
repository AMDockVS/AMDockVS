from amdockvs.models.codes import CodeRecord, ensure_codes
from amdockvs.models.clustering import SimilarityResult, ClusteringResult, ClusteringRun
from amdockvs.models.complexes import ComplexRecord
from amdockvs.models.descriptors import (
    DescriptorBlockRecord,
    DescriptorSchema,
    DescriptorVectorRecord,
    FingerprintRecord,
)
from amdockvs.models.molecules import (
    ImportBatch,
    MoleculeModel,
    MoleculeRecord,
    MoleculeRepresentation,
    MoleculeUsageClass,
)
from amdockvs.models.docking import (
    BindingSite,
    DockingResult,
    DockingRun,
    InteractionsResult,
    LigandPrep,
    ReceptorPrep,
    prep_table,
)
from amdockvs.models.screening import (
    ScreeningDispatch,
    ScreeningShard,
    ScreeningShardRun,
    ScreeningTarget,
    ShardPrep,
    ShardGeneration,
)
from amdockvs.models.qsar import QSARModel, QSARPrediction, LigandActivity
from amdockvs.models.sets import MoleculeSet, MoleculeSetMember, SetRecord, SetItemRecord

# Temporary compatibility aliases while callers migrate to the new schema.
LigandRecord = MoleculeRecord
ReceptorRecord = MoleculeRecord
DockingResultRecord = DockingResult
MoleculeDescriptorRecord = DescriptorVectorRecord
ActivityRecord = LigandActivity
QSARModelRecord = QSARModel
QSARPredictionRecord = QSARPrediction

__all__ = [
    "CodeRecord",
    "ensure_codes",
    "ImportBatch",
    "MoleculeRecord",
    "MoleculeRepresentation",
    "MoleculeModel",
    "MoleculeUsageClass",
    "LigandRecord",
    "ReceptorRecord",
    "BindingSite",
    "LigandPrep",
    "ReceptorPrep",
    "prep_table",
    "DescriptorSchema",
    "DescriptorVectorRecord",
    "DescriptorBlockRecord",
    "MoleculeDescriptorRecord",
    "FingerprintRecord",
    "SimilarityResult",
    "ClusteringResult",
    "ClusteringRun",
    "ScreeningShard",
    "ShardPrep",
    "ShardGeneration",
    "ScreeningShardRun",
    "ScreeningTarget",
    "ScreeningDispatch",
    "ComplexRecord",
    "MoleculeSet",
    "MoleculeSetMember",
    "SetRecord",
    "SetItemRecord",
    "ActivityRecord",
    "LigandActivity",
    "DockingResultRecord",
    "DockingResult",
    "DockingRun",
    "QSARModelRecord",
    "QSARModel",
    "QSARPredictionRecord",
    "QSARPrediction",
    "InteractionsResult",
]
