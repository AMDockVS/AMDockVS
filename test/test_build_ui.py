import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Import the runtime before PySide6; shiboken's feature loader otherwise trips over
# pydantic's lazy migration shim while ms_flow is being imported.
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QGroupBox, QPushButton

from amdockvs.ui.catalog.molecules import MoleculeWidget
from amdockvs.ui.tools.molecules.build import MoleculeBuildWidget
from amdockvs.vocab import MoleculeType


class _FakeMolecules:
    def __init__(self):
        self.selections = []

    def select(self, *, molecule_type):
        selection = SimpleNamespace(molecule_type=molecule_type, filters={})
        self.selections.append(selection)
        return selection

    @staticmethod
    def filter(scope, *, filters):
        return SimpleNamespace(
            molecule_type=scope.molecule_type,
            filters={**scope.filters, **filters},
        )


class _FakeChemistry:
    def __init__(self):
        self.calls = []

    def protonate_ligands(self, **config):
        self.calls.append(("protonate_ligands", config))
        return "small-job"

    def protonate_receptors(self, **config):
        self.calls.append(("protonate_receptors", config))
        return "protein-job"


@pytest.fixture
def build_widget():
    app = QApplication.instance() or QApplication(["amdockvs-build-test"])
    runtime = SimpleNamespace(molecules=_FakeMolecules(), chemistry=_FakeChemistry())
    widget = MoleculeBuildWidget(runtime=runtime)
    yield widget
    widget.close()
    app.processEvents()


def _group_titles(page):
    return [box.title() for box in page.findChildren(QGroupBox)]


def test_build_uses_molecule_type_tabs_and_requested_groups(build_widget):
    assert build_widget.tabs.count() == 2
    assert [build_widget.tabs.tabText(index) for index in range(2)] == [
        "Small molecules",
        "Proteins",
    ]
    assert _group_titles(build_widget.small_molecules_tab) == [
        "Protonation",
        "3D Generation",
        "Conformers",
        "Minimization",
    ]
    assert _group_titles(build_widget.proteins_tab) == [
        "Protonation",
        "Fix Structure",
        "3D Generation",
        "Minimization",
    ]
    # 3D Generation (ESMFold) is a placeholder until a predictor is wired to a job
    assert not build_widget.receptor_predict_box.isEnabled()

    button_texts = [button.text() for button in build_widget.findChildren(QPushButton)]
    assert "Refresh" not in button_texts
    assert "Open Jobs" not in button_texts
    assert not any("Ligand" in text or "Receptor" in text for text in button_texts)
    assert [
        build_widget.ligand_protonation_method.itemText(index)
        for index in range(build_widget.ligand_protonation_method.count())
    ] == ["Dimorphite-DL", "OpenBabel", "pKasso", "Polar Hs"]
    assert build_widget._active_protonation_method() == "dimorphite"


def test_small_molecule_protonation_method_switches_compact_form(build_widget):
    build_widget.ligand_protonation_method.setCurrentIndex(3)

    assert build_widget._active_protonation_method() == "polar_hydrogens"
    assert build_widget.run_protonate_ligands_button.text() == "Add polar Hs"
    assert build_widget._cfg_protonate_ligands()["method"] == "polar_hydrogens"
    assert build_widget.ligand_protonation_pages["polar_hydrogens"].isVisibleTo(
        build_widget.ligand_protonation_box
    )


def test_build_actions_are_right_aligned_and_tabs_fix_operation_scope(build_widget):
    run_buttons = (
        build_widget.run_protonate_ligands_button,
        build_widget.run_generate_3d_button,
        build_widget.run_conformers_button,
        build_widget.run_ligand_minimize_button,
        build_widget.run_protonate_receptors_button,
        build_widget.run_fix_receptors_button,
        build_widget.run_receptor_minimize_button,
    )
    for button in run_buttons:
        action_layout = button.parentWidget().layout()
        assert action_layout.itemAt(0).spacerItem() is not None
        assert action_layout.itemAt(action_layout.count() - 1).widget() is button

    build_widget.tabs.setCurrentWidget(build_widget.proteins_tab)
    assert build_widget._selected_molecule_type() == MoleculeType.PROTEIN
    assert build_widget._cfg_protonate_ligands()["ligands"].molecule_type == MoleculeType.SMALL_MOLECULE
    assert build_widget._cfg_protonate_receptors()["receptors"].molecule_type == MoleculeType.PROTEIN


