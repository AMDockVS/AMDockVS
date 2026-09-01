from __future__ import annotations

import csv
import gzip
import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import select

from amdockvs.models import BindingSite, MoleculeRecord
from amdockvs.pockets.api import PocketPredictionAPI, defined_reference_ligands
from amdockvs.pockets.jobs import (
    P2RankPredictionParams,
    p2rank_prediction_finalize,
)
from amdockvs.pockets.p2rank import (
    P2RANK_VERSION,
    _safe_archive_members,
    ensure_p2rank,
    parse_p2rank_outputs,
    p2rank_status,
)
from ms_flow.core.database import ProjectStore


def test_defined_reference_ligands_uses_import_metadata():
    assert defined_reference_ligands(
        {
            "workflow": {
                "reference_ligands": ["A:HEM:401", "", "B:NAD:502"],
                "ligand_candidates": ["ignored"],
            }
        }
    ) == ["A:HEM:401", "B:NAD:502"]
    assert defined_reference_ligands({"workflow": {"ligand_candidates": ["A:LIG:1"]}}) == []


def test_prediction_params_validate_per_receptor_profiles():
    params = P2RankPredictionParams(
        receptor_ids=[4, 7],
        profile="default",
        profiles={"4": "alphafold", 7: "default"},
        run_id="run",
        p2rank_command="/tmp/prank",
        java_command="/tmp/java",
    )
    assert params.profiles == {4: "alphafold", 7: "default"}
    with pytest.raises(ValueError, match="profiles values"):
        P2RankPredictionParams(
            receptor_ids=[4],
            profiles={4: "unsupported"},
            run_id="run",
            p2rank_command="/tmp/prank",
            java_command="/tmp/java",
        )


def _write_outputs(root: Path) -> None:
    root.mkdir(parents=True)
    with (root / "protein.pdb_predictions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "name",
                "rank",
                "score",
                "probability",
                "sas_points",
                "surf_atoms",
                "center_x",
                "center_y",
                "center_z",
                "residue_ids",
                "surf_atom_ids",
            ]
        )
        writer.writerow(
            ["pocket1", 1, 9.5, 0.7, 12, 8, 1.0, 2.0, 3.0, "A_10 A_11", "1 2 3"]
        )
    visualizations = root / "visualizations"
    data = visualizations / "data"
    data.mkdir(parents=True)
    with gzip.open(data / "protein.pdb_points.pdb.gz", "wt", encoding="utf-8") as handle:
        handle.write(
            "HETATM    1 H    STP 1   1       0.000   1.000   2.000  0.50 0.100\n"
            "HETATM    2 H    STP 1   1      12.000  13.000  14.000  0.50 0.600\n"
        )
    (visualizations / "protein.pdb_pymol.pml").write_text("show spheres\n", encoding="utf-8")


def test_parse_p2rank_outputs_builds_binding_site_rows(tmp_path):
    output = tmp_path / "output"
    _write_outputs(output)

    rows = parse_p2rank_outputs(
        output_dir=output,
        receptor_id=7,
        run_id="run-1",
        profile="default",
    )

    assert len(rows) == 1
    row = rows[0]
    assert (row["molecule_id"], row["source"], row["source_ref"]) == (7, "p2rank", "1")
    assert (row["center_x"], row["center_y"], row["center_z"]) == (1.0, 2.0, 3.0)
    assert (row["size_x"], row["size_y"], row["size_z"]) == (20.0, 20.0, 20.0)
    assert row["extra_data"]["score"] == 9.5
    assert row["extra_data"]["probability"] == 0.7
    assert row["extra_data"]["residue_ids"] == ["A_10", "A_11"]
    assert Path(row["extra_data"]["points_path"]).is_file()


def _pocket_api(project_store: ProjectStore) -> PocketPredictionAPI:
    runtime = SimpleNamespace(
        molsuite=SimpleNamespace(project_db=project_store),
        _require_active_project=lambda: None,
    )
    return PocketPredictionAPI(runtime)


