"""Build links to managed protein tools and notices installs without restarting."""
from types import SimpleNamespace

import pytest
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401 - pydantic before PySide6
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from amdockvs.ui.shell.settings_tools import ExternalToolsPage, tool_events
from amdockvs.ui.tools.molecules import build as build_module
from test_build_ui import build_widget  # noqa: F401 - shared fixture


def _openmm_status(build_widget, monkeypatch, installed: set):
    build_widget.runtime.chemistry.receptor_protonation_tool_status = lambda name: SimpleNamespace(
        installed=name in installed, message=f"{name}: {'ready' if name in installed else 'not installed'}",
        command=None)
    monkeypatch.setattr(build_module, "run_async", lambda work, done, **kwargs: done(work()))


def test_build_routes_the_missing_openmm_env_to_settings_and_updates_after_install(build_widget, monkeypatch):
    installed, opened = set(), []
    _openmm_status(build_widget, monkeypatch, installed)
    build_widget.runtime.molecules.count = lambda _scope: 0
    monkeypatch.setattr(build_widget, "window", lambda: SimpleNamespace(
        open_settings=lambda **kwargs: opened.append(kwargs)))
    build_widget.tabs.setCurrentWidget(build_widget.proteins_tab)
    build_widget.receptor_min_section.setChecked(True)
    build_widget.resize(560, 1000)
    build_widget.show()
    QApplication.processEvents()
    build_widget._refresh_receptor_tools()
    assert build_widget.receptor_fix_settings_button.isVisible()
    assert build_widget.receptor_min_settings_button.isVisible()
    assert build_widget.receptor_fix_status.text() == "openmm: not installed"
    build_widget.receptor_fix_settings_button.click()
    build_widget.receptor_min_settings_button.click()
    assert [entry["tool"] for entry in opened] == ["openmm", "openmm"]
    assert all(entry["node"] == "page:External tools" for entry in opened)

    installed.add("openmm")
    tool_events.installed.emit("openmm")
    assert not build_widget.receptor_fix_settings_button.isVisible()
    assert not build_widget.receptor_min_settings_button.isVisible()
    assert build_widget.receptor_min_status.text() == "openmm: ready"
    build_widget.receptor_protonation_section.setChecked(False)
    assert build_widget._protein_tools_ready(build_widget._protein_ops())
    assert not build_widget.runtime.chemistry.calls  # Detection does not submit work.


@pytest.mark.parametrize("answer", ["Open External tools…", "Cancel"])
def test_run_offers_settings_before_submitting_a_job_with_missing_dependencies(build_widget, monkeypatch, answer):
    opened, seen = [], []
    _openmm_status(build_widget, monkeypatch, set())
    monkeypatch.setattr(build_widget, "window", lambda: SimpleNamespace(
        open_settings=lambda **kwargs: opened.append(kwargs)))
    for section in build_widget._protein_sections():
        section.setChecked(False)
    build_widget.receptor_fix_section.setChecked(True)

    def choose():
        box = QApplication.activeModalWidget()
        seen.append(box.informativeText())
        next(button for button in box.buttons() if button.text().replace("&", "") == answer).click()

    QTimer.singleShot(0, choose)
    build_widget._run_proteins()
    assert seen and "Protein preparation" in seen[0]
    assert not build_widget.runtime.chemistry.calls
    assert bool(opened) is (answer != "Cancel")
    if opened:
        assert opened == [{"node": "page:External tools", "tool": "openmm"}]


def test_settings_lists_the_protein_preparation_tools(build_widget, monkeypatch):
    from amdockvs.integrations.registry import MANAGED_TOOLS, ToolStatus
    from amdockvs.ui.shell import settings_tools

    statuses = [ToolStatus(tool.tool_id, False, f"{tool.label} is not installed.") for tool in MANAGED_TOOLS]
    monkeypatch.setattr(settings_tools, "run_async", lambda work, done, **kwargs: done(statuses))
    page = ExternalToolsPage(build_widget.runtime)
    try:
        page.resize(900, 520)
        page.show()
        page.search.setText("Protein preparation")
        QApplication.processEvents()
        assert {key for key, row in page._rows.items() if row.isVisible()} == {"openmm", "reduce", "pdb2pqr"}
        assert page._rows["openmm"].install_button.isEnabled()
        page._apply([ToolStatus(tool.tool_id, tool.tool_id == "openmm", "ready") for tool in MANAGED_TOOLS])
        assert not page._rows["openmm"].install_button.isEnabled()
    finally:
        page.close()


