"""The Filter tool lists the cuts over a sharded library and unticking one undoes it."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox

from amdockvs.ui.tools.molecules.filter import MoleculeFilterWidget


def test_unticking_a_cut_brings_its_records_back():
    _app = QApplication.instance() or QApplication([])
    calls = []
    molecules = SimpleNamespace(
        sync_set_membership=lambda: None, list_sets=lambda: [], scope_clause=lambda _scope: None,
        shard_exclusions=lambda: [], set_shard_exclusion_active=lambda *args: calls.append(args),
    )
    widget = MoleculeFilterWidget(runtime=SimpleNamespace(molecules=molecules, has_shards=lambda: True))
    widget._show_cuts([{"id": 4, "active": True, "reason": "qsar: top 10", "n": 1500}])
    (box,) = widget.cuts_box.findChildren(QCheckBox)
    assert box.isChecked() and "1,500" in box.text()
    box.setChecked(False)
    assert calls == [(4, False)]
    widget._show_cuts(None)  # a table library: no cuts to show
    assert widget.cuts_box.isHidden()
