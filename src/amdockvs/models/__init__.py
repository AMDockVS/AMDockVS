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
    MoleculeModel,
    MoleculeRecord,
    MoleculeRepresentation,
    MoleculeSourceProperty,
    MoleculeUsageClass,
)
from amdockvs.models.docking import (
    BindingSite,
    ConsensusScore,
    DockingResult,
    DockingRun,
    EngineState,
    InteractionsResult,
)
from amdockvs.models.screening import (
    ScreeningDispatch,
    ScreeningShard,
    ScreeningShardRun,
    ScreeningTarget,
    ShardEngineState,
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
    "MoleculeRecord",
    "MoleculeRepresentation",
    "MoleculeModel",
    "MoleculeSourceProperty",
    "MoleculeUsageClass",
    "LigandRecord",
    "ReceptorRecord",
    "BindingSite",
    "EngineState",
    "DescriptorSchema",
    "DescriptorVectorRecord",
    "DescriptorBlockRecord",
    "MoleculeDescriptorRecord",
    "FingerprintRecord",
    "SimilarityResult",
    "ClusteringResult",
    "ClusteringRun",
    "ScreeningShard",
    "ShardEngineState",
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
    "ConsensusScore",
    "QSARModelRecord",
    "QSARModel",
    "QSARPredictionRecord",
    "QSARPrediction",
    "InteractionsResult",
]
