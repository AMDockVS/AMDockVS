from __future__ import annotations

import inspect
from types import SimpleNamespace

from amdockvs.api_common import MoleculeScope
from amdockvs.docking.planning import (
    DockingProtocol,
    DockingRunIdentity,
    DockingRunRequest,
    docking_signature,
)
from amdockvs.docking.readiness import DockingReadinessService
from amdockvs.docking.submission import DockingSubmissionService


def test_docking_studio_is_a_small_coordinator_with_specialized_components():
    from amdockvs.ui.tools.docking.flexible_residues import FlexibleResiduesPanel
    from amdockvs.ui.tools.docking.preparation_panel import PreparationPanel
    from amdockvs.ui.tools.docking.protocol_editor import ProtocolEditorWidget
    from amdockvs.ui.tools.docking.run_panel import RunPanel
    from amdockvs.ui.tools.docking.scope_panel import ScopePanel
    from amdockvs.ui.tools.docking.studio import DockingStudioWidget

    assert len(inspect.getsource(DockingStudioWidget).splitlines()) < 600
    for component in (
        ProtocolEditorWidget,
        PreparationPanel,
        ScopePanel,
        FlexibleResiduesPanel,
        RunPanel,
    ):
        assert component in DockingStudioWidget.__mro__


def test_typed_run_request_has_stable_scientific_signature():
    protocol = DockingProtocol.from_mapping({
        "program": "vina",
        "label": "Vina",
        "config": {"exhaustiveness": 8, "scoring_function": "vina"},
    })
    request = DockingRunRequest(
        run_kind="docking",
        ligand_scope=None,
        receptor_scope=None,
        protocols=(protocol,),
    )
    identity = DockingRunIdentity(
        receptor_type="protein",
        ligand_type="small_molecule",
        ligand_mode="selected",
        ligand_ids=(2, 1),
        receptor_mode="selected",
        receptor_ids=(10,),
    )
    assert docking_signature(request, identity) == docking_signature(request, identity)
    assert len(docking_signature(request, identity)) == 40


class _ReadinessDocking:
    def count_docked_pairs(self, **_kwargs):
        return 1


class _ReadinessRuntime:
    def __init__(self):
        self.docking = _ReadinessDocking()
        self.requested_statuses = ()

    def list_jobs(self, *, statuses):
        self.requested_statuses = tuple(statuses)
        return []


def test_readiness_service_counts_protocol_aware_existing_pairs():
    runtime = _ReadinessRuntime()
    service = DockingReadinessService(runtime)
    protocols = (
        DockingProtocol.from_mapping({"program": "vina", "label": "A", "config": {"exhaustiveness": 8}}),
        DockingProtocol.from_mapping({"program": "vina", "label": "B", "config": {"exhaustiveness": 16}}),
    )
    result = service.pairs(
        ligand_count=3,
        receptor_ids=(10, 11),
        protocols=protocols,
        skip_existing=True,
    )
    assert result.as_mapping() == {"total": 12, "already": 2, "to_run": 10}
    assert service.conflict("sig", {}) == ("none", "")
    assert runtime.requested_statuses == ("pending", "running", "staging", "cancel_requested")


def test_readiness_service_detects_active_docking_conflicts_via_runtime_api():
    runtime = _ReadinessRuntime()
    runtime.list_jobs = lambda *, statuses: [
        SimpleNamespace(job_id="dock-1", task_type="Run docking", status="running"),
        SimpleNamespace(job_id="prep-1", task_type="Prepare ligand", status="running"),
    ]
    service = DockingReadinessService(runtime)

    assert service.conflict("same", {"dock-1": "same"})[0] == "error"
    assert service.conflict("different", {"dock-1": "same"})[0] == "warning"


class _SubmissionMolecules:
    def __init__(self):
        self.filters = []

    def filter(self, scope, *, filters):
        self.filters.append((scope, filters))
        return (scope, filters)


class _SubmissionDocking:
    def __init__(self):
        self.run_calls = []
        self.interaction_calls = []

    def run(self, **kwargs):
        self.run_calls.append(kwargs)
        return f"job-{len(self.run_calls)}"

    def compute_interactions(self, **kwargs):
        self.interaction_calls.append(kwargs)
        return "interactions-1"


def test_submission_service_dispatches_only_through_runtime_docking():
    runtime = SimpleNamespace(
        molecules=_SubmissionMolecules(),
        docking=_SubmissionDocking(),
    )
    protocol = DockingProtocol.from_mapping({
        "program": "vina",
        "label": "Vina",
        "config": {"exhaustiveness": 12, "num_modes": 5},
    })
    request = DockingRunRequest(
        run_kind="docking",
        ligand_scope=MoleculeScope(filters={"is_ligand": True}),
        receptor_scope=MoleculeScope(filters={"is_receptor": True}),
        protocols=(protocol,),
        batch_size=50,
        executor_name="thread",
        run_id="run-1",
        compute_interactions=True,
    )
    result = DockingSubmissionService(runtime).submit(request, ready_receptor_ids=(10,))

    assert result.job_ids == {f"1:Vina:{protocol.hash[:8]}": "job-1"}
    assert result.interaction_job_id == "interactions-1"
    assert result.receptor_ids == (10,)
    assert runtime.docking.run_calls[0]["ligand_set"][1] == {
        "prepared": True,
        "prepared_engine_key": "ad4",
    }
    assert runtime.docking.run_calls[0]["receptor_set"] == request.receptor_scope
