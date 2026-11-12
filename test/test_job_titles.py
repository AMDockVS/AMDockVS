"""T3: the Jobs monitor and the job notices name a job by what it does, not by its function."""
from types import SimpleNamespace

from amdockvs.core.job_titles import job_label, job_title
from amdockvs.runtime import AMDockVSRuntime


def test_docking_jobs_are_titled_by_program_and_stage():
    assert job_title("amdock_docking_job", {"engine": "gnina"}) == "Docking · gnina (CNN)"
    assert job_title("amdock_docking_job", {"engine": "vina"}) == "Docking · AutoDock Vina"
    assert (job_title("amdock_docking_job", {"engine": "qvina", "selected_from_receptor_id": 3})
            == "Docking · QuickVina 2 · off-target stage")
    assert job_title("amdock_docking_job", {"engine": "not_registered"}) == "Docking · not_registered"


def test_imports_and_chemistry_say_what_they_work_on():
    assert job_title("amdock_load_receptors_file_job", {"file_path": "/data/4OYS.pdb"}) == "Import receptors · 4OYS.pdb"
    assert (job_title("amdock_load_receptors_file_job", {"file_paths": ["/a/4OYS.pdb", "/a/4UWH.pdb", "/a/4UWF.cif"]})
            == "Import receptors · 4OYS.pdb +2")
    assert (job_title("amdock_ligand_chemistry_job", {"operation": [["standardize", {}], ["protonate", {"ph": 7.4}]]})
            == "Ligand chemistry · standardize, protonate")
    assert job_title("amdock_prepare_receptors_job", {"engine": "ad4"}) == "Prepare receptors"


def test_without_params_or_for_an_unknown_job_the_kind_alone():
    assert job_title("amdock_docking_job") == "Docking"
    assert job_label("amdock_some_new_thing_job") == "Some new thing"
    assert job_label("") == "Job"


def test_the_runtime_titles_an_earlier_job_from_the_journal_and_asks_once():
    asked = []

    def origin(key):
        asked.append(key)
        if key == "lost":
            raise ValueError("not on record")
        return {"params": {"engine": "gnina"}}

    runtime = SimpleNamespace(_job_titles={"new": "Docking · AutoDock Vina"}, workspace=SimpleNamespace(origin=origin))
    title = AMDockVSRuntime.job_title
    assert title(runtime, "new", "amdock_docking_job") == "Docking · AutoDock Vina"  # titled at submit
    assert title(runtime, "old", "amdock_docking_job") == "Docking · gnina (CNN)"
    assert title(runtime, "old", "amdock_docking_job") == "Docking · gnina (CNN)"
    assert title(runtime, "lost", "amdock_docking_job") == "Docking"
    assert asked == ["old", "lost"]
