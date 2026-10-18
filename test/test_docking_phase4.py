from __future__ import annotations

import inspect
from types import SimpleNamespace

from amdockvs.molecules.scopes import MoleculeScope
from amdockvs.docking.planning import (
    DockingProtocol,
    DockingRunIdentity,
    DockingRunRequest,
    docking_signature,
)
from amdockvs.docking.readiness import DockingReadinessService
from amdockvs.docking.submission import DockingSubmissionService


def test_docking_studio_is_a_small_coordinator_with_specialized_components():
    from amdockvs.ui.tools.binding_sites.flexible_residues import FlexibleResiduesPanel
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
    assert result.interaction_job_id == ""  # found inside the docking chunks
    assert runtime.docking.run_calls[0]["compute_interactions"] is True
    assert result.receptor_ids == (10,)
    assert runtime.docking.run_calls[0]["ligand_set"][1] == {
        "prepared": True,
        "prepared_engine_key": "ad4",
    }
    assert runtime.docking.run_calls[0]["receptor_set"] == request.receptor_scope


def test_a_program_that_is_not_installed_stays_listed_and_cannot_be_run(monkeypatch, tmp_path):
    import pytest

    pytest.importorskip("PySide6")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    from PySide6.QtWidgets import QApplication

    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.ui.tools.docking import protocol_editor
    from amdockvs.ui.tools.docking.studio import DockingStudioWidget

    app = QApplication.instance() or QApplication([])  # noqa: F841  (kept alive for the widgets)
    monkeypatch.setattr(protocol_editor, "run_async", lambda *_args, **_kwargs: None)  # no lookup on this machine
    runtime = AMDockVSRuntime()
    runtime.create_project(name="p", folder=tmp_path / "p", description="")
    studio = DockingStudioWidget(runtime=runtime)
    rows = lambda: [studio.program_list.item(i).text() for i in range(studio.program_list.count())]
    ticked = lambda: [key for key, check in studio._program_checks.items() if check.isChecked()]
    assert (ticked(), studio._current_program_key()) == (["qvina"], "qvina")  # what a run starts with

    studio._apply_missing_programs({"qvina": "QuickVina 2 is not installed."})

    assert (ticked(), studio._current_program_key()) == (["vina"], "vina")  # and Vina when it cannot run
    assert "QuickVina 2 — not installed" in rows()
    assert not studio._program_checks["qvina"].isEnabled()
    assert "qvina" not in [protocol["program"] for protocol in studio._selected_protocols()]
    assert not studio._program_notices["qvina"][1].isHidden()

    studio._apply_missing_programs({})  # installed from Settings: the option is back

    assert "QuickVina 2" in rows()
    assert studio._program_checks["qvina"].isEnabled()
    assert studio._program_notices["qvina"][1].isHidden()
    assert "qvina" in ticked() and studio._current_program_key() == "qvina"  # and left active


def test_redocking_readiness_counts_distinct_receptors_and_keeps_picked_pairs_whole():
    """4 pairs over 3 receptors (receptor 20 holds two ligands); ligands 1,2 prepared, receptors
    20,21 ready. Picking ligands narrows the pairs; their receptors come with them."""
    pairs = [SimpleNamespace(id=i, ligand_molecule_id=lig, receptor_molecule_id=rec)
             for i, (lig, rec) in enumerate([(1, 20), (2, 20), (3, 21), (4, 22)], start=1)]
    prepared = {1, 2}

    class _Molecules:
        all = staticmethod(lambda: {})
        filter = staticmethod(lambda scope, filters: {**scope, **filters})
        stream_ids = staticmethod(lambda scope: [i for i in scope["id__in"]
                                                 if not scope.get("prepared") or i in prepared])

    runtime = SimpleNamespace(molecules=_Molecules(),
                              complexes=SimpleNamespace(list=lambda purpose: list(pairs)))
    service = DockingReadinessService(runtime)
    service.receptors = lambda scope, program: SimpleNamespace(
        ready_ids=tuple(i for i in scope["id__in"] if i in (20, 21)), failed=0)
    service.preparation = lambda *_a, **_k: SimpleNamespace(failed=0)

    every = service.evaluate_redocking(program="vina").as_mapping()
    assert (every["ligands"]["ready"], every["ligands"]["total"]) == (2, 4)
    assert (every["receptors"]["ready"], every["receptors"]["total"]) == (2, 3)
    assert every["ready_complex_ids"] == [1, 2]

    picked = service.evaluate_redocking(program="vina", ligand_ids=[2, 4]).as_mapping()
    assert (picked["ligands"]["ready"], picked["ligands"]["total"]) == (1, 2)
    assert (picked["receptors"]["ready"], picked["receptors"]["total"]) == (1, 2)
    assert picked["ready_complex_ids"] == [2]
