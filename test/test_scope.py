"""Scope = what the table shows. "Select" is a shortcut for typing the ids into the filter.

The point of the design is that there is no scope object to get out of sync: a tool reads
the rows its table is showing, so the filter chip in the header IS the scope.
"""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from sqlalchemy.orm import sessionmaker
from sqlmodel import Field, Session, SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ms_components.ms_table import ColumnDef, ColumnKind, FilterOperator, FilterSpec, TableConfig

from amdockvs.ui.catalog.common import BoundTableWidget


class _Mol(SQLModel, table=True):
    __tablename__ = "scope_test_mol"
    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    excluded: bool = False


class _Runtime:
    def __init__(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine, tables=[_Mol.__table__])
        factory = sessionmaker(bind=engine, class_=Session)
        with factory() as session:
            session.add_all([_Mol(name=f"m{i}", excluded=i == 3) for i in range(5)])
            session.commit()
        self.active_context = object()
        self.amdock_configuration = None
        self.molsuite = type("MS", (), {"project_db": type("DB", (), {"get_session": lambda _s: factory()})()})()


class _SelectableWidget(BoundTableWidget):
    selectable = True


@pytest.fixture
def widget():
    QApplication.instance() or QApplication(["amdockvs-scope-test"])
    config = TableConfig(
        model_class=_Mol,
        columns=[ColumnDef("id", kind=ColumnKind.INTEGER), ColumnDef("name")],
        # The catalog's own default: the scope starts as "everything not excluded".
        default_filters=[FilterSpec("excluded", FilterOperator.EQ, False, label="selected_only")],
    )
    return _SelectableWidget(runtime=_Runtime(), config=config, empty_text="no project")


def test_select_writes_the_ids_into_the_id_column_filter(widget):
    widget._select_rows([SimpleNamespace(id=2), SimpleNamespace(id=5), SimpleNamespace(id=0)])

    spec = next(f for f in widget.table._builder.active_filters if f.field == "id")
    assert (spec.op, spec.value) == (FilterOperator.IN, [2, 5])
    assert sorted(widget.table.all_filtered_ids()) == [2, 5]
    # Row 4 is excluded=True: the catalog default still applies, so "Select" narrows the
    # scope, it never widens it past what the table was already showing.
    widget._select_rows([SimpleNamespace(id=4)])
    assert widget.table.all_filtered_ids() == []


def test_scope_is_whatever_the_table_shows(widget):
    # No user filter yet: the catalog defaults are not narrowing, so the caller's own scope
    # already covers it and nobody materialises ids.
    assert widget.scope_ids() is None

    widget._select_rows([SimpleNamespace(id=2), SimpleNamespace(id=5)])
    assert widget.scope_ids() == [2, 5]

    # Clearing the filter from the header hands the whole catalog back.
    widget.table._clear_column_filter("id")
    assert widget.scope_ids() is None


def test_a_tools_own_pushed_filter_is_not_the_user_narrowing_it(widget):
    """Otherwise entering a tool would materialise every id in the catalog on each refresh."""
    widget.push_scope("tool", filters=[FilterSpec("name", FilterOperator.EQ, "m1")])
    assert widget.scope_ids() is None
    assert widget.table.all_filtered_ids() == [2]  # ...but the table really is narrowed

    widget._select_rows([SimpleNamespace(id=2)])
    assert widget.scope_ids() == [2]

    widget.pop_scope("tool")
    widget.table._clear_column_filter("id")
    assert widget.scope_ids() is None


def test_a_borrowed_table_stops_offering_the_catalogs_own_actions(widget):
    """"Import…" belongs to the catalog, not to the tool the table is on loan to."""
    assert widget.table._config_actions_visible
    widget.push_scope("tool", filters=[FilterSpec("name", FilterOperator.EQ, "m1")])
    assert not widget.table._config_actions_visible
    widget.pop_scope("tool")
    assert widget.table._config_actions_visible
