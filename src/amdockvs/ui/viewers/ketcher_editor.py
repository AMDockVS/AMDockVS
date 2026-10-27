"""Edit a small molecule in Ketcher; after Apply, choose to replace it or create a copy.

Ketcher is a web app installed from Settings > External tools and run from its folder in a
QWebEngineView. Its JS API answers with Promises, which runJavaScript cannot await: the settled
value is parked in a window variable and polled from here.

The editor runs in its own process (`python -m amdockvs.ui.viewers.ketcher_editor`): in the app's,
PySide 6.10 segfaults as soon as the pointer hovers a QWebEngineView while any Python
application-wide event filter is installed — and the app has several (Shift+wheel, themed icons,
settings search). It also keeps Chromium's memory and GPU use away from PyMOL's."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QCoreApplication, Qt, QTimer, QUrl, QUrlQuery
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QMessageBox, QVBoxLayout

# Resolves once Ketcher has built `window.ketcher`, some time after the page itself loads.
_READY = "new Promise(r => { const t = setInterval(() => { if (window.ketcher) { clearInterval(t); r(true); } }, 100); })"

# Ketcher 3.18 standalone's URL options configure the editor before it mounts.
_HIDDEN_CONTROLS = (
    "open", "save", "arrows", "reaction-plus", "reaction-mapping-tools", "reaction-map", "reaction-unmap",
    "rgroup", "rgroup-label", "rgroup-fragment", "rgroup-attpoints", "sgroup", "create-monomer",
    "shapes", "shape-ellipse", "shape-rectangle", "shape-line", "text", "images",
    "recognize", "analyse", "miew", "settings", "help", "about", "fullscreen",
)


class KetcherDialog(QDialog):
    """Ketcher opened on `molblock`; after exec() == Accepted, `molblock` holds the drawing."""

    def __init__(self, folder: Path, molblock: str, *, title: str, parent=None):
        from PySide6.QtWebEngineWidgets import QWebEngineView  # heavy: only when someone edits

        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1100, 750)
        self.molblock = molblock
        self._done: Callable[[dict], None] | None = None
        self._poll = QTimer(self, interval=100)
        # As JSON: a JS object comes back to Python empty, a string intact.
        self._poll.timeout.connect(
            lambda: self._view.page().runJavaScript("JSON.stringify(window.__amdock ?? null)", 0, self._polled)
        )

        self._view = QWebEngineView(self)
        self._view.loadFinished.connect(self._loaded)
        url = QUrl.fromLocalFile(str(folder / "index.html"))
        query = QUrlQuery()
        query.addQueryItem("hiddenControls", ",".join(_HIDDEN_CONTROLS))
        query.addQueryItem("disableMacromoleculesEditor", "true")  # initialize without waiting for macromolecules
        url.setQuery(query)
        self._view.load(url)
        note = QLabel(
            "Apply lets you create a copy or replace this molecule and delete its associated data. "
            "Nothing in the project changes until you confirm. Cancel discards the drawing.",
            self,
        )
        note.setWordWrap(True)
        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel, self)
        self._buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        self._buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled(False)
        self._buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self._view, 1)
        layout.addWidget(note)
        layout.addWidget(self._buttons)

    def _call(self, promise: str, done: Callable[[dict], None]) -> None:
        """Evaluate a JS Promise; `done({"ok": value} | {"error": text})` once it settles."""
        self._done = done
        self._view.page().runJavaScript(
            "window.__amdock = undefined;"
            f"Promise.resolve({promise}).then(v => window.__amdock = {{ok: v ?? ''}},"
            " e => window.__amdock = {error: String(e)});"
        )
        self._poll.start()

    def _polled(self, value) -> None:
        result = json.loads(value or "null")
        if not result or self._done is None:
            return
        self._poll.stop()
        done, self._done = self._done, None
        done(result)

    def _loaded(self, ok: bool) -> None:
        if not ok:
            QMessageBox.critical(self, "Ketcher", "Ketcher could not be loaded.")
            return
        self._call(f"{_READY}.then(() => window.ketcher.setMolecule({json.dumps(self.molblock)}))", self._ready)

    def _ready(self, result: dict) -> None:
        if "error" in result:
            QMessageBox.warning(self, "Ketcher", f"Ketcher could not open the molecule:\n{result['error']}")
            return
        self._buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled(True)

    def _apply(self) -> None:
        self._buttons.setEnabled(False)
        self._call("window.ketcher.getMolfile()", self._got)

    def _got(self, result: dict) -> None:
        self._buttons.setEnabled(True)
        if "error" in result:
            QMessageBox.critical(self, "Ketcher", f"Could not read the drawing:\n{result['error']}")
            return
        self.molblock = str(result.get("ok") or "")
        self.accept()


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
                opener(node="page:External tools")
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
    """Open Ketcher in a child process; the drawing, or None when cancelled or chemically unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "edited.mol"
        proc = subprocess.run(
            [sys.executable, "-m", __name__, str(folder), title, str(out)],
            input=molblock, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"The editor stopped (exit code {proc.returncode}):\n{proc.stderr[-2000:]}")
        if not out.exists():
            return None
        from rdkit import Chem

        from amdockvs.molecules.editing import changed_molecule

        drawn = out.read_text()
        original = Chem.MolFromMolBlock(molblock, removeHs=False)
        return drawn if changed_molecule(drawn, original) is not None else None


def _main(folder: str, title: str, out: str) -> int:
    """The child process: molblock on stdin; the drawing is written to `out` only on Apply."""
    from amdockvs.ui.common.theme import apply_theme, saved_base_font_pt, saved_theme_name

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)  # QtWebEngine wants it before the app
    app = QApplication(["amdockvs-ketcher"])
    apply_theme(saved_theme_name(), app, base_font_pt=saved_base_font_pt())
    dialog = KetcherDialog(Path(folder), sys.stdin.read(), title=title)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        Path(out).write_text(dialog.molblock)
    return 0


__all__ = ["KetcherDialog", "edit_molecule"]

if __name__ == "__main__":
    sys.exit(_main(*sys.argv[1:4]))
