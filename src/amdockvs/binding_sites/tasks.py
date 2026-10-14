"""What a worker runs to predict the pockets of a receptor.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

from typing import Any

from amdockvs.binding_sites.p2rank import run_p2rank_prediction
from amdockvs.core.constants import AMDOCKVS_PROCESS_EXECUTORS
from amdockvs.core.jobs import task


@task(
    name="amdock_p2rank_predict_receptor",
    description="Predict receptor pockets with P2Rank and stage their native outputs.",
    executor="compute",
    supported_executors=AMDOCKVS_PROCESS_EXECUTORS,
)
def p2rank_prediction_task(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if bool(payload.get("reuse_only")):
        return []
    return run_p2rank_prediction(payload)
