from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QSpinBox, QWidget

from amdockvs.ui.common.widgets import QueueResources, RunDestinationCombo


def test_a_run_asks_a_queue_only_for_what_differs_from_its_executor(monkeypatch):
    QApplication.instance() or QApplication(["queue-resources-test"])
    hpc = SimpleNamespace(kind="queue", partition="short", account="", nodes=0, cpus=4, gpus=0, memory_gb=0, time="")
    executors = {"hpc": hpc, "local": SimpleNamespace(kind="process")}
    runtime = SimpleNamespace(run_destinations=lambda: [("local", "This computer"), ("hpc", "hpc")], queue_asks={},
                              workspace=SimpleNamespace(settings_manager=SimpleNamespace(
                                  settings=SimpleNamespace(executors=executors))))
    parent = QWidget()
    destination = RunDestinationCombo(parent, runtime)
    button = QueueResources(parent, runtime, destination)
    assert not button.queue() and button.asks() == {}

    def fill(dialog):  # what the user types: 16 CPUs, 12 hours
        [spin.setValue(16) for spin in dialog.findChildren(QSpinBox) if spin.value() == 4]
        [line.setText("12:00:00") for line in dialog.findChildren(QLineEdit) if "e.g." in line.placeholderText()]
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fill)
    destination.setCurrentIndex(destination.findData("hpc"))
    button.click()
    assert button.queue() and button.asks() == {"cpus": 16, "time": "12:00:00"}
    assert runtime.queue_asks == {"hpc": {"cpus": 16, "time": "12:00:00"}}  # what every tool's jobs ask
    assert "changed" in button.text()
    destination.setCurrentIndex(destination.findData("local"))
    assert button.asks() == {}  # only a queue is asked
