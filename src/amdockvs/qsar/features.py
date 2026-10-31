"""Reading the stored features of molecules: the RDKit 2D block and the Morgan fingerprints.

Only reads: the descriptor job computes and stores them. Shared by the API (diagnostics, the
correlation heatmap) and the train job's feed, which runs without the runtime.
"""
from __future__ import annotations

import numpy as np
from sqlmodel import select

from amdockvs.models import DescriptorBlockRecord, FingerprintRecord
from amdockvs.models.descriptors import FingerprintType


def fingerprint_matrix(session, ids, *, radius: int, nbits: int) -> dict[int, np.ndarray]:
    """molecule_id -> 0/1 vector unpacked from the stored Morgan bitstring bytes (no RDKit)."""
    fp_type = FingerprintType.ECFP6 if int(radius) >= 3 else FingerprintType.ECFP4
    statement = (
        select(FingerprintRecord)
        .where(FingerprintRecord.fp_type == fp_type)
        .where(FingerprintRecord.nbits == int(nbits))
        .where(FingerprintRecord.radius == int(radius))
    )
    if ids is not None:
        statement = statement.where(FingerprintRecord.molecule_id.in_([int(i) for i in ids]))
    return {int(rec.molecule_id): np.frombuffer(rec.fp_binary, dtype=np.uint8) - ord("0")
            for rec in session.exec(statement)}


def rdkit2d_blocks(session, ids) -> dict[int, dict[str, float]]:
    """{molecule_id: {descriptor: value}} for the molecules whose RDKit 2D block is stored."""
    return {
        int(r.molecule_id): dict(r.values_json or {})
        for r in session.exec(
            select(DescriptorBlockRecord)
            .where(DescriptorBlockRecord.block == "rdkit2d")
            .where(DescriptorBlockRecord.molecule_id.in_([int(i) for i in ids] or [0]))
        )
        if r.values_json
    }


def design_matrix(session, ids, *, kind: str, names, fp_radius: int, fp_nbits: int):
    """(the ids that have the features, their rows, the feature names), in the order of ``ids``.
    rdkit2d gaps become NaN for the pipeline's imputer."""
    if kind == "rdkit2d":
        block = rdkit2d_blocks(session, ids)
        names = tuple(names)
        vectors = {mid: [float(values.get(n, np.nan)) for n in names] for mid, values in block.items()}
    else:
        names = tuple(f"bit_{i}" for i in range(int(fp_nbits)))
        vectors = fingerprint_matrix(session, ids, radius=fp_radius, nbits=fp_nbits)
    kept = [int(i) for i in ids if int(i) in vectors]
    return kept, [vectors[i] for i in kept], names
