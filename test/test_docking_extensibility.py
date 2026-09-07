from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field, ValidationError
import pytest

import amdockvs.docking.api as docking_api
import amdockvs.docking.preparation.profiles as docking_preparations
import amdockvs.docking.engines.programs as docking_programs
import amdockvs.docking.engines.registry as docking_registry
import amdockvs.docking.submission as docking_submission
from amdockvs.docking.api import DockingAPI
from amdockvs.docking.engines.registry import register_docking_engine, run_docking_chunk
from amdockvs.docking.preparation.profiles import (
    PreparationProfile,
    get_preparation_profile,
    register_preparation_profile,
)
from amdockvs.docking.planning import DockingProtocol, DockingRunRequest
from amdockvs.docking.engines.programs import DockingProgramSpec, get_docking_program
from amdockvs.docking.protocols import protocol_identity
from amdockvs.docking.pairs import iter_docking_batches_from_rows
from amdockvs.docking.submission import DockingSubmissionService


class _ToyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cycles: int = Field(default=3, ge=1)


def _toy_runner(payload: dict) -> list[dict]:
    return [{"cycles": payload["cycles"]}]


def _toy_program() -> DockingProgramSpec:
    return DockingProgramSpec(
        key="test_toy",
        label="Test toy",
        workflow_key="vina",
        preparation_engine="none",
        docking_engine="test_toy",
        requires_ligand_3d=False,
        requires_ligand_preparation=False,
        requires_receptor_preparation=False,
        requires_binding_site=False,
        config_model=_ToyConfig,
        protocol_identity_keys=("cycles",),
        resource_resolver=lambda _config: {"cpu_required": 2},
    )


def test_one_registration_exposes_program_config_resources_and_runner(monkeypatch):
    monkeypatch.setattr(docking_programs, "_PROGRAMS", dict(docking_programs._PROGRAMS))
    monkeypatch.setattr(docking_registry, "DOCK_RUNNERS", dict(docking_registry.DOCK_RUNNERS))
    program = _toy_program()
    register_docking_engine(program, _toy_runner, replace=True)

    assert get_docking_program("test_toy") is program
    assert program.validate_config({"cycles": 7}) == {"cycles": 7}
    assert program.resource_requirements({"cycles": 7}) == {"cpu_required": 2}
    assert run_docking_chunk({"engine": "test_toy", "cycles": 1, "engine_config": {"cycles": 7}}) == [
        {"cycles": 7}
    ]
    with pytest.raises(ValidationError):
        program.validate_config({"unknown": True})


def test_preparation_profile_keeps_all_preparation_entrypoints_together(monkeypatch):
    monkeypatch.setattr(docking_preparations, "_PROFILES", dict(docking_preparations._PROFILES))
    profile = PreparationProfile(
        key="test_prep",
        prepare_entities=lambda **_kwargs: {"updates": [], "failures": []},
        prepare_shard=lambda *_args, **_kwargs: {"n_records": 0},
    )
    register_preparation_profile(profile, replace=True)

    assert get_preparation_profile("test_prep") is profile
    assert profile.prepare_entities() == {"updates": [], "failures": []}
    assert profile.prepare_shard("in", "out") == {"n_records": 0}


def test_program_declares_which_config_fields_change_protocol_identity(monkeypatch):
    monkeypatch.setattr(docking_programs, "_PROGRAMS", dict(docking_programs._PROGRAMS))
    monkeypatch.setattr(docking_registry, "DOCK_RUNNERS", dict(docking_registry.DOCK_RUNNERS))
    register_docking_engine(_toy_program(), _toy_runner, replace=True)

    assert protocol_identity({"cycles": 8, "worker_hint": "local"}, program="test_toy") == {
        "cycles": 8
    }


def test_readiness_honors_programs_without_preparation_or_binding_site(monkeypatch):
    program = _toy_program()
    monkeypatch.setattr(DockingAPI, "get_program_spec", lambda _self, _program: program)
    monkeypatch.setattr(docking_api, "count_entity_rows", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(
        docking_api,
        "entity_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("preparation queried")),
    )
    monkeypatch.setattr(
        docking_api,
        "list_entity_rows",
        lambda *_args, **_kwargs: [
            {"id": 10, "prepared_engine": False, "grid_engine": False},
        ],
    )
    runtime = SimpleNamespace(
        _require_active_project=lambda: None,
        molsuite=SimpleNamespace(project_db=object()),
    )

    result = DockingAPI(runtime).check_required()

    assert result["ready"] is True
    assert result["missing"] == {
        "ligands_prepared": [],
        "receptors_prepared": [],
        "receptor_binding_sites": [],
    }
    assert result["counts"]["ligands_ready"] == 2
    assert result["counts"]["receptors_ready"] == 1


def test_batching_allows_program_without_binding_site(tmp_path):
    chunks = list(
        iter_docking_batches_from_rows(
            ligands=[{"id": 1, "current_path": "ligand.sdf"}],
            receptors=[{"id": 2, "current_path": "receptor.pdb"}],
            output_dir=tmp_path,
            batch_size=1,
            engine="vina",
            preparation_engine="none",
            requires_binding_site=False,
        )
    )

    assert len(chunks) == 1
    assert "invalid_reason" not in chunks[0]["pairs"][0]
    assert chunks[0]["pairs"][0]["box_center"] is None


def test_submission_does_not_force_prepared_scope_when_program_does_not_need_it(monkeypatch):
    program = _toy_program()
    monkeypatch.setattr(docking_submission, "get_docking_program", lambda _key: program)
    calls: list[dict] = []
    ligand_scope = object()
    runtime = SimpleNamespace(
        molecules=SimpleNamespace(
            filter=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("prepared scope forced")
            )
        ),
        docking=SimpleNamespace(run=lambda **kwargs: calls.append(kwargs) or "job-1"),
    )
    request = DockingRunRequest(
        run_kind="docking",
        ligand_scope=ligand_scope,
        receptor_scope=object(),
        protocols=(DockingProtocol(program="test_toy", label="Toy", config={"cycles": 4}),),
    )

    DockingSubmissionService(runtime).submit(request)

    assert calls[0]["ligand_set"] is ligand_scope
    assert calls[0]["engine_config"] == {"cycles": 4}
