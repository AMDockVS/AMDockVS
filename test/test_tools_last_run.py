"""Build, Pocket detection and Diversity reopen on what the project's last Run submitted
(project/submitted.py): the options, never which molecules."""
import time

import pytest

from amdockvs.runtime import AMDockVSRuntime  # noqa: F401 - pydantic before PySide6


@pytest.fixture
def project(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.binding_sites import detection
    from amdockvs.ui.tools.molecules import build, diversity

    app = QApplication.instance() or QApplication([])  # noqa: F841  (kept alive for the widgets)
    for module in (build, detection, diversity):
        if hasattr(module, "run_async"):
            monkeypatch.setattr(module, "run_async", lambda *_args, **_kwargs: None)
    runtime = AMDockVSRuntime()
    runtime.create_project(name="p", folder=tmp_path / "p", description="")
    widgets = []
    runtime.keep = widgets.append
    yield runtime
    for widget in widgets:
        widget.deleteLater()
    runtime.close_project()


def _submit(runtime, job, params) -> None:
    """A job submitted through MolSuite, as Run submits it. It fails (the project is empty),
    which does not matter: what was submitted is on record either way."""
    from ms_flow.engine.journal import recorded

    path = runtime.active_context.path / "executor.db"
    before = len(recorded(path)) if path.is_file() else 0
    runtime.submit_job(job, params=params)
    deadline = time.monotonic() + 10
    while not path.is_file() or len(recorded(path)) == before:  # written every few tens of ms
        assert time.monotonic() < deadline, "the submission never reached the journal"
        time.sleep(0.02)


def _build(runtime):
    from amdockvs.ui.tools.molecules.build import MoleculeBuildWidget

    widget = MoleculeBuildWidget(runtime=runtime)
    runtime.keep(widget)
    return widget


def test_tools_with_no_runs_keep_the_values_they_are_built_with(project):
    from amdockvs.ui.tools.binding_sites.detection import PocketDetectionWidget
    from amdockvs.ui.tools.molecules.diversity import DiversitySelectionWidget

    build = _build(project)
    assert build.ligand_protonation_ph.value() == 7.4 and build.ligand_num_conformers.value() == 20
    pockets = PocketDetectionWidget(runtime=project)
    diversity = DiversitySelectionWidget(runtime=project)
    project.keep(pockets), project.keep(diversity)
    assert pockets.threads_spin.value() == 0  # Auto
    assert diversity.radius_spin.value() == 2


def test_build_reopens_on_its_last_ligand_run(project):
    from amdockvs.chemistry.jobs import LigandChemistryJobParams, ligand_chemistry_job

    steps = [["protonate", {"method": "openbabel", "ph": 6.5, "model": "mixed", "threads": 3, "gpu": False}],
             ["generate_3d", {"random_seed": 42, "method": "sretkdgv3", "attempts": 3}],
             ["minimize", {"forcefield": "uff", "max_iters": 300, "prune_rms_thresh": 0.0}]]
    _submit(project, ligand_chemistry_job,
            LigandChemistryJobParams(operation=steps, batch_size=64).model_dump(mode="python"))
    build = _build(project)
    assert build.ligand_protonation_section.isChecked() and build.ligand_3d_section.isChecked()
    assert build.ligand_min_section.isChecked() and not build.ligand_conformers_section.isChecked()
    assert build.ligand_protonation_method.currentData() == "openbabel" and build.ligand_protonation_ph.value() == 6.5
    assert build.pkasso_model.currentData() == "mixed" and build.pkasso_threads.value() == 3
    assert build.ligand_embed_method.currentData() == "sretkdgv3" and build.ligand_embed_attempts.value() == 3
    assert build.ligand_random_seed.value() == 42
    assert build.ligand_forcefield.currentData() == "uff" and build.ligand_max_iters.value() == 300
    assert build.batch_size_ligands.value() == 64


def test_build_reopens_each_protein_step_on_its_last_run(project):
    from amdockvs.chemistry.jobs import (
        ReceptorChemistryJobParams, ReceptorPredictionJobParams, receptor_chemistry_job, receptor_prediction_job)

    for operation, options in (
            ("protonate", {"method": "pdb2pqr", "ph": 6.0, "forcefield": "CHARMM", "tool_command": "/opt/pdb2pqr"}),
            ("fix", {"add_missing_residues": False, "add_missing_atoms": True, "replace_nonstandard": False,
                     "remove_heterogens": True, "keep_water": False, "structure_source": "original"}),
            ("minimize", {"forcefields": ["amber14-all.xml", "amber14/tip3p.xml"], "max_iterations": 800,
                          "tolerance_kj_mol": 5.0})):
        _submit(project, receptor_chemistry_job, ReceptorChemistryJobParams(
            operation=operation, params=options, batch_size=4).model_dump(mode="python"))
    _submit(project, receptor_prediction_job,
            ReceptorPredictionJobParams(model="esmfold2-2026-05", force=True).model_dump(mode="python"))
    build = _build(project)
    assert build.receptor_protonation_method.currentData() == "pdb2pqr" and build.receptor_protonation_ph.value() == 6.0
    assert build.receptor_protonation_forcefield.currentData() == "CHARMM"
    assert not build.fix_missing_residues.isChecked() and build.fix_remove_heterogens.isChecked()
    assert not build.fix_keep_water.isChecked() and build.receptor_fix_structure.currentData() == "original"
    assert list(build.receptor_forcefields.currentData()) == ["amber14-all.xml", "amber14/tip3p.xml"]
    assert build.receptor_max_iterations.value() == 800 and build.receptor_tolerance.value() == 5.0
    assert build.batch_size_receptors.value() == 4
    assert build.receptor_predictor.currentData() == "esmfold2-2026-05" and build.receptor_predict_force.isChecked()


def test_a_recorded_value_the_widget_does_not_take_is_skipped_and_the_rest_restored(project):
    from amdockvs.chemistry.jobs import LigandChemistryJobParams, ligand_chemistry_job

    steps = [["protonate", {"method": "renamed-since", "ph": "unreadable", "threads": 5}]]
    _submit(project, ligand_chemistry_job,
            LigandChemistryJobParams(operation=steps).model_dump(mode="python"))
    build = _build(project)  # opens
    assert build.ligand_protonation_method.currentData() == "dimorphite"  # as built
    assert build.ligand_protonation_ph.value() == 7.4 and build.pkasso_threads.value() == 5


@pytest.mark.parametrize(("batch_size", "shown_threads"), [(None, 3), (8, 0)])
def test_pocket_detection_reopens_jobs_without_a_thread_mode(project, batch_size, shown_threads):
    from amdockvs.binding_sites.jobs import P2RankPredictionParams, p2rank_prediction_job
    from amdockvs.ui.tools.binding_sites.detection import PocketDetectionWidget

    _submit(project, p2rank_prediction_job, P2RankPredictionParams(
        receptor_ids=[1], profile="alphafold", threads=3, batch_size=batch_size, run_id="r", p2rank_command="prank",
        java_command="java").model_dump(mode="python"))
    pockets = PocketDetectionWidget(runtime=project)
    project.keep(pockets)
    assert pockets.profile_combo.currentData() == "alphafold" and pockets.threads_spin.value() == shown_threads


@pytest.mark.parametrize(("requested_threads", "effective_threads", "shown_threads"), [("auto", 1, 0), (3, 3, 3)])
def test_pocket_detection_restores_the_requested_thread_mode(
    project, monkeypatch, requested_threads, effective_threads, shown_threads
):
    from types import SimpleNamespace

    from amdockvs.binding_sites import api
    from amdockvs.project.submitted import last_params
    from amdockvs.ui.tools.binding_sites.detection import PocketDetectionWidget

    plan = SimpleNamespace(missing_structure_ids=(), recalculate_receptor_ids=(1,))
    monkeypatch.setattr(project.binding_sites, "plan_prediction", lambda **kwargs: plan)
    monkeypatch.setattr(api, "p2rank_status", lambda: SimpleNamespace(
        installed=True, java_version=17, command="prank", java_command="java",
    ))
    project.binding_sites.predict(receptor_ids=[1], threads=requested_threads)
    deadline = time.monotonic() + 10
    while not (params := last_params(project.active_context.path, ("amdock_pocket_prediction_job",))):
        assert time.monotonic() < deadline, "the submission never reached the journal"
        time.sleep(0.02)
    assert params["threads"] == effective_threads

    pockets = PocketDetectionWidget(runtime=project)
    project.keep(pockets)
    assert pockets.threads_spin.value() == shown_threads


def test_diversity_reopens_on_its_last_cluster_not_on_a_preview(project):
    from amdockvs.diversity.jobs import SelectionClusterJobParams, cluster_molecules_job
    from amdockvs.ui.tools.molecules.diversity import DiversitySelectionWidget

    _submit(project, cluster_molecules_job, SelectionClusterJobParams(
        method="bitbirch", threshold=0.5, fp_radius=3, fp_nbits=1024, cluster_run_id="c").model_dump(mode="python"))
    diversity = DiversitySelectionWidget(runtime=project)
    project.keep(diversity)
    threshold = diversity._method_widgets["bitbirch"]["threshold"]
    assert threshold.value() == 0.5 and diversity._cluster_params()["threshold"] == 0.5
    assert diversity.radius_spin.value() == 3 and diversity.nbits_spin.value() == 1024


def test_a_run_over_table_and_shards_keeps_what_only_the_table_takes(project):
    from amdockvs.chemistry.jobs import (
        LigandChemistryJobParams, ShardChemistryJobParams, ligand_chemistry_job, shard_chemistry_job)

    protonate = ["protonate", {"method": "openbabel", "ph": 6.8}]
    table = [protonate, ["generate_3d", {"random_seed": 11, "method": "etkdgv3", "attempts": 1}],
             ["conformers", {"num_conformers": 3, "random_seed": 11, "prune_rms_thresh": 0.7}]]
    shards = [protonate, ["generate_3d", {"random_seed": 11, "method": "etkdgv3", "attempts": 1}]]
    # One Run: the table's job first, the shards' last (MoleculeBuildWidget._cfg_build_ligands).
    _submit(project, ligand_chemistry_job,
            LigandChemistryJobParams(operation=table, batch_size=7).model_dump(mode="python"))
    _submit(project, shard_chemistry_job, ShardChemistryJobParams(operation=shards).model_dump(mode="python"))
    build = _build(project)
    assert build.ligand_conformers_section.isChecked() and build.ligand_num_conformers.value() == 3
    assert build.ligand_prune_rms.value() == 0.7 and build.batch_size_ligands.value() == 7
    assert build.ligand_protonation_ph.value() == 6.8 and build.ligand_random_seed.value() == 11


def test_an_open_preview_pile_wins_whole_and_the_next_preview_extends_it(project):
    import json

    from amdockvs.diversity.jobs import SelectionClusterJobParams, cluster_molecules_job
    from amdockvs.ui.tools.molecules.diversity import DiversitySelectionWidget

    _submit(project, cluster_molecules_job, SelectionClusterJobParams(
        method="bitbirch", threshold=0.5, fp_radius=3, fp_nbits=1024, cluster_run_id="c").model_dump(mode="python"))
    # A Preview made after that Cluster, at another threshold and fingerprint, left in the cache.
    key = [["all", None], 2, 3072, None, "", [["method", "bitbirch"], ["threshold", 0.65]]]
    cache = project.active_context.path / "cache" / "diversity_preview.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"selections": [{"molecule_ids": [1]}], "basis_key": key, "seen_ids": [1]}))
    diversity = DiversitySelectionWidget(runtime=project)
    project.keep(diversity)
    assert diversity._cluster_params() == {"method": "bitbirch", "threshold": 0.65}  # the pile's, not the Cluster's
    assert (diversity.radius_spin.value(), diversity.nbits_spin.value()) == (2, 3072)
    scope = diversity._scope_params()
    assert scope is not None and diversity._scope_key(scope) == diversity._basis_key  # the next Preview extends it


def test_a_recorded_number_the_widget_does_not_offer_changes_nothing(project):
    from amdockvs.chemistry.jobs import LigandChemistryJobParams, ligand_chemistry_job

    steps = [["protonate", {"method": "openbabel", "ph": True}],
             ["generate_3d", {"random_seed": 5, "method": "etkdgv3", "attempts": 999}]]
    _submit(project, ligand_chemistry_job, LigandChemistryJobParams(operation=steps).model_dump(mode="python"))
    build = _build(project)
    assert build.ligand_protonation_ph.value() == 7.4  # not 1.0 (True is 1 in Python)
    assert build.ligand_embed_attempts.value() == 1  # not 50 (Qt clamps 999 to the maximum)
    assert build.ligand_protonation_method.currentData() == "openbabel" and build.ligand_random_seed.value() == 5


def test_a_cluster_clicked_after_the_preview_pile_wins_the_settings(project):
    import json
    import os

    from amdockvs.diversity.jobs import SelectionClusterJobParams, cluster_molecules_job
    from amdockvs.ui.tools.molecules.diversity import DiversitySelectionWidget

    key = [["all", None], 2, 3072, None, "", [["method", "bitbirch"], ["threshold", 0.65]]]
    cache = project.active_context.path / "cache" / "diversity_preview.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"selections": [{"molecule_ids": [1]}], "basis_key": key, "seen_ids": [1]}))
    os.utime(cache, (time.time() - 60, time.time() - 60))  # the Preview, a minute before the Cluster
    _submit(project, cluster_molecules_job, SelectionClusterJobParams(
        method="bitbirch", threshold=0.5, fp_radius=3, fp_nbits=1024, cluster_run_id="c").model_dump(mode="python"))
    diversity = DiversitySelectionWidget(runtime=project)
    project.keep(diversity)
    assert diversity._cluster_params() == {"method": "bitbirch", "threshold": 0.5}  # the Cluster's
    assert (diversity.radius_spin.value(), diversity.nbits_spin.value()) == (3, 1024)
    assert len(diversity._selections) == 1  # the pile is still there to look at
    scope = diversity._scope_params()
    assert scope is not None and diversity._scope_key(scope) != diversity._basis_key  # the next Preview starts anew
