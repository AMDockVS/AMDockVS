"""Docking Studio step logic that runs without a window: step marks, the unprepared filter,
and the redocking pair rule."""
from types import SimpleNamespace

import amdockvs.runtime  # noqa: F401 - pydantic before PySide6 (see test_diagram_follows_results)
from amdockvs.ui.tools.docking.preparation_panel import PreparationPanel
from amdockvs.ui.tools.docking.run_panel import RunPanel
from amdockvs.ui.tools.docking.scope_panel import ScopePanel


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


def test_redocking_picked_ligands_bring_exactly_their_receptors():
    pairs = [SimpleNamespace(ligand_molecule_id=lig, receptor_molecule_id=rec)
             for lig, rec in [(1, 20), (2, 20), (3, 21), (4, 22)]]
    panel = ScopePanel()
    panel.runtime = SimpleNamespace(
        mode="vs", complexes=SimpleNamespace(list=lambda purpose: list(pairs)))
    picked: list[int] = [2, 3]
    panel._catalog_ligand_widget = lambda: SimpleNamespace(scope_ids=lambda: picked)
    panel._catalog_receptor_widget = lambda: SimpleNamespace(scope_ids=lambda: [22])
    panel._run_kind = lambda: "redocking"
    assert panel._selected_receptor_ids == [20, 21]  # the receptor table's own pick gives way
    picked = [9]
    assert panel._selected_receptor_ids == [0]  # no pair: no receptor, not every receptor
    panel._run_kind = lambda: "docking"
    assert panel._selected_receptor_ids == [22]
