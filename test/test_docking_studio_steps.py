"""Docking Studio step logic that runs without a window: step marks and the unprepared filter."""
from types import SimpleNamespace

import amdockvs.runtime  # noqa: F401 - pydantic before PySide6 (see test_diagram_follows_results)
from amdockvs.ui.tools.docking.preparation_panel import PreparationPanel
from amdockvs.ui.tools.docking.run_panel import RunPanel


class _Step:
    """Only set_done: painting a step failed or partial would raise here."""

    def __init__(self):
        self.done = None

    def set_done(self, value=True):
        self.done = value


def test_a_step_is_done_or_not_never_failed_or_partial():
    for prepared, total, done in ((0, 5, False), (3, 5, False), (5, 5, True), (0, 0, False)):
        step = _Step()
        RunPanel._mark_step(step, prepared, total)
        assert step.done is done


def test_a_prepared_selection_leaves_the_step_table_empty():
    panel = PreparationPanel()
    selection = SimpleNamespace(get=lambda role: [1, 2])
    panel.window = lambda: SimpleNamespace(selection=selection)
    panel.force_prepare_ligands = SimpleNamespace(isChecked=lambda: False)
    panel.stepper = SimpleNamespace(current_index=1)
    panel._selected_prep_engines = lambda: ["ad4"]
    panel.runtime = SimpleNamespace(docking=SimpleNamespace(
        unprepared_molecules_clause=lambda role_type, engines: ("unprepared", role_type, tuple(engines))))
    assert panel._unprepared_clause("ligand") == ("unprepared", "ligand", ("ad4",))

