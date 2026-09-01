import pytest

from amdockvs.runtime import AMDockVSRuntime, _job_category


@pytest.mark.parametrize(
    "name, category",
    [
        ("amdock_load_ligands_file_job", "import"),
        ("amdock_load_ligands_multithreaded_sdf_job", "import"),
        ("amdock_load_receptors_file_job", "import"),
        ("amdock_ligand_chemistry_job", "chemistry"),
        ("amdock_receptor_chemistry_job", "chemistry"),
        ("amdock_prepare_ligands_job", "prepare"),
        ("amdock_prepare_receptors_job", "prepare"),
        ("amdock_calculate_molecule_descriptors_job", "descriptors"),
        ("amdock_docking_job", "docking"),
        ("amdock_redocking_job", "docking"),
        ("something_unknown", None),
    ],
)
def test_job_category(name, category):
    assert _job_category(name) == category


class _Status:
    def __init__(self, job_id, task_type):
        self.job_id = job_id
        self.task_type = task_type
        self.status = "running"


class _Job:
    def __init__(self, name):
        self.name = name


class _FakeRuntime:
    def __init__(self, active):
        self._active = active

    def list_jobs(self, *, statuses=()):
        return self._active

    resolve_job_dependencies = AMDockVSRuntime.resolve_job_dependencies


def test_prepare_waits_for_import_and_chemistry():
    rt = _FakeRuntime([
        _Status("imp1", "amdock_load_ligands_file_job"),
        _Status("chem1", "amdock_ligand_chemistry_job"),
        _Status("dock1", "amdock_docking_job"),  # not a prerequisite of prepare
    ])
    deps = rt.resolve_job_dependencies(_Job("amdock_prepare_ligands_job"), None)
    assert set(deps) == {"imp1", "chem1"}


def test_chemistry_jobs_are_serialized_to_protect_current_path():
    rt = _FakeRuntime([
        _Status("chem1", "amdock_ligand_chemistry_job"),
        _Status("dock1", "amdock_docking_job"),
    ])
    deps = rt.resolve_job_dependencies(_Job("amdock_ligand_chemistry_job"), None)
    assert deps == ["chem1"]


def test_docking_waits_for_prepare_chain():
    rt = _FakeRuntime([
        _Status("imp1", "amdock_load_ligands_file_job"),
        _Status("chem1", "amdock_ligand_chemistry_job"),
        _Status("prep1", "amdock_prepare_ligands_job"),
    ])
    deps = rt.resolve_job_dependencies(_Job("amdock_docking_job"), None)
    assert set(deps) == {"imp1", "chem1", "prep1"}


def test_import_has_no_prerequisites():
    rt = _FakeRuntime([_Status("imp1", "amdock_load_ligands_file_job")])
    assert rt.resolve_job_dependencies(_Job("amdock_load_receptors_file_job"), None) is None


def test_explicit_deps_are_unioned():
    rt = _FakeRuntime([_Status("imp1", "amdock_load_ligands_file_job")])
    deps = rt.resolve_job_dependencies(_Job("amdock_prepare_ligands_job"), ["manual"])
    assert set(deps) == {"manual", "imp1"}
