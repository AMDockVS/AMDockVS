"""push_scope / pop_scope: a tool narrows a borrowed catalog table and gives it back.

The part worth a test is the giving back: pop must restore the catalog's *default* filters,
not "no filter" — that is how a table ends up silently showing rows it never shows.
"""
from pathlib import Path
import sys

import pytest

import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6 (see test_ui_job_feedback)

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from sqlalchemy.orm import sessionmaker
from sqlmodel import Field, Session, SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ms_components.ms_table import ColumnDef, FilterOperator, FilterSpec, TableConfig, ToolbarAction

from amdockvs.ui.catalog.common import BoundTableWidget, SelectionStore


class _Mol(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    molecule_type: str = ""
    usage_class: str = ""
    excluded: bool = False


class _Runtime:
    """Minimum BoundTableWidget asks for: an active context and a session provider."""

    def __init__(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine, tables=[_Mol.__table__])
        factory = sessionmaker(bind=engine, class_=Session)
        self.active_context = object()
        self.amdock_configuration = None
        self.workspace = type("MS", (), {"project_db": type("DB", (), {"get_session": lambda _s: factory()})()})()


def _widget(parent=None):
    QApplication.instance() or QApplication(["amdockvs-scope-test"])
    config = TableConfig(
        model_class=_Mol,
        columns=[ColumnDef("id"), ColumnDef("molecule_type"), ColumnDef("usage_class")],
        default_filters=[
            FilterSpec("usage_class", FilterOperator.EQ, "general", label="general_only"),
            FilterSpec("excluded", FilterOperator.EQ, False, label="selected_only"),
        ],
    )
    return BoundTableWidget(runtime=_Runtime(), config=config, empty_text="no project", parent=parent)


def _active(widget):
    return {f.field: f.value for f in widget.table._builder.active_filters}


def test_pop_scope_restores_catalog_defaults_and_drops_the_rest():
    widget = _widget()
    assert _active(widget) == {"usage_class": "general", "excluded": False}

    widget.push_scope(
        "docking",
        filters=[
            FilterSpec("molecule_type", FilterOperator.EQ, "protein", label="type"),
            FilterSpec("usage_class", FilterOperator.EQ, "reference", label="reference"),
        ],
        clause=_Mol.id > 0,
        actions=[ToolbarAction(label="Prepare", on_click=lambda objs: None)],
        empty_message="nothing left to prepare",
    )
    assert _active(widget) == {"molecule_type": "protein", "usage_class": "reference", "excluded": False}
    assert [a.text() for a in widget.table._keyed_actions["docking"]] == ["Prepare"]

    widget.pop_scope("docking")
    # molecule_type had no default: gone. usage_class had one: back to the catalog's own.
    assert _active(widget) == {"usage_class": "general", "excluded": False}
    assert "docking" not in widget.table._keyed_actions
    assert widget._base_clauses["docking"] is None


def test_repushing_a_narrower_scope_gives_back_the_field_it_dropped():
    widget = _widget()
    widget.push_scope("docking", filters=[
        FilterSpec("molecule_type", FilterOperator.EQ, "protein", label="type"),
        FilterSpec("usage_class", FilterOperator.EQ, "reference", label="reference"),
    ])
    widget.push_scope("docking", filters=[
        FilterSpec("molecule_type", FilterOperator.EQ, "small_molecule", label="type"),
    ])
    assert _active(widget) == {"molecule_type": "small_molecule", "usage_class": "general", "excluded": False}


def test_selection_is_shared_by_the_tables_bound_to_its_role():
    """Selecting in Ligands is found by a tool that borrows Molecules, and cleared in one place."""
    from PySide6.QtWidgets import QWidget

    app = QApplication.instance() or QApplication(["amdockvs-scope-test"])
    window = QWidget()
    window.selection = SelectionStore(window)
    ligands, molecules = _widget(window), _widget(window)
    ligands.selection_role = "ligand"  # what LigandWidget declares
    ligands.refresh()
    app.processEvents()

    ligands._select_rows([_Mol(id=3), _Mol(id=1)])
    app.processEvents()
    assert window.selection.get("ligand") == [1, 3]

    molecules.bind_selection("ligand")  # Build on its Small molecules tab
    assert _active(molecules)["id"] == [1, 3]
    molecules.bind_selection("receptor")  # Proteins tab: no receptor selected, nothing narrowed
    assert "id" not in _active(molecules)
    assert window.selection.get("ligand") == [1, 3]

    molecules.bind_selection("ligand")
    molecules.set_base_filter("id", None)  # the filter cleared from the borrowed table's header
    app.processEvents()
    assert window.selection.get("ligand") is None and "id" not in _active(ligands)

    window.selection.set("ligand", [2])
    molecules.bind_selection(None)  # the tool leaves: its table lets go, the selection stays
    app.processEvents()
    assert "id" not in _active(molecules) and _active(ligands)["id"] == [2]


def test_reveal_shows_one_row_without_touching_the_selection_and_show_all_gives_it_back():
    """Docking Results → "Show in Ligands": a look at one row, not a new selection."""
    from PySide6.QtWidgets import QWidget

    app = QApplication.instance() or QApplication(["amdockvs-scope-test"])
    window = QWidget()
    window.selection = SelectionStore(window)
    window.selection.set("ligand", [2, 3])
    ligands = _widget(window)
    ligands.selection_role = "ligand"
    ligands.refresh()
    app.processEvents()
    assert _active(ligands)["id"] == [2, 3]

    ligands.reveal([7], label="ligand 7")
    app.processEvents()
    assert _active(ligands)["id"] == [7]
    assert window.selection.get("ligand") == [2, 3]
    window.selection.set("ligand", [4])  # selected elsewhere meanwhile: the revealed row stays
    assert _active(ligands)["id"] == [7]

    show_all = ligands.table._keyed_actions["reveal"][0]
    assert show_all.text() == "Show all"
    show_all.trigger()
    app.processEvents()
    assert _active(ligands)["id"] == [4] and "reveal" not in ligands.table._keyed_actions
    assert window.selection.get("ligand") == [4]


def test_clearing_the_selection_sticks_when_a_tool_reloads_the_table_first():
    """Status-bar ✕ while the docking studio is on Ligands: the studio hears the change first and
    re-pushes its scope; that reload must not write the old id filter back into the store."""
    from PySide6.QtWidgets import QWidget

    app = QApplication.instance() or QApplication(["amdockvs-scope-test"])
    window = QWidget()
    window.selection = SelectionStore(window)
    ligands = _widget(window)
    ligands.selection_role = "ligand"

    def tool(_role):
        clause = None if window.selection.get("ligand") else (_Mol.id > 0)
        ligands.push_scope("docking", clause=clause)

    window.selection.changed.connect(tool)  # before the table's own listener
    ligands.refresh()
    app.processEvents()
    window.selection.set("ligand", [1, 2])
    app.processEvents()
    assert _active(ligands)["id"] == [1, 2]

    window.selection.set("ligand", None)
    app.processEvents()
    assert window.selection.get("ligand") is None and "id" not in _active(ligands)


def test_delete_all_shown_takes_every_filtered_row_not_just_the_loaded_page(monkeypatch):
    from amdockvs.ui.catalog import deletion

    BoundTableWidget.delete_kind = "molecule"  # what a deleting catalog subclass declares
    try:
        widget = _widget()
    finally:
        BoundTableWidget.delete_kind = None
    widget.delete_kind = "molecule"
    with widget.runtime.workspace.project_db.get_session() as session:
        session.add_all([_Mol(usage_class="general") for _ in range(45)] + [_Mol(usage_class="reference")])
        session.commit()
    asked = []
    monkeypatch.setattr(deletion, "delete_objects", lambda _p, _r, objects, **_k: asked.extend(objects))
    (action,) = widget.table._keyed_actions["delete_shown"]  # a toolbar verb, not a row-menu one
    action.trigger()
    assert asked == list(range(1, 46))  # the reference row is filtered out, so it stays
