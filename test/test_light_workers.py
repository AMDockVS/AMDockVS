"""A worker imports the module its task is pickled by, and everything that module imports."""
import subprocess
import sys

import pytest

import amdockvs.docking.jobs as docking_jobs
import amdockvs.docking.preparation.jobs as preparation_jobs
import amdockvs.docking.results.jobs as results_jobs

LIGHT = [
    docking_jobs.DockingJobSpec,
    docking_jobs.RedockingJobSpec,
    preparation_jobs.PrepareLigandsJobSpec,
    preparation_jobs.PrepareReceptorsJobSpec,
    preparation_jobs.PrepareLigandShardsJobSpec,
    results_jobs.InteractionJobSpec,
    results_jobs.DiagramJobSpec,
]


@pytest.mark.parametrize("spec", LIGHT, ids=lambda spec: spec.__name__)
def test_the_module_of_a_task_brings_no_qt_and_no_sql(spec):
    module = spec.run_chunk.__module__
    assert not module.endswith("jobs"), f"{spec.__name__} runs a function of its declaration module"
    code = (f"import sys, amdockvs.core.jobs, {module}; "
            "found = [name for name in ('PySide6', 'sqlalchemy', 'pydantic') if name in sys.modules]; "
            "assert not found, found")
    subprocess.run([sys.executable, "-c", code], check=True)