def test_prediction_plan_reuses_only_one_coherent_current_run(tmp_path):
    project_store = ProjectStore.open_at(tmp_path / "project.db")
    output = tmp_path / "results" / "pockets" / "old" / "receptor_7"
    _write_outputs(output)
    parsed = parse_p2rank_outputs(
        output_dir=output,
        receptor_id=7,
        run_id="old",
        profile="default",
    )
    with project_store.get_session() as session:
        session.add(MoleculeRecord(id=7, name="rec", is_receptor=True))
        session.commit()
        session.add(BindingSite(**parsed[0]))
        session.commit()

    api = _pocket_api(project_store)
    reused = api.plan_prediction(receptor_ids=[7], profiles={7: "default"})
    assert reused.reused_receptor_ids == (7,)
    assert reused.recalculate_receptor_ids == ()

    changed = api.plan_prediction(receptor_ids=[7], profiles={7: "alphafold"})
    assert changed.recalculate_receptor_ids == (7,)

    with project_store.get_session() as session:
        session.add(
            BindingSite(
                molecule_id=7,
                name="duplicate",
                source="p2rank",
                source_ref="1",
                extra_data=dict(parsed[0]["extra_data"]),
            )
        )
        session.commit()
    duplicated = api.plan_prediction(receptor_ids=[7], profiles={7: "default"})
    assert duplicated.recalculate_receptor_ids == (7,)
    ProjectStore.clear_cached_stores()


def test_parsed_rows_go_through_the_real_sink_unchanged(tmp_path):
    """What the parser returns is written AS-IS: there is no SQLModel on the path.

    That is why `created_at` (NOT NULL, with `default_factory`) has to be set by `build_row` and
    not by the model — the default_factory only runs when instantiating `BindingSite(...)`, which
    the sink never does. This test writes through the same `output_spec` as the job, which is
    where it blew up.
    """
    from amdockvs.pockets.jobs import p2rank_prediction_job

    output = tmp_path / "results" / "pockets" / "run" / "receptor_7"
    _write_outputs(output)
    rows = parse_p2rank_outputs(
        output_dir=output, receptor_id=7, run_id="run", profile="default"
    )
    project_store = ProjectStore.open_at(tmp_path / "project.db")
    with project_store.get_session() as session:
        session.add(MoleculeRecord(id=7, name="rec", is_receptor=True))
        session.commit()

    project_store.persist_output_spec(p2rank_prediction_job.output_spec, rows)

    with project_store.get_session() as session:
        stored = session.exec(select(BindingSite)).all()
    assert len(stored) == len(rows)
    assert all(site.created_at is not None for site in stored)
    assert all(dict(site.extra_data or {}).get("run_id") == "run" for site in stored)
    ProjectStore.clear_cached_stores()


def test_a_second_run_is_added_next_to_the_first_and_leaves_the_active_site_alone(tmp_path):
    """Prediction rewrites nothing: it appends. The old stays until the user deletes it.

    This used to be a reconciliation (match by source_ref, reserve free indices, move the active
    site if its own disappeared). Now the rows are written by the sink and the only invariant
    left is this one: the new run does not touch a single row of the old one.
    """
    project_store = ProjectStore.open_at(tmp_path / "project.db")
    output_root = tmp_path / "results" / "pockets"
    for run in ("old-run", "new-run"):
        _write_outputs(output_root / run / "receptor_7")
    old_rows = parse_p2rank_outputs(
        output_dir=output_root / "old-run" / "receptor_7",
        receptor_id=7,
        run_id="old-run",
        profile="default",
    )
    with project_store.get_session() as session:
        session.add(MoleculeRecord(id=7, name="rec", is_receptor=True))
        session.commit()
        session.add(BindingSite(molecule_id=7, name="Manual", source="manual"))
        session.add(BindingSite(**old_rows[0]))
        session.commit()
        old_prediction = session.exec(
            select(BindingSite).where(BindingSite.source == "p2rank")
        ).one()
        receptor = session.get(MoleculeRecord, 7)
        receptor.active_binding_site_id = old_prediction.id
        session.add(receptor)
        session.commit()
        old_prediction_id = int(old_prediction.id)

    new_rows = parse_p2rank_outputs(
        output_dir=output_root / "new-run" / "receptor_7",
        receptor_id=7,
        run_id="new-run",
        profile="alphafold",
    )
    with project_store.get_session() as session:
        session.add(BindingSite(**new_rows[0]))
        session.commit()

    params = P2RankPredictionParams(
        receptor_ids=[7],
        profiles={7: "alphafold"},
        recalculate_receptor_ids=[7],
        run_id="new-run",
        output_dir=str(output_root),
        p2rank_command="/tmp/prank",
        java_command="/tmp/java",
    )
    result = p2rank_prediction_finalize(
        {"job_id": "job"},
        {
            "job_params": params.model_dump(mode="python"),
            "terminal_status": "completed",
            "project_db_path": str(project_store.db_path),
        },
    )
    assert result == {"predicted_receptors": 1, "status": "added"}

    with project_store.get_session() as session:
        sites = session.exec(
            select(BindingSite).where(BindingSite.molecule_id == 7).order_by(BindingSite.id)
        ).all()
        receptor = session.get(MoleculeRecord, 7)
    assert [site.source for site in sites] == ["manual", "p2rank", "p2rank"]
    assert [dict(site.extra_data or {}).get("run_id") for site in sites[1:]] == ["old-run", "new-run"]
    # The active one is still the one the user chose, even if its run is no longer the latest.
    assert receptor.active_binding_site_id == old_prediction_id
    # And the files of the old run are not thrown away either.
    assert (output_root / "old-run" / "receptor_7").exists()
    ProjectStore.clear_cached_stores()


