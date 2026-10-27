"""Opening/cancelling the drawing never applies an edit; the choice follows Apply."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("drawing, choice", [(None, None), ("edited", None), ("edited", True), ("edited", False)])
def test_edit_choice_follows_apply(monkeypatch, drawing, choice):
    import amdockvs.core.configuration  # noqa: F401 - pydantic before PySide6
    import ms_env

    from amdockvs.ui.viewers import ketcher_editor as editor

    events = []
    jobs = []
    monkeypatch.setattr("amdockvs.ui.common.async_query.run_async", lambda fn, done, **kw: jobs.append((fn, done)))
    monkeypatch.setattr("amdockvs.integrations.registry.get_tool", lambda _name: SimpleNamespace(recipe=lambda _rt: None))
    monkeypatch.setattr(ms_env, "locate", lambda _recipe: (ms_env.READY, "/ketcher/index.html"))

    def draw(*_args):
        assert events == []
        events.append("drawing")
        return drawing

    def dependents(_id):
        assert events == ["drawing"]
        events.append("impact")
        return {"docking results": 2}

    def ask(_parent, _name, impact):
        assert events == ["drawing", "impact"]
        assert impact == {"docking results": 2}
        events.append("choice")
        return choice

    def apply(molecule_id, molblock, *, copy):
        assert events == ["drawing", "impact", "choice"]
        assert (molecule_id, molblock, copy) == (1, "edited", choice)
        events.append("apply")
        return 2 if copy else 1

    monkeypatch.setattr(editor, "_run_editor", draw)
    monkeypatch.setattr(editor, "_ask_copy", ask)
    api = SimpleNamespace(molblock=lambda _id: "original", dependents=dependents, edit=apply)
    completed = []
    editor.edit_molecule(None, SimpleNamespace(molecules=api),
                         [SimpleNamespace(id=1, name="ligand", molecule_type="small_molecule")], done=completed.append)
    assert events == []
    while jobs:
        fn, done = jobs.pop(0)
        done(fn())
    expected = ["drawing"] if drawing is None else ["drawing", "impact", "choice"]
    assert events == expected + (["apply"] if choice is not None else [])
    assert completed == ([] if choice is None else [2 if choice else 1])


@pytest.mark.parametrize("choice", [True, False, None])
def test_apply_offers_copy_even_without_results(monkeypatch, choice):
    from amdockvs.ui.viewers import ketcher_editor as editor

    class Question:
        def __init__(self, *_args, **_kwargs):
            self.buttons = []

        def setInformativeText(self, text):
            assert "conformers" in text and "docking results" in text

        def addButton(self, *_args):
            button = object()
            self.buttons.append(button)
            return button

        def setDefaultButton(self, button):
            assert button is self.buttons[0]

        def exec(self):
            pass

        def clickedButton(self):
            return self.buttons[0 if choice else 1] if choice is not None else self.buttons[2]

    for enum in ("Icon", "ButtonRole", "StandardButton"):
        setattr(Question, enum, getattr(editor.QMessageBox, enum))
    monkeypatch.setattr(editor, "QMessageBox", Question)
    assert editor._ask_copy(None, "ligand", {}) is choice


def test_cannot_apply_when_loading_the_molecule_failed(monkeypatch):
    from amdockvs.ui.viewers import ketcher_editor as editor

    monkeypatch.setattr(editor.QMessageBox, "warning", lambda *_args: None)
    # No button access: Apply must stay disabled when setMolecule rejects.
    editor.KetcherDialog._ready(SimpleNamespace(), {"error": "invalid structure"})


@pytest.mark.parametrize("smiles, changed", [
    ("CCO", False),
    ("OCC", False),
    ("CCN", True),
    ("CC[O-]", True),
    ("CCO[2H]", True),
])
def test_editor_returns_only_chemical_changes(monkeypatch, smiles, changed):
    from pathlib import Path

    from rdkit import Chem
    from rdkit.Chem import AllChem

    from amdockvs.ui.viewers import ketcher_editor as editor

    original = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(original, randomSeed=7)
    # Ketcher can remove/reinsert an H as an implicit H, reorder atoms and redraw in 2D.
    drawing = Chem.MolFromSmiles(smiles)
    AllChem.Compute2DCoords(drawing)
    molblock = Chem.MolToMolBlock(drawing)

    def run(command, **_kwargs):
        Path(command[-1]).write_text(molblock)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(editor.subprocess, "run", run)
    result = editor._run_editor(Path("/ketcher"), Chem.MolToMolBlock(original), "Edit")
    assert result == (molblock if changed else None)
