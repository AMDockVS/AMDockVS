"""The has_3d gate must come from the program spec, not from the docking API."""

from types import SimpleNamespace

import pytest

import amdockvs.docking.api as docking_api
from amdockvs.docking.api import DockingAPI
from amdockvs.docking.engines.programs import VINA_PROGRAM, DockingProgramSpec


@pytest.fixture
def api(monkeypatch):
    """Every ligand in the library is missing 3D coordinates."""
    monkeypatch.setattr(docking_api, "count_entity_rows", lambda *a, **k: 5)
    monkeypatch.setattr(docking_api, "entity_ids", lambda *a, **k: [1, 2, 3])
    runtime = SimpleNamespace(
        _require_active_project=lambda: None,
        molsuite=SimpleNamespace(project_db=object()),
    )
    return DockingAPI(runtime=runtime)


def _with_spec(monkeypatch, api, spec):
    monkeypatch.setattr(DockingAPI, "get_program_spec", lambda _self, program: spec)
    return api.check_ligand_preparation_required()


def test_a_program_that_needs_coordinates_is_not_ready(monkeypatch, api):
    result = _with_spec(monkeypatch, api, VINA_PROGRAM)

    assert result["ready"] is False
    assert result["counts"]["ligands_missing_has_3d"] == 3


def test_a_program_that_docks_from_smiles_is_ready_without_3d(monkeypatch, api):
    # The registered programs all build their own __init__, so state the spec directly.
    smiles_docker = DockingProgramSpec(
        key="diffdock",
        label="DiffDock",
        workflow_key="vina",
        preparation_engine="ad4",
        docking_engine="diffdock",
        requires_ligand_3d=False,
    )
    result = _with_spec(monkeypatch, api, smiles_docker)

    assert result["ready"] is True
    assert result["missing"]["ligands_has_3d"] == []
    assert result["counts"]["ligands_ready_has_3d"] == 5
