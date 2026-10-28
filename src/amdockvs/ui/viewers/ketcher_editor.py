"""Edit a small molecule in Ketcher; after Apply, choose to replace it or create a copy.

Ketcher is a web app installed from Settings > External tools; ms_components.ketcher runs it
in its own process."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QMessageBox

_NOTE = (
    "Apply lets you create a copy or replace this molecule and delete its associated data. "
    "Nothing in the project changes until you confirm. Cancel discards the drawing."
)


def edit_molecule(parent, runtime, objects, *, done: Callable[[int], None]) -> None:
    """The "Edit…" row action: open the first of `objects` in Ketcher; `done(id)` after a change,
    with the id of the molecule holding the edit (a new one when the user edits a copy)."""
    from amdockvs.core.vocab import MoleculeType
    from amdockvs.ui.common.async_query import run_async

    molecule = next(iter(objects or ()), None)
    if molecule is None:
        return
    if str(getattr(molecule, "molecule_type", "") or "") != MoleculeType.SMALL_MOLECULE:
        QMessageBox.information(parent, "Edit", "Only small molecules can be edited.")
        return
    molecule_id, name = int(molecule.id), str(getattr(molecule, "name", "") or f"Molecule {molecule.id}")

    def look_up():
        from ms_env import locate

        from amdockvs.integrations.registry import get_tool

        return locate(get_tool("ketcher").recipe(runtime)), runtime.molecules.molblock(molecule_id)

    def failed(exc) -> None:
        QMessageBox.critical(parent, "Edit", f"Could not apply the edit:\n{exc}")

    def drawn(molblock: str | None) -> None:
        if molblock is None:
            return

        def confirm(dependents) -> None:
            copy = _ask_copy(parent, name, dependents)
            if copy is None:
                return
            run_async(
                lambda: runtime.molecules.edit(molecule_id, molblock, copy=copy),
                lambda edited_id: done(edited_id) if edited_id is not None else None,
                on_error=failed,
                busy=parent,
            )

        # Read the impact after Apply: results may have been added while the editor was open.
        run_async(lambda: runtime.molecules.dependents(molecule_id), confirm, on_error=failed, busy=parent)

    def opened(found) -> None:
        from ms_env import READY

        (state, index), molblock = found
        if state != READY:
            answer = QMessageBox.question(
                parent, "Edit", "The molecule editor (Ketcher) is not installed.\n\nOpen Settings > External tools to install it?"
            )
            opener = getattr(parent.window(), "open_settings", None)
            if answer == QMessageBox.StandardButton.Yes and callable(opener):
                opener(node="page:External tools", tool="ketcher")
            return

        run_async(
            lambda: _run_editor(Path(index).parent, molblock, f"Edit {name}"),
            drawn,
            on_error=failed,
            busy=parent,
        )

    run_async(look_up, opened, on_error=lambda exc: QMessageBox.critical(parent, "Edit", str(exc)), busy=parent)


def _ask_copy(parent, name: str, dependents: dict[str, int]) -> bool | None:
    """An edit makes another compound: what was measured or computed on this one does not carry
    over. True = edit a copy, False = replace this one, None = cancel."""
    lost = ", ".join(f"{count} {label}" for label, count in dependents.items())
    box = QMessageBox(QMessageBox.Icon.Question, "Apply edit", f"Apply the edited structure of {name}?", parent=parent)
    box.setInformativeText(
        "Create a copy to keep the original molecule and all its data. Replacing deletes its conformers, "
        "preparations, descriptors, fingerprints, docking results and other associated data; "
        "the imported original structure is kept."
        + (f"\n\nAssociated data: {lost}." if lost else "")
    )
    copy = box.addButton("Create a copy", QMessageBox.ButtonRole.AcceptRole)
    replace = box.addButton("Replace and delete associated data", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(copy)
    box.exec()
    if box.clickedButton() is copy:
        return True
    if box.clickedButton() is replace:
        return False
    return None


def _run_editor(folder: Path, molblock: str, title: str) -> str | None:
    """The drawing, or None when cancelled or chemically unchanged."""
    from rdkit import Chem

    from amdockvs.molecules.editing import changed_molecule
    from amdockvs.ui.common.theme import saved_base_font_pt, saved_theme_name
    from ms_components.ketcher import run_editor

    drawn = run_editor(folder, molblock, title, note=_NOTE, theme=saved_theme_name(),
                       base_font_pt=saved_base_font_pt())
    if drawn is None:
        return None
    original = Chem.MolFromMolBlock(molblock, removeHs=False)
    return drawn if changed_molecule(drawn, original) is not None else None


__all__ = ["edit_molecule"]
