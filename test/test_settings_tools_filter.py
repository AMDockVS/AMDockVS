"""Settings > External tools: grouped by category, filtered by text, narrowed to one tool."""
# Before PySide6: its import hook breaks pydantic's lazy shim while ms_flow is being imported.
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401
from PySide6.QtWidgets import QApplication


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
