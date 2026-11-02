"""Settings > External tools: grouped by category, filtered by text, narrowed to one tool."""
import pytest

# Before PySide6: its import hook breaks pydantic's lazy shim while ms_flow is being imported.
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401
from PySide6.QtWidgets import QApplication
import shiboken6


def test_tools_are_grouped_filtered_and_narrowed_to_one(monkeypatch):
    from amdockvs.ui.shell import settings_tools

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(settings_tools, "run_async", lambda *_a, **_kw: None)  # no status check
    page = settings_tools.ExternalToolsPage(runtime=None)
    shown = lambda: {tool_id for tool_id, row in page._rows.items() if not row.isHidden()}

    assert {"Docking engines", "Binding sites", "Editors"} <= set(page._headings)
    page.search.setText("docking engines")
    assert shown() == {"vina", "qvina", "adgpu", "gnina", "unidock"}
    assert page._headings["Editors"].isHidden() and not page._headings["Docking engines"].isHidden()

    page.show_only("vina")  # "Vina" is in "QuickVina 2" too: only the one asked for stays
    assert shown() == {"vina"} and page.search.text() == "AutoDock Vina"
    page.show_only(None)
    assert shown() == set(page._rows)


@pytest.mark.parametrize("failed", [False, True])
def test_status_reply_after_closing_settings_is_ignored(monkeypatch, failed):
    from amdockvs.ui.shell import settings_tools

    app = QApplication.instance() or QApplication([])
    callbacks = {}
    monkeypatch.setattr(
        settings_tools, "run_async",
        lambda work, done, on_error=None, **kwargs: callbacks.update(done=done, error=on_error),
    )
    page = settings_tools.ExternalToolsPage(runtime=None)
    if failed:
        reply = lambda: callbacks["error"](RuntimeError("status check failed"))
        reply()
        assert "status check failed" in next(iter(page._rows.values())).status_label.text()
    else:
        reply = lambda: callbacks["done"]([])
        reply()
        assert not page.install_all_button.isEnabled()

    shiboken6.delete(page)
    reply()
    app.processEvents()


def test_a_removal_stopped_by_a_lock_left_behind_unlocks_it_when_told(monkeypatch, tmp_path):
    import json

    from ms_env import prefix_lock
    from PySide6.QtWidgets import QMessageBox

    from amdockvs.ui.shell import settings_tools

    QApplication.instance() or QApplication([])
    lock = tmp_path / "vina.ms-env-lock"
    lock.mkdir()
    (lock / "owner.json").write_text(json.dumps({"client": "elsewhere", "pid": 7, "since": "today"}))
    def run_async(work, done, on_error=None, **_):
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - as run_async hands it over
            return on_error(exc)
        done(result)

    def uninstall(_runtime, _tool_id):
        with prefix_lock(str(tmp_path / "vina")):
            return "Removed"

    asked = []
    monkeypatch.setattr(settings_tools, "run_async", run_async)
    monkeypatch.setattr(settings_tools, "uninstall_tool", uninstall)
    monkeypatch.setattr(QMessageBox, "question", lambda _parent, title, *_: (
        asked.append(title), QMessageBox.StandardButton.Yes)[1])
    page = settings_tools.ExternalToolsPage(runtime=None)
    monkeypatch.setattr(page, "refresh", lambda: None)
    page.remove("vina")
    assert asked == ["Remove AutoDock Vina", "Environment locked"] and not lock.exists()
    assert page._rows["vina"].status_label.text() == "Removed"
