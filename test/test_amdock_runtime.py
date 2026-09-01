import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "//src")

import amdockvs.io.api as loader_api_module
from amdockvs import AMDockVSRuntime
from amdockvs.docking.protocols import protocol_hash
from amdockvs.io.api import LoaderAPI
from amdockvs.manifest import manifest


def _patch_fake_home(monkeypatch, fake_home: Path):
    monkeypatch.setenv("HOME", str(fake_home))


def _make_smiles_file(path: Path, count: int = 8):
    with path.open("w", encoding="utf-8") as handle:
        for idx in range(count):
            smiles = "CCO" if idx % 2 == 0 else "CCN"
            handle.write(f"{smiles} LIG_{idx:04d}\n")


def _make_sdf_file(path: Path, count: int = 8):
    from rdkit import Chem

    writer = Chem.SDWriter(str(path))
    for idx in range(count):
        mol = Chem.MolFromSmiles("CCO" if idx % 2 == 0 else "CCN")
        assert mol is not None
        mol.SetProp("_Name", f"SDF_{idx:04d}")
        writer.write(mol)
    writer.close()


def _make_receptor_pdb(path: Path):
    path.write_text(
        "\n".join(
            [
                "ATOM      1  N   MET A   1      11.104  13.207  10.147  1.00 20.00           N",
                "ATOM      2  CA  MET A   1      12.560  13.100  10.350  1.00 20.00           C",
                "END",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _wait_until(predicate, *, timeout_s: float = 5.0, poll_s: float = 0.05):
    deadline = time.monotonic() + timeout_s
    last_value = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(poll_s)
    return last_value


def _require_docking_stack():
    pytest.importorskip("rdkit")
    pytest.importorskip("meeko")
    pytest.importorskip("vina")


def _wait_completed(runtime: AMDockVSRuntime, job_ids: list[str], *, timeout_s: float = 120.0):
    status_map = runtime.wait_for_jobs(job_ids, timeout_s=timeout_s, poll_s=0.05)
    assert all(item.status == "completed" for item in status_map.values())
    return status_map


def _role_scope(runtime: AMDockVSRuntime, role: str, *, limit: int | None = None):
    return runtime.molecules.select(role=role, limit=limit)


def _role_rows(runtime: AMDockVSRuntime, role: str, *, limit: int | None = None):
    return list(runtime.molecules.stream(_role_scope(runtime, role, limit=limit)))


def _role_count(runtime: AMDockVSRuntime, role: str) -> int:
    return runtime.molecules.count(_role_scope(runtime, role))


class _FakeLoaderRuntime:
    def __init__(self):
        self.required_calls = 0
        self.submit_job_calls = 0

    def _require_active_project(self):
        self.required_calls += 1
        return object()

    def submit_job(self, *_args, **_kwargs):
        self.submit_job_calls += 1
        raise AssertionError("LoaderAPI should dispatch through JobDefinition helpers.")


class _FakeLoaderJob:
    def __init__(self):
        self.calls = []

    def submit_with_options(self, runtime, *, params, config=None, **kwargs):
        self.calls.append(
            {
                "runtime": runtime,
                "params": dict(params),
                "config": config,
                "kwargs": dict(kwargs),
            }
        )
        return "job-001"


def test_generate_ligand_3d_keeps_embedded_conformer_when_forcefield_params_are_missing(monkeypatch):
    pytest.importorskip("rdkit")

    from rdkit import Chem
    from rdkit.Chem import AllChem

    from amdockvs.chemistry.tools.ligands import generate_ligand_3d

    mol = Chem.MolFromSmiles("CCO")
    assert mol is not None

    monkeypatch.setattr(AllChem, "MMFFHasAllMoleculeParams", lambda _mol: False)
    monkeypatch.setattr(AllChem, "UFFHasAllMoleculeParams", lambda _mol: False)

    result = generate_ligand_3d(mol, optimize=True, fragment_mode="keep", filter_metals=False, filter_simple_ions=False)

    assert result.GetNumConformers() == 1
    assert result.HasProp("_amdock_is_minimized")
    assert result.GetBoolProp("_amdock_is_minimized") is False


def test_loader_api_dispatches_jobs_via_jobdefinition_helpers(monkeypatch, tmp_path):
    runtime = _FakeLoaderRuntime()
    api = LoaderAPI(runtime)
    fake_job = _FakeLoaderJob()
    ligands_file = tmp_path / "ligands.smi"
    ligands_file.write_text("CCO LIG_0001\n", encoding="utf-8")

    monkeypatch.setattr(loader_api_module, "load_molecules_file_job", fake_job)

    job_ids = api.load_molecules(
        [ligands_file],
        batch_size=25,
        executor_name="thread",
        max_job_cpus=3,
        primary_role="ligand",
        primary_context="screening",
        molecule_kind="small_molecule",
    )

    assert job_ids == ["job-001"]
    assert runtime.required_calls == 1
    assert runtime.submit_job_calls == 0
    assert fake_job.calls == [
        {
            "runtime": runtime,
            "config": None,
            "params": {
                "file_paths": [str(ligands_file.resolve())],
                "batch_size": 25,
                "storage_resource": "molecules",
                "primary_role": "ligand",
                "primary_context": "screening",
                "molecule_kind": "small_molecule",
            },
            "kwargs": {
                "executor_name": "thread",
                "depends_on": None,
                "max_job_cpu": 3,
                "total_chunks": 1,
                "max_inflight_tasks": 32,
            },
        }
    ]


def test_load_receptors_headless_resolves_box_size(monkeypatch, tmp_path):
    """Headless import (no UI) must plumb the search-box edge into every file's import_profile:
    an explicit value wins, an omitted one falls back to config. This is the path the UI-assembles-
    then-saves refactor moved into the API, and the branch large imports silently dropped before."""
    receptor = tmp_path / "rec.pdb"
    _make_receptor_pdb(receptor)
    fake_job = _FakeLoaderJob()
    monkeypatch.setattr(loader_api_module, "load_receptors_file_job", fake_job)
    api = LoaderAPI(_FakeLoaderRuntime())  # no amdock_configuration -> app_config uses packaged default

    api.load_receptors([receptor], binding_site_box_size=24.0, build_specs=False)
    api.load_receptors([receptor], build_specs=False)  # omitted -> config default (20.0)

    key = str(receptor.resolve())
    explicit = fake_job.calls[0]["params"]["extra_data_patch_by_file"][key]["structure"]["import_profile"]
    from_config = fake_job.calls[1]["params"]["extra_data_patch_by_file"][key]["structure"]["import_profile"]
    assert explicit["binding_site_box_size"] == 24.0
    from amdockvs.configuration import DEFAULT_BINDING_SITE_BOX_SIZE

    assert from_config["binding_site_box_size"] == DEFAULT_BINDING_SITE_BOX_SIZE


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_manifest_and_runtime_declare_project_resource_contract(tmp_path, monkeypatch):
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        assert tuple(spec.key for spec in manifest.project_resources) == (
            "molecules",
            "docking_results",
            "qsar_models",
            "pocket_predictions",
            "exports",
            "jobs",
        )

        project = runtime.create_project(
            name="resource_contract",
            folder=tmp_path / "resource_project",
            description="resource contract test",
        )
        paths = runtime.get_project_paths()

        assert project.scope == "docking"
        assert runtime.get_project_resource_path("molecules") == paths["molecule_data_dir"]
        assert runtime.get_project_resource_path("docking_results") == paths["docking_results_dir"]
        assert runtime.get_project_resource_path("pocket_predictions").is_dir()
        assert paths["molecule_data_dir"].is_dir()
        assert paths["docking_results_dir"].is_dir()
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_monitor_bridge_tracks_real_import_job_detail_and_history(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    pytest.importorskip("PySide6")
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    bridge = runtime.create_monitor_bridge(poll_ms=50, max_recent_jobs=20)
    try:
        runtime.create_project(name="monitor_project", folder=tmp_path / "monitor_project", description="monitor test")
        ligands_file = tmp_path / "monitor_ligands.smi"
        _make_smiles_file(ligands_file, count=6)

        job_ids = runtime.loader.load_ligands([ligands_file], batch_size=2, executor_name="thread")
        _wait_completed(runtime, job_ids)

        snapshot = bridge.refresh_now()
        detail = bridge.get_job_detail(job_ids[0])
        history = bridge.get_job_history(limit=10)
        capabilities = {item.action: item for item in bridge.get_action_capabilities(job_ids[0])}

        assert snapshot is not None
        assert snapshot.has_project is True
        assert snapshot.project_name == "monitor_project"
        assert any(job.job_id == job_ids[0] for job in snapshot.jobs)
        assert detail is not None
        assert detail.job is not None
        assert detail.job.status == "completed"
        assert len(detail.events) >= 1
        assert any(job.job_id == job_ids[0] for job in history)
        assert capabilities["refresh"].supported is True
        assert capabilities["cancel_job"].supported is False
        assert capabilities["resubmit_job"].supported is True
    finally:
        bridge.stop()
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_runtime_import_and_descriptor_pipeline(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        project = runtime.create_project(name="pipeline", folder=tmp_path / "project", description="pipeline test")
        assert project.app_id == "amdockvs"
        ligands_file = tmp_path / "ligands.smi"
        receptor_file = tmp_path / "receptor.pdb"
        _make_smiles_file(ligands_file, count=12)
        _make_receptor_pdb(receptor_file)

        load_jobs = [
            *runtime.loader.load_ligands([ligands_file], batch_size=5, executor_name="thread"),
            *runtime.loader.load_receptors([receptor_file], batch_size=5, executor_name="thread"),
        ]
        _wait_completed(runtime, load_jobs)

        descriptor_job = runtime.qsar.compute_descriptors(batch_size=4, executor_name="thread")
        descriptor_status = runtime.wait_for_jobs([descriptor_job], timeout_s=120)
        assert descriptor_status[descriptor_job].status == "completed"

        ligands = _role_rows(runtime, "ligand")
        receptors = _role_rows(runtime, "receptor")
        descriptors = runtime.qsar.list_descriptors()

        assert len(ligands) == 12
        assert len(receptors) == 1
        assert len(descriptors) == 12
        assert _role_count(runtime, "ligand") == 12
        assert _role_count(runtime, "receptor") == 1
        assert runtime.get_active_project().name == "pipeline"
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_runtime_workflow_selection_filters_incompatible_ligands(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="workflow_filtering", folder=tmp_path / "workflow_filtering", description="workflow filter test")
        small_molecule_file = tmp_path / "small_molecules.smi"
        peptide_file = tmp_path / "peptides.smi"
        receptor_file = tmp_path / "workflow_receptor.pdb"
        _make_smiles_file(small_molecule_file, count=4)
        _make_smiles_file(peptide_file, count=3)
        _make_receptor_pdb(receptor_file)

        load_jobs = [
            *runtime.loader.load_ligands([small_molecule_file], batch_size=2, executor_name="thread"),
            *runtime.loader.load_molecules(
                [peptide_file],
                batch_size=2,
                executor_name="thread",
                primary_role="ligand",
                molecule_kind="peptide",
            ),
            *runtime.loader.load_receptors([receptor_file], batch_size=1, executor_name="thread"),
        ]
        _wait_completed(runtime, load_jobs)

        all_ligands = list(runtime.molecules.stream(runtime.molecules.select(role="ligand")))
        vina_ligands = list(runtime.molecules.stream(runtime.molecules.select(role="ligand", workflow="vina")))
        qsar_ligands = list(runtime.molecules.stream(runtime.molecules.select(role="ligand", workflow="qsar")))
        vina_receptors = list(runtime.molecules.stream(runtime.molecules.select(role="receptor", workflow="vina")))

        assert len(all_ligands) == 7
        assert len(vina_ligands) == 4
        assert len(qsar_ligands) == 4
        assert all(row.molecule_type in {"small_molecule", "macrocycle"} for row in vina_ligands)
        assert len(vina_receptors) == 1
        assert vina_receptors[0].molecule_type == "protein"
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_runtime_qsar_descriptors_ignore_non_qsar_ligands(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="qsar_filtering", folder=tmp_path / "qsar_filtering", description="qsar filter test")
        small_molecule_file = tmp_path / "qsar_small_molecules.smi"
        peptide_file = tmp_path / "qsar_peptides.smi"
        _make_smiles_file(small_molecule_file, count=5)
        _make_smiles_file(peptide_file, count=2)

        load_jobs = [
            *runtime.loader.load_ligands([small_molecule_file], batch_size=2, executor_name="thread"),
            *runtime.loader.load_molecules(
                [peptide_file],
                batch_size=2,
                executor_name="thread",
                primary_role="ligand",
                molecule_kind="peptide",
            ),
        ]
        _wait_completed(runtime, load_jobs)

        descriptor_job = runtime.qsar.compute_descriptors(batch_size=2, executor_name="thread")
        descriptor_status = runtime.wait_for_jobs([descriptor_job], timeout_s=120)
        assert descriptor_status[descriptor_job].status == "completed"

        allowed_ids = {
            int(row.id or 0)
            for row in runtime.molecules.stream(runtime.molecules.select(role="ligand", workflow="qsar"))
        }
        descriptor_ids = {int(row["molecule_id"] or 0) for row in runtime.qsar.list_descriptors()}

        assert len(allowed_ids) == 5
        assert descriptor_ids == allowed_ids
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_runtime_import_ligands_streams_full_file(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="limited_import", folder=tmp_path / "project_limited", description="limit test")
        ligands_file = tmp_path / "limited_ligands.smi"
        _make_smiles_file(ligands_file, count=12)

        job_ids = runtime.loader.load_ligands(
            [ligands_file],
            batch_size=2,
            executor_name="thread",
        )
        _wait_completed(runtime, job_ids)

        ligands = _role_rows(runtime, "ligand")
        assert len(ligands) == 12
        assert sorted(item.name for item in ligands) == [f"LIG_{idx:04d}" for idx in range(12)]

        detailed = runtime.wait_for_job(job_ids[0], poll_s=0.05)
        assert detailed.feed_cursor_position == 6
        assert detailed.feed_items_acked == 6
        assert detailed.chunks_total == 6
        assert detailed.chunks_done == 6
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_runtime_import_prefilter_discards_ligands_early(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    _patch_fake_home(monkeypatch, tmp_path)

    from ms_flow.query import db_rows

    from amdockvs.constants import TABLE_MOLECULES

    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="prefilter_exclusion", folder=tmp_path / "prefilter_exclusion", description="prefilter")
        ligands_file = tmp_path / "prefilter_ligands.smi"
        _make_smiles_file(ligands_file, count=4)

        job_ids = runtime.loader.load_ligands(
            [ligands_file],
            batch_size=2,
            executor_name="thread",
            prefilter={"max_atoms": 1},
        )
        _wait_completed(runtime, job_ids)

        selected_ligands = _role_rows(runtime, "ligand")
        inventory_rows = db_rows(
            runtime.molsuite.project_db,
            TABLE_MOLECULES,
            filters={"is_ligand": True},
            order=("id",),
        )

        assert selected_ligands == []
        assert inventory_rows == []
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_runtime_import_ligands_multithreaded_sdf_runs_as_single_chunk_job(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="mt_sdf_import", folder=tmp_path / "project_mt_sdf", description="mt sdf test")
        ligands_file = tmp_path / "ligands_mt.sdf"
        _make_sdf_file(ligands_file, count=6)

        job_ids = runtime.loader.load_ligands_multithreaded_sdf(
            [ligands_file],
            executor_name="compute",
            num_threads=2,
            max_job_cpus=4,
        )
        _wait_completed(runtime, job_ids)

        ligands = _wait_until(lambda: _role_rows(runtime, "ligand"))
        assert len(ligands) == 6
        assert [item.name for item in ligands] == [f"SDF_{idx:04d}" for idx in range(6)]

        detailed = runtime.wait_for_job(job_ids[0], poll_s=0.05)
        assert detailed.chunks_total == 1
        assert detailed.chunks_done == 1
        assert detailed.max_job_cpu == 4
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_runtime_prepare_and_dock_with_stored_grid(tmp_path, monkeypatch):
    _require_docking_stack()
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="dock_grid", folder=tmp_path / "dock_grid", description="grid workflow")
        ligands_file = tmp_path / "dock_ligands.smi"
        receptor_file = tmp_path / "dock_receptor.pdb"
        _make_smiles_file(ligands_file, count=4)
        _make_receptor_pdb(receptor_file)

        load_jobs = [
            *runtime.loader.load_ligands([ligands_file], batch_size=4, executor_name="thread"),
            *runtime.loader.load_receptors([receptor_file], batch_size=1, executor_name="thread"),
        ]
        _wait_completed(runtime, load_jobs)

        receptor = next(runtime.molecules.stream(_role_scope(runtime, "receptor", limit=1)))
        runtime.docking.set_grid(
            receptor_id=int(receptor.id or 0),
            center=(12.0, 13.0, 10.0),
            size=(20.0, 20.0, 20.0),
        )
        receptor_set = runtime.molecules.create_set(
            _role_scope(runtime, "receptor"),
            name="dock_receptor_set",
            kind="snapshot",
        )

        gen3d_job = runtime.chemistry.generate_3d_ligands(batch_size=4, executor_name="thread")
        _wait_completed(runtime, [gen3d_job])

        prep_jobs = [
            runtime.docking.prepare_ligands(batch_size=4, executor_name="thread"),
            runtime.docking.prepare_receptors(batch_size=1, executor_name="thread"),
        ]
        _wait_completed(runtime, prep_jobs)

        docking_job = runtime.docking.run(
            receptor_set=receptor_set,
            batch_size=4,
            executor_name="thread",
        )
        docking_status = runtime.wait_for_jobs([docking_job], timeout_s=120)
        assert docking_status[docking_job].status == "completed"

        results = runtime.docking.list_results()
        assert len(results) == 4
        assert all(item.status == "completed" for item in results)

        # A skip-existing rerun has no work. Its declared total must be zero as well;
        # otherwise Molsuite waits forever for chunks the feed intentionally omits.
        skipped_job = runtime.docking.run(
            receptor_set=receptor_set,
            batch_size=1,
            executor_name="thread",
            skip_existing=True,
        )
        skipped_status = runtime.wait_for_jobs([skipped_job], timeout_s=20, poll_s=0.05)[skipped_job]
        assert skipped_status.status == "completed"
        assert skipped_status.chunks_total == 0
        assert skipped_status.chunks_done == 0
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_runtime_exposes_domain_queries_for_receptors_and_docking_results(tmp_path, monkeypatch):
    _require_docking_stack()
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="domain_queries", folder=tmp_path / "domain_project", description="domain test")
        ligands_file = tmp_path / "domain_ligands.smi"
        receptor_file = tmp_path / "domain_receptor.pdb"
        _make_smiles_file(ligands_file, count=6)
        _make_receptor_pdb(receptor_file)

        load_jobs = [
            *runtime.loader.load_ligands([ligands_file], batch_size=3, executor_name="thread"),
            *runtime.loader.load_receptors([receptor_file], batch_size=2, executor_name="thread"),
        ]
        _wait_completed(runtime, load_jobs)

        descriptor_job = runtime.qsar.compute_descriptors(batch_size=3, executor_name="thread")
        _wait_completed(runtime, [descriptor_job])

        receptor = next(runtime.molecules.stream(_role_scope(runtime, "receptor", limit=1)))
        runtime.docking.set_grid(
            receptor_id=int(receptor.id or 0),
            center=(12.0, 13.0, 10.0),
            size=(20.0, 20.0, 20.0),
        )
        receptor_set = runtime.molecules.create_set(
            _role_scope(runtime, "receptor"),
            name="domain_receptor_set",
            kind="snapshot",
        )

        gen3d_job = runtime.chemistry.generate_3d_ligands(batch_size=3, executor_name="thread")
        _wait_completed(runtime, [gen3d_job])

        prep_jobs = [
            runtime.docking.prepare_ligands(batch_size=3, executor_name="thread"),
            runtime.docking.prepare_receptors(batch_size=1, executor_name="thread"),
        ]
        _wait_completed(runtime, prep_jobs)

        docking_job = runtime.docking.run(receptor_set=receptor_set, batch_size=6, executor_name="thread")
        _wait_completed(runtime, [docking_job])

        docking_stats = runtime.docking.result_stats()
        receptor_summaries = runtime.docking.receptor_summaries()
        top_hits = runtime.docking.top_hits(limit=3, only_completed=True)

        assert _role_count(runtime, "receptor") == 1
        assert docking_stats.total_results == 6
        assert docking_stats.completed_results == 6
        assert docking_stats.unique_ligands == 6
        assert docking_stats.unique_receptors == 1
        assert len(receptor_summaries) == 1
        assert receptor_summaries[0].receptor_name
        assert receptor_summaries[0].total_results == 6
        assert len(top_hits) == 3
        assert top_hits[0].ligand_name
        assert top_hits[0].receptor_name
        assert top_hits[0].status == "completed"
        assert top_hits[0].output_path is not None

        # Results view pagination: one row per ligand (best pose + pose count), by pages.
        page_one = runtime.docking.ligand_summaries(limit=4)
        page_two = runtime.docking.ligand_summaries(limit=4, offset=4)
        assert len(page_one) == 4 and len(page_two) == 2  # 6 ligands docked
        assert page_one[0][0].score == top_hits[0].score  # best ligand first
        assert all(count >= 1 for _best, count in page_one + page_two)
        assert {best.ligand_id for best, _ in page_one}.isdisjoint({best.ligand_id for best, _ in page_two})
        poses = runtime.docking.filtered_hits(ligand_id=page_one[0][0].ligand_id, limit=50)
        assert len(poses) == page_one[0][1]
        assert min(hit.score for hit in poses) == page_one[0][0].score

        # Pair counts: what the Docking Studio previews before a run, and what the results
        # table compares each receptor against ("Missing").
        receptor_id = int(receptor.id or 0)
        assert runtime.docking.count_docked_pairs() == 6  # 1 receptor x 6 ligands
        assert runtime.docking.count_docked_pairs(receptor_ids=[receptor_id]) == 6
        assert runtime.docking.count_docked_pairs(receptor_ids=[-1]) == 0
        assert runtime.docking.count_docked_pairs(protocol_hash="not-a-hash") == 0
        assert receptor_summaries[0].expected_ligands == 6  # nothing missing here

        # Protocol identity: only the scientific keys count. Reporting more poses, swapping the
        # vina binding or changing the thread count is the same protocol -- otherwise the
        # skip-existing guard (it filters by protocol.hash) would re-dock everything.
        base = {"scoring_function": "vina", "exhaustiveness": 8, "num_modes": 9, "vina_backend": "binary"}
        same = dict(base, num_modes=20, vina_backend="python", vina_cpu=4)
        assert protocol_hash(program="vina", config=base) == protocol_hash(program="vina", config=same)
        assert protocol_hash(program="vina", config=base) != protocol_hash(
            program="vina", config=dict(base, exhaustiveness=16)
        )
        assert protocol_hash(program="vina", config=base) != protocol_hash(
            program="vina", config=dict(base, scoring_function="vinardo")
        )
    finally:
        runtime.shutdown()
