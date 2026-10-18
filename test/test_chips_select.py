"""ChipsSelect: typed values become chips only when editable; chips reorder."""
from __future__ import annotations

import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6

from PySide6.QtWidgets import QApplication

from amdockvs.ui.common.widgets import ChipsSelect


def _app():
    return QApplication.instance() or QApplication(["chips-test"])


def test_typing_adds_a_chip_only_when_editable_and_chips_reorder():
    _app()
    chips = ChipsSelect(editable=True, reorderable=True, normalize=lambda text: text.strip().upper())
    chips.set_items(["ZN", "FE"], selected=["ZN", "FE"])
    chips._combo.lineEdit().setText(" mn ")
    chips._combo.lineEdit().returnPressed.emit()
    assert chips.selected_items() == ["ZN", "FE", "MN"]
    chips.move_item("MN", 0)
    assert chips.selected_items() == ["MN", "ZN", "FE"]
    assert not ChipsSelect()._combo.isEditable()  # chains/ligands: a closed set, nothing typed in