def test_each_build_tab_keeps_its_own_scope(build_widget):
    assert build_widget.small_molecule_scope_combo.currentData() == "all"
    assert build_widget.protein_scope_combo.currentData() == "all"

    build_widget.small_molecule_scope_combo.setCurrentIndex(1)
    build_widget._on_molecule_selection_changed([
        SimpleNamespace(id=12, molecule_type=MoleculeType.SMALL_MOLECULE),
        SimpleNamespace(id=31, molecule_type=MoleculeType.PROTEIN),
    ])

    small_scope = build_widget._scope(MoleculeType.SMALL_MOLECULE)
    protein_scope = build_widget._scope(MoleculeType.PROTEIN)
    assert small_scope.filters == {"id__in": [12]}
    assert protein_scope.filters == {}


def test_only_fix_structure_can_go_back_to_the_imported_structure(build_widget):
    # Inside a tool the Molecules table shows the working structure; only the op that
    # explicitly needs the untouched import says otherwise.
    assert build_widget.receptor_fix_structure.currentData() == "current"
    build_widget.receptor_fix_structure.setCurrentIndex(1)
    assert build_widget._cfg_fix_receptors()["structure_source"] == "original"

    for cfg in (
        build_widget._cfg_protonate_ligands(),
        build_widget._cfg_generate_3d_ligands(),
        build_widget._cfg_generate_ligand_conformers(),
        build_widget._cfg_minimize_ligands(),
        build_widget._cfg_protonate_receptors(),
        build_widget._cfg_minimize_receptors(),
    ):
        assert "structure_source" not in cfg


def test_filtered_build_scope_uses_all_matching_table_rows(build_widget):
    build_widget._bound_molecules_table = SimpleNamespace(all_filtered_ids=lambda: [4, 9])
    build_widget.protein_scope_combo.setCurrentIndex(2)

    scope = build_widget._scope(MoleculeType.PROTEIN)

    assert scope.filters == {"id__in": [4, 9]}


def test_build_protonation_buttons_use_chemistry_api(build_widget, monkeypatch):
    monkeypatch.setattr(build_widget, "_submit", lambda _title, callback: callback())
    monkeypatch.setattr(build_widget, "_require_executable", lambda **_kwargs: True)

    build_widget.run_protonate_ligands_button.click()
    build_widget.run_protonate_receptors_button.click()

    ligand_call, protein_call = build_widget.runtime.chemistry.calls
    assert ligand_call[0] == "protonate_ligands"
    assert ligand_call[1]["ligands"].molecule_type == MoleculeType.SMALL_MOLECULE
    assert protein_call[0] == "protonate_receptors"
    assert protein_call[1]["receptors"].molecule_type == MoleculeType.PROTEIN
    assert protein_call[1]["method"] == "reduce"


def test_pdb2pqr_enables_its_specific_protein_options(build_widget):
    assert not build_widget.receptor_protonation_ph.isEnabled()
    assert not build_widget.receptor_protonation_forcefield.isEnabled()

    build_widget.receptor_protonation_method.setCurrentIndex(1)

    assert build_widget.receptor_protonation_ph.isEnabled()
    assert build_widget.receptor_protonation_forcefield.isEnabled()


def test_molecules_structure_source_lives_and_dies_with_tool_scope(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication(["amdockvs-molecule-source-test"])
    runtime = AMDockVSRuntime()
    widget = None
    try:
        runtime.create_project(name="source_scope", folder=tmp_path / "source_scope")
        widget = MoleculeWidget(runtime=runtime)
        reloads = []
        monkeypatch.setattr(widget, "_reload_selected_structure", lambda: reloads.append(True))

        widget.push_scope("build", structure_source="current")
        assert widget._structure_sources == {"build": "current"}

        widget.pop_scope("build")
        assert widget._structure_sources == {}
        assert reloads == [True, True]
    finally:
        if widget is not None:
            widget.close()
        runtime.shutdown()
        app.processEvents()