def test_plan_prediction_judges_only_the_latest_run(tmp_path):
    """With two runs stacked, consistency is measured on the latest one, not on the mixture."""
    project_store = ProjectStore.open_at(tmp_path / "project.db")
    output_root = tmp_path / "results" / "pockets"
    with project_store.get_session() as session:
        session.add(MoleculeRecord(id=7, name="rec", is_receptor=True))
        session.commit()
        for run in ("old-run", "new-run"):
            output = output_root / run / "receptor_7"
            _write_outputs(output)
            rows = parse_p2rank_outputs(
                output_dir=output, receptor_id=7, run_id=run, profile="default"
            )
            session.add(BindingSite(**rows[0]))
        session.commit()

    api = _pocket_api(project_store)
    assert api.plan_prediction(receptor_ids=[7], profiles={7: "default"}).reused_receptor_ids == (7,)
    ProjectStore.clear_cached_stores()


def test_p2rank_finalize_failed_job_preserves_previous_prediction(tmp_path):
    project_store = ProjectStore.open_at(tmp_path / "project.db")
    output_root = tmp_path / "results" / "pockets"
    failed_output = output_root / "failed-run" / "receptor_7"
    _write_outputs(failed_output)
    with project_store.get_session() as session:
        session.add(MoleculeRecord(id=7, name="rec", is_receptor=True))
        session.commit()
        session.add(
            BindingSite(
                molecule_id=7,
                name="Old",
                source="p2rank",
                source_ref="1",
                extra_data={"profile": "default", "run_id": "old-run"},
            )
        )
        session.commit()
    params = P2RankPredictionParams(
        receptor_ids=[7],
        recalculate_receptor_ids=[7],
        run_id="failed-run",
        output_dir=str(output_root),
        p2rank_command="/tmp/prank",
        java_command="/tmp/java",
    )
    result = p2rank_prediction_finalize(
        {"job_id": "job"},
        {
            "job_params": params.model_dump(mode="python"),
            "terminal_status": "failed",
            "project_db_path": str(project_store.db_path),
        },
    )
    assert result["status"] == "skipped_failed_job"
    with project_store.get_session() as session:
        sites = session.exec(select(BindingSite)).all()
    assert len(sites) == 1
    assert sites[0].extra_data["run_id"] == "old-run"
    assert not (output_root / "failed-run").exists()
    ProjectStore.clear_cached_stores()


def test_safe_archive_rejects_parent_traversal():
    memory = io.BytesIO()
    with tarfile.open(fileobj=memory, mode="w:gz") as archive:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    memory.seek(0)
    with tarfile.open(fileobj=memory, mode="r:gz") as archive:
        with pytest.raises(ValueError, match="Unsafe path"):
            _safe_archive_members(archive)


def test_ensure_p2rank_installs_verified_local_archive(tmp_path, monkeypatch):
    archive_path = tmp_path / "p2rank_test.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        for name, content, mode in (
            ("p2rank_test/prank", b"#!/bin/sh\n", 0o755),
            ("p2rank_test/bin/p2rank.jar", b"jar", 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    tools_home = tmp_path / "tools"
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tools_home))
    monkeypatch.delenv("AMDOCK_P2RANK_HOME", raising=False)
    monkeypatch.setattr("amdockvs.pockets.p2rank.java_major_version", lambda _command=None: 17)
    monkeypatch.setattr(
        "amdockvs.pockets.p2rank.find_java_command",
        lambda: Path("/fake/java"),
    )

    installed = ensure_p2rank(
        version="test",
        archive_path=archive_path,
        expected_sha256=checksum,
    )

    assert installed.installed is True
    assert installed.command.is_file()
    assert installed.command.stat().st_mode & 0o111
    assert (installed.home / "bin" / "p2rank.jar").read_bytes() == b"jar"


def test_installed_p2rank_distribution_reports_ready():
    status = p2rank_status(P2RANK_VERSION)
    if not status.installed:
        pytest.skip("P2Rank distribution is not installed in this environment.")
    assert status.java_version is not None and status.java_version >= 17
    assert status.command.is_file()
