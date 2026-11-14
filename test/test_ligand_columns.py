"""The state flags a later step depends on (3D, H...) must be visible and filterable in Ligands."""
from types import SimpleNamespace

# The runtime before PySide6: shiboken's feature loader trips over pydantic's lazy shim otherwise.
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401
from ms_components.ms_table import ColumnKind

from amdockvs.ui.catalog.ligands import _ligand_table_config


def test_ligands_show_and_filter_the_hidden_state_flags():
    columns = {c.field: c for c in _ligand_table_config(runtime=SimpleNamespace()).columns}

    assert columns["has_3d"].visible
    for field in ("has_3d", "has_hs", "is_minimized"):
        # INTEGER so the numeric "= 0" filter binds to SQLite's 0/1 booleans.
        assert columns[field].kind == ColumnKind.INTEGER and columns[field].filterable
        assert (columns[field].formatter(True), columns[field].formatter(False)) == ("Yes", "No")
    assert columns["exclusion_reason"].filterable


def test_ligands_show_and_filter_the_formal_charge_of_the_current_structure():
    columns = {c.field: c for c in _ligand_table_config(runtime=SimpleNamespace()).columns}

    charge = columns["formal_charge"]
    assert charge.visible and charge.filterable and charge.kind == ColumnKind.INTEGER
    assert [charge.formatter(v) for v in (-2, 0, 1, None)] == ["-2", "0", "+1", ""]
