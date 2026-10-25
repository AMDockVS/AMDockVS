from __future__ import annotations

import io
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
from amdockvs.docking.engines.vina import _follow_progress_line
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


def test_a_chunk_tells_the_share_of_its_pairs_that_are_docked(monkeypatch):
    monkeypatch.setattr(docking_registry, "DOCK_RUNNERS", dict(docking_registry.DOCK_RUNNERS))

    def runner(payload: dict) -> list[dict]:
        for pair in payload["pairs"]:
            payload["_pair_progress"](0.5)  # halfway through this pair
            payload["_pair_callback"]([pair])
        return list(payload["pairs"])

    docking_registry.register_dock_runner("test_pairs", runner, replace=True)
    seen, told = [], []
    chunk = {"engine": "test_pairs", "pairs": [{"n": n} for n in range(4)], "_pair_callback": seen.append}

    assert run_docking_chunk(chunk, told.append) == chunk["pairs"]
    assert told == [12.5, 25.0, 37.5, 50.0, 62.5, 75.0, 87.5, 100.0]
    assert seen == [[pair] for pair in chunk["pairs"]]  # whoever was listening still hears


def test_the_progress_line_of_vina_tells_how_far_a_pair_is():
    output = (
        "#### AutoDock Vina ####\n* a note with a star\nPerforming docking (random seed: 1) ... \n"
        "0%   10   20   30   40   50   60   70   80   90   100%\n"
        "|----|----|----|----|----|----|----|----|----|----|\n" + "*" * 51 + "\n\nmode |   affinity\n"
    )
    told = []

    assert _follow_progress_line(io.StringIO(output), told.append) == output
    assert len(told) == 52 and told[0] == told[1] == 1 / 51  # the note's star, then the line starts over
    assert told[1:] == sorted(told[1:]) and told[-1] == 1.0
    assert _follow_progress_line(io.StringIO(output)) == output  # nobody listening: read in one go


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
        workspace=SimpleNamespace(project_db=object()),
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
            ligands=[{"id": 1, "ligand": b"ligand"}],
            receptors=[{"id": 2, "input_path": "receptor.pdbqt"}],
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
        docking=SimpleNamespace(run=lambda **kwargs: calls.append(kwargs) or "job-1",
                                check_protocol=lambda **_kwargs: None),
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


def test_a_docking_chunk_goes_by_its_engine_and_its_first_pair():
    from amdockvs.docking.jobs import pairs_label, redocking_job

    assert pairs_label({"engine": "vina", "pairs": [{"receptor_id": 1, "ligand_id": 2}]}) == "vina-R1-L2"
    assert pairs_label({"engine": "qvina", "pairs": [{"receptor_id": 1, "ligand_id": n} for n in (2, 3, 4)]}) \
        == "qvina-R1-L2 +2"
    # and the engine is told: it is what the monitor shows
    assert redocking_job.engine.__job__["task"].__task__["label"] is pairs_label
