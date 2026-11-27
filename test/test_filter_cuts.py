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


def test_a_strong_cut_offers_to_repack_the_library(monkeypatch):
    """Most of the library left out: Repack is offered (the library as it is stays kept); not when a
    later step works on it, which it says, and not for a light cut."""
    from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

    _app = QApplication.instance() or QApplication([])
    submitted = []
    molecules = SimpleNamespace(sync_set_membership=lambda: None, list_sets=lambda: [],
                                scope_clause=lambda _scope: None, shard_exclusions=lambda: [])
    loader = SimpleNamespace(repack_library=lambda: submitted.append(True) or "job-1")
    widget = MoleculeFilterWidget(runtime=SimpleNamespace(molecules=molecules, has_shards=lambda: True, loader=loader))
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    cut = [{"id": 4, "active": True, "reason": "filter", "n": 750}]

    def repack_button():
        return next((b for b in widget.cuts_box.findChildren(QPushButton) if b.text().startswith("Repack")), None)

    widget._show_cuts(cut, {"left_out": 0.75, "reason": None})
    assert repack_button().isEnabled()
    assert any("75%" in label.text() for label in widget.cuts_box.findChildren(QLabel))
    repack_button().click()
    assert submitted == [True]
    widget._show_cuts(cut, {"left_out": 0.75, "reason": "a later step keeps a state on it (docked k)"})
    assert not repack_button().isEnabled()
    assert any("docked k" in label.text() for label in widget.cuts_box.findChildren(QLabel))
    widget._show_cuts(cut, {"left_out": 0.1, "reason": None})
    assert repack_button() is None