@pytest.mark.parametrize("method", ["reduce", "pdb2pqr"])
def test_protein_protonation_offers_the_selected_installer_and_notices_install(build_widget, monkeypatch, method):
    installed, opened = set(), []
    build_widget.runtime.molecules.count = lambda _scope: 0
    build_widget.runtime.chemistry.receptor_protonation_tool_status = lambda name: SimpleNamespace(
        installed=name in installed, message=f"{name}: {'ready' if name in installed else 'not installed'}")
    monkeypatch.setattr(build_module, "run_async", lambda work, done, **kwargs: done(work()))
    monkeypatch.setattr(build_widget, "window", lambda: SimpleNamespace(open_settings=lambda **kw: opened.append(kw)))
    build_widget.tabs.setCurrentWidget(build_widget.proteins_tab)
    build_widget.receptor_protonation_method.setCurrentIndex(0 if method == "reduce" else 1)
    build_widget.resize(560, 1000)
    build_widget.show()
    QApplication.processEvents()
    build_widget._refresh_receptor_protonation_tools()
    assert build_widget.receptor_protonation_settings_button.isVisible()
    build_widget.receptor_protonation_settings_button.click()
    assert opened == [{"node": "page:External tools", "tool": method}]
    build_widget.grab().save(f"/tmp/amdockvs-{method}-protonation.png")
    installed.add(method)
    tool_events.installed.emit(method)
    assert not build_widget.receptor_protonation_settings_button.isVisible()
    assert build_widget.receptor_protonation_status.text() == f"{method}: ready"
    assert build_widget.receptor_protonation_method.currentData() == method
    assert not build_widget.runtime.chemistry.calls


@pytest.mark.parametrize("method", ["reduce", "pdb2pqr"])
def test_run_protonation_links_missing_executable_to_settings(build_widget, monkeypatch, method):
    opened = []
    build_widget.runtime.chemistry.receptor_protonation_tool_status = lambda _name: SimpleNamespace(
        installed=False, message="Not installed")
    monkeypatch.setattr(build_module, "run_async", lambda work, done, **kwargs: done(work()))
    monkeypatch.setattr(build_widget, "window", lambda: SimpleNamespace(open_settings=lambda **kw: opened.append(kw)))
    for section in build_widget._protein_sections():
        section.setChecked(False)
    build_widget.receptor_protonation_section.setChecked(True)
    build_widget.receptor_protonation_method.setCurrentIndex(0 if method == "reduce" else 1)

    def choose():
        box = QApplication.activeModalWidget()
        next(button for button in box.buttons() if button.text() == "Open External tools…").click()

    QTimer.singleShot(0, choose)
    build_widget._run_proteins()
    assert opened == [{"node": "page:External tools", "tool": method}]
    assert not build_widget.runtime.chemistry.calls


def test_outdated_protonation_status_does_not_override_the_selected_method(build_widget, monkeypatch):
    pending = []
    build_widget.runtime.chemistry.receptor_protonation_tool_status = lambda name: SimpleNamespace(
        installed=name == "pdb2pqr", message=name)
    monkeypatch.setattr(build_module, "run_async", lambda work, done, **kwargs: pending.append((work, done)))
    build_widget._refresh_receptor_protonation_tools()
    build_widget.receptor_protonation_method.setCurrentIndex(1)
    for work, done in reversed(pending):
        done(work())
    assert build_widget.receptor_protonation_status.text() == "pdb2pqr"


@pytest.mark.parametrize("method", ["reduce", "pdb2pqr"])
def test_run_reuses_the_tool_located_off_the_gui_thread(build_widget, monkeypatch, method):
    """Locating pdb2pqr runs its version command (~300ms): never on the thread that paints.

    The panel already looks the tool up asynchronously, so pressing Run reads that result and
    hands the executable to the submit instead of checking a second time.
    """
    probes = []
    build_widget.runtime.chemistry.receptor_protonation_tool_status = lambda name: (
        probes.append(name) or SimpleNamespace(installed=True, message=f"{name}: ready",
                                               command=f"/managed/bin/{name}")
    )
    monkeypatch.setattr(build_module, "run_async", lambda work, done, **kwargs: done(work()))
    for section in build_widget._protein_sections():
        section.setChecked(False)
    build_widget.receptor_protonation_section.setChecked(True)
    build_widget.receptor_protonation_method.setCurrentIndex(0 if method == "reduce" else 1)
    build_widget._refresh_receptor_protonation_tools()  # what being shown does, off-thread
    assert probes and set(probes) == {method}
    probes.clear()

    build_widget._run_proteins()

    assert probes == []  # the click located nothing: it used what was already found
    submitted = dict(build_widget.runtime.chemistry.calls[-1][1])
    assert submitted["method"] == method
    assert submitted["tool_command"] == f"/managed/bin/{method}"


def test_a_click_that_beats_the_first_lookup_still_checks(build_widget, monkeypatch):
    """Correctness first: with nothing cached yet, Run falls back to the blocking check."""
    probes = []
    build_widget.runtime.chemistry.receptor_protonation_tool_status = lambda name: (
        probes.append(name) or SimpleNamespace(installed=False, message="Not installed", command=None)
    )
    monkeypatch.setattr(build_module, "run_async", lambda work, done, **kwargs: None)  # never lands
    monkeypatch.setattr(build_widget, "window", lambda: SimpleNamespace(open_settings=lambda **kw: None))
    for section in build_widget._protein_sections():
        section.setChecked(False)
    build_widget.receptor_protonation_section.setChecked(True)
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())

    build_widget._run_proteins()

    assert probes == ["reduce"]
    assert not build_widget.runtime.chemistry.calls  # not installed: nothing submitted
