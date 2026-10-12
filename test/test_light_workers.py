"""A worker imports the module its task is pickled by, and everything that module imports."""
import importlib
import pkgutil
import subprocess
import sys

import pytest

import amdockvs
from amdockvs.core.jobs import JobDefinition

# ponytail: clustering still runs a function of its jobs module; move it with the rest when that file settles.
NOT_YET = {"amdockvs.diversity.jobs"}


def _task_modules() -> list[str]:
    found = set()
    for info in pkgutil.walk_packages(amdockvs.__path__, "amdockvs."):
        if ".ui" in info.name or not info.name.endswith("jobs"):
            continue
        for value in vars(importlib.import_module(info.name)).values():
            definition = value if isinstance(value, JobDefinition) else getattr(value, "__dict__", {}).get("definition")
            if isinstance(definition, JobDefinition):
                found.add(definition.run_chunk.__module__)
    return sorted(found - NOT_YET)


@pytest.mark.parametrize("module", _task_modules())
def test_the_module_of_a_task_brings_no_qt_and_no_sql(module):
    assert not module.endswith("jobs"), f"{module}: a task is pickled by the module that declares its job"
    code = (f"import sys, amdockvs.core.jobs, {module}; "
            "found = [name for name in ('PySide6', 'sqlalchemy') if name in sys.modules]; assert not found, found")
    subprocess.run([sys.executable, "-c", code], check=True)
