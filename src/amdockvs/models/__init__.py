from amdockvs.models.analisis import SimilarityResult, ClusteringResult, ClusteringRun
from amdockvs.models.base import TimestampedRecord
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
    EngineState,
    InteractionsResult,
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
    "ComplexRecord",
    "MoleculeSet",
    "MoleculeSetMember",
    "SetRecord",
    "SetItemRecord",
    "ActivityRecord",
    "LigandActivity",
    "DockingResultRecord",
    "DockingResult",
    "ConsensusScore",
    "QSARModelRecord",
    "QSARModel",
    "QSARPredictionRecord",
    "QSARPrediction",
    "InteractionsResult",
    "TimestampedRecord",
]
