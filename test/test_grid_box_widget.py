from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QHeaderView

from amdockvs.models import BindingSite, MoleculeRecord
from amdockvs.ui.tools.binding_sites.box import (
    ACTIVE_SITE_ROLE,
    SITE_COLOR_ROLE,
    GridBoxSettingDockWidget,
)


class _BindingSitesApi:
    def __init__(self, sites: list[BindingSite]):
        self._sites = sites

    def list_sites(self, *, molecule_id: int) -> list[BindingSite]:
        assert molecule_id == 4
        return list(self._sites)


@pytest.fixture
def grid_box_widget():
    app = QApplication.instance() or QApplication(["grid-box-test"])
    sites = [
        BindingSite(
            id=1,
            molecule_id=4,
            name="RBQ site",
            source="ligand",
            center_x=1.0,
            center_y=2.0,
            center_z=3.0,
            size_x=20.0,
            size_y=20.0,
            size_z=20.0,
        ),
        BindingSite(
            id=2,
            molecule_id=4,
            name="P2Rank pocket",
            source="p2rank",
            center_x=4.0,
            center_y=5.0,
            center_z=6.0,
            size_x=22.0,
            size_y=22.0,
            size_z=22.0,
        ),
    ]
    widget = GridBoxSettingDockWidget(
        runtime=SimpleNamespace(binding_sites=_BindingSitesApi(sites)),
    )
    widget.set_molecule(
        MoleculeRecord(
            id=4,
            name="4UWG",
            is_receptor=True,
            active_binding_site_id=2,
        )
    )
    app.processEvents()
    try:
        yield widget
    finally:
        widget.close()


def test_grid_box_site_list_uses_compact_indicator_and_two_data_columns(grid_box_widget):
    tree = grid_box_widget.site_tree

    assert tree.columnCount() == 3
    assert [tree.headerItem().text(column) for column in range(3)] == ["", "Name", "Source"]
    assert tree.header().stretchLastSection() is False
    assert tree.columnWidth(0) == 15
    assert tree.header().sectionResizeMode(0) == QHeaderView.Fixed
    assert tree.header().sectionResizeMode(1) == QHeaderView.Stretch
    assert tree.header().sectionResizeMode(2) == QHeaderView.ResizeToContents


def test_grid_box_name_cell_carries_color_and_active_state(grid_box_widget):
    tree = grid_box_widget.site_tree
    saved = tree.topLevelItem(0)
    active = tree.topLevelItem(1)

    assert not saved.text(1).startswith("*")
    assert saved.background(1).color().isValid()
    assert saved.data(1, SITE_COLOR_ROLE) == saved.background(1).color().name()
    assert saved.data(0, ACTIVE_SITE_ROLE) is False
    assert active.data(0, ACTIVE_SITE_ROLE) is True
    assert not active.icon(0).isNull()
    assert active.toolTip(0) == "Active docking site"


def test_grid_box_unsaved_working_box_uses_asterisk_and_tooltip(grid_box_widget):
    grid_box_widget._working.dirty = True
    grid_box_widget._working.source_site_id = 2
    grid_box_widget._ensure_working_item_present(select_if_created=False)

    working = grid_box_widget.site_tree.topLevelItem(2)
    assert working.text(0) == ""
    assert working.text(1) == "* Working Box"
    assert working.text(2) == "manual"
    assert "temporary from #2" in working.toolTip(1)

    grid_box_widget._working.dirty = False
    grid_box_widget._ensure_working_item_present(select_if_created=False)
    assert grid_box_widget.site_tree.topLevelItemCount() == 2


def test_grid_box_receptor_row_exposes_legend(grid_box_widget):
    tooltip = grid_box_widget.legend_icon.toolTip()

    assert grid_box_widget.receptor_label.text() == "Receptor: 4UWG"
    assert not isinstance(grid_box_widget.legend_icon, QAbstractButton)
    assert grid_box_widget.legend_icon.accessibleName() == "Grid box legend"
    assert "Unsaved working box" in tooltip
    assert "Colored name" in tooltip
    assert "Active docking site" in tooltip


def test_grid_box_actions_fit_one_row_and_save_without_a_site_saves_as_new(grid_box_widget, monkeypatch):
    """New ▾, Save ▾ and Set Active replace the old eight buttons; the variants live in the menus."""
    widget = grid_box_widget
    assert [a.text() for a in widget.new_button.menu().actions()] == [
        "From active site", "From ligand", "From selection…"]
    assert [a.text() for a in widget.save_button.menu().actions()] == ["Save as new…"]
    saved_as_new = []
    monkeypatch.setattr(widget, "_save_new_site", lambda: saved_as_new.append(True))
    widget.site_tree.clearSelection()
    widget.site_tree.setCurrentItem(None)
    widget._working.source_site_id = None
    assert widget.save_button.isEnabled()
    widget.save_button.click()
    assert saved_as_new == [True]


def test_set_active_is_off_for_the_site_that_is_already_active(grid_box_widget):
    widget = grid_box_widget
    item = lambda site_id: next(widget.site_tree.topLevelItem(r) for r in range(widget.site_tree.topLevelItemCount())
                                if widget.site_tree.topLevelItem(r).data(0, Qt.UserRole) == ("site", site_id))
    widget.site_tree.setCurrentItem(item(2))  # the active one
    assert not widget.activate_button.isEnabled()
    widget.site_tree.setCurrentItem(item(1))
    assert widget.activate_button.isEnabled()


def test_a_new_working_box_can_be_discarded_back_to_the_active_site(grid_box_widget):
    from PySide6.QtTest import QTest

    widget = grid_box_widget
    rows = lambda: [widget.site_tree.topLevelItem(r).data(0, Qt.UserRole) for r in range(widget.site_tree.topLevelItemCount())]
    widget._new_working_box()
    assert ("working", None) in rows()
    widget.show()
    widget.activateWindow()
    widget.site_tree.setFocus()
    QTest.qWaitForWindowActive(widget)
    QTest.keyClick(widget.site_tree, Qt.Key.Key_Delete)
    assert ("working", None) not in rows() and not widget._working.dirty
    assert (widget.cx.value(), widget.cy.value(), widget.cz.value()) == (4.0, 5.0, 6.0)  # the active site again
