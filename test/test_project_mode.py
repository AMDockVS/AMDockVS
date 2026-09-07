"""The campaign mode is derived from the data, not declared at creation.

What is worth testing is the derivation (a project becomes `htpvs` the moment it holds a
shard) and the invariant behind it: a project never ends up with two screening libraries.
"""
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.core.constants import TABLE_SCREENING_SHARDS
from amdockvs.molecules.storage import (
    LIBRARY_ROWS,
    LIBRARY_SHARDS,
    check_library_target,
    library_kind,
)
from amdockvs.runtime import AMDockVSRuntime
from amdockvs.core.vocab import ProjectMode, ShardState


@pytest.fixture
def runtime(tmp_path):
    rt = AMDockVSRuntime()
    rt.create_project(name="derived", folder=tmp_path)
    yield rt
    rt.close_project()


def _add_shard(runtime, tmp_path):
    from datetime import datetime

    from amdockvs.models.screening import ScreeningShard

    with runtime.molsuite.project_db.get_session() as session:
        session.add(
            ScreeningShard(
                source=str(tmp_path / "lib.smi"),
                shard_index=0,
                path=str(tmp_path / "shard_00000.smi"),
                input_format="smi",
                n_records=10,
                state=ShardState.PENDING,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )
        session.commit()


def test_mode_follows_the_data(runtime, tmp_path):
    project_db = runtime.molsuite.project_db
    assert library_kind(project_db) == ProjectMode.VS == runtime.mode
    _add_shard(runtime, tmp_path)
    assert library_kind(project_db) == ProjectMode.HTPVS == runtime.mode


def test_mode_is_not_stored_anywhere(runtime, tmp_path):
    """It survives a reopen because there is nothing to persist — the shards are the state."""
    summary_id = runtime.get_active_project().id
    _add_shard(runtime, tmp_path)
    runtime.close_project()
    runtime.open_project(summary_id)
    assert runtime.mode == ProjectMode.HTPVS


def test_shards_refuse_to_join_a_row_library(runtime, tmp_path):
    """One screening library per project: that is what lets a scope name a single store."""
    project_db = runtime.molsuite.project_db
    check_library_target(project_db, target=LIBRARY_ROWS)  # empty project: either is fine
    check_library_target(project_db, target=LIBRARY_SHARDS)
    _add_shard(runtime, tmp_path)
    check_library_target(project_db, target=LIBRARY_SHARDS)  # more shards are fine
    with pytest.raises(ValueError, match="sharded"):
        check_library_target(project_db, target=LIBRARY_ROWS)


def test_shard_table_is_the_only_source_of_truth():
    assert TABLE_SCREENING_SHARDS == "screening_shards"


def test_the_import_dialog_routes_the_library_to_the_chosen_store(runtime, tmp_path, monkeypatch):
    """The checkbox is the whole mode decision, so it is worth checking that ticking it sends
    the small molecules to `shard_ligands` and leaves the other kinds on `load_ligands`."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.import_workspace import LigandImportDialog
    from amdockvs.core.vocab import MoleculeType

    QApplication.instance() or QApplication(["amdockvs-import-test"])
    library, receptor = tmp_path / "lib.smi", tmp_path / "rec.pdb"
    library.write_text("CCO one\n")
    receptor.write_text("ATOM\n")

    dialog = LigandImportDialog(runtime=runtime, defer=True)
    dialog.table.add_files([str(library), str(receptor)])
    dialog.table.cellWidget(1, 1).setCurrentIndex(  # second row is the receptor
        dialog.table.cellWidget(1, 1).findData(MoleculeType.PROTEIN)
    )
    dialog.shard_checkbox.setChecked(True)

    calls: list[tuple[str, str]] = []
    loader = type(
        "Loader",
        (),
        {
            "shard_ligands": lambda self, paths, **kw: calls.append(("shards", kw["molecule_kind"])) or ["j1"],
            "load_ligands": lambda self, paths, **kw: calls.append(("rows", kw["molecule_kind"])) or ["j2"],
        },
    )()
    submit, _name = dialog.workflow_submit()
    submit(SimpleNamespace(loader=loader, mode=ProjectMode.VS))
    assert sorted(calls) == [("rows", MoleculeType.PROTEIN), ("shards", MoleculeType.SMALL_MOLECULE)]
    assert dialog._target_sharded  # and the dialog lands on Shards, not on an empty Ligands


def test_the_import_dialog_defaults_to_the_existing_library(runtime, tmp_path):
    """Sharded project: shards are the default, but curated ligands can still come in as rows."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.import_workspace import LigandImportDialog

    QApplication.instance() or QApplication(["amdockvs-import-test"])
    _add_shard(runtime, tmp_path)
    dialog = LigandImportDialog(runtime=runtime, defer=True)
    assert dialog.shard_checkbox.isChecked() and dialog.shard_checkbox.isEnabled()

    cocrystal = tmp_path / "ref.sdf"
    cocrystal.write_text("\n")
    dialog.table.add_files([str(cocrystal)])
    dialog.shard_checkbox.setChecked(False)

    contexts: list[str] = []
    loader = type("Loader", (), {
        "load_ligands": lambda self, paths, **kw: contexts.append(kw["primary_context"]) or ["j1"],
    })()
    submit, _name = dialog.workflow_submit()
    submit(SimpleNamespace(loader=loader, mode=ProjectMode.HTPVS))
    # Rows in a sharded project are curated by definition — never a second screening library.
    assert contexts == ["reference"] and not dialog._target_sharded


def test_reference_ligands_are_not_the_screening_library(runtime, tmp_path):
    """`usage_class` follows the context, so a reference import never inflates the library count."""
    from amdockvs.io.transformers.rows import _build_row
    from amdockvs.core.vocab import MoleculeUsageClass

    def row(context):
        return _build_row(
            project_root=tmp_path, source_file=tmp_path / "l.sdf", source_index=0, name="l",
            n_atoms=1, input_format="sdf", stored_path=tmp_path / "l.sdf", current_path=None,
            metadata={}, molecule_kind="small_molecule", primary_role="ligand",
            primary_context=context,
        )

    assert row("general")["usage_class"] == MoleculeUsageClass.GENERAL
    assert row("reference")["usage_class"] == MoleculeUsageClass.REFERENCE
