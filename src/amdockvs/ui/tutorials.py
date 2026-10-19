"""Small, on-demand UI tours; application behavior and slots remain independent.

Keep these bindings with the UI they describe. Large practice datasets can live
in a separate repository without making a basic interface tour an extra package.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from amdockvs.ui.main_window import AMDockVSMainWindow
    from ms_components.ms_tutorial import TutorialController


def start_interface_tour(window: AMDockVSMainWindow) -> TutorialController:
    """Explain the visible interface without opening views or executing actions.

    Content stays in Python for this short tour, so it ships as a regular module
    without a separate content bundle. Nothing is imported at application startup.
    """
    from PySide6.QtWidgets import QMessageBox, QToolButton
    from ms_components.ms_tutorial import Tutorial, TutorialController, TutorialStep, TutorialTarget

    from amdockvs.ui.view_ids import DOCKING_VIEW_ID

    controller = getattr(window, "_interface_tutorial", None)
    if controller is None:
        controller = TutorialController(targets={}, parent=window)
        controller.failed.connect(
            lambda _tutorial_id, message: QMessageBox.warning(window, "Guided tour", message)
        )
        window._interface_tutorial = controller

    with_project = window.runtime.active_context is not None
    workspace_text = (
        "Your open tables and result views appear here. Review the imported molecules, "
        "define binding sites, configure docking, then inspect the results. "
        "This tour keeps your current tabs and data."
        if with_project else
        "Open or create a project to enable the catalogs and tools. A typical workflow is: "
        "import a receptor and ligands, check the input, define a binding site, configure docking, "
        "then inspect the results. This tour does not require a project or sample data."
    )
    sections = (
        ("projects", window.menuBar(), "1. Projects and input data",
         "Use File > Projects to open or create a project. Once a project is open, "
         "File > Import Receptors and Import Ligands load your input files. "
         "This is an informative tour: advance with Next, go Back, or leave with Escape."),
        ("catalog", window.views.catalog_toolbar, "2. Review your data",
         "The Data toolbar opens catalogs such as Molecules, Receptors, Ligands and Binding Sites, "
         "as well as result views. Review the imported data before configuring calculations."),
        ("docking", window.tools.action_buttons.get(DOCKING_VIEW_ID), "3. Configure calculations",
         "Docking Studio opens the docking configuration. Choose the receptor, ligands, "
         "binding site and engine settings before starting a calculation. "
         "The other tools on this side handle filtering, diversity, pocket detection and models."),
        ("workspace", window.central_widget, "Your workspace", workspace_text),
        ("status", window.statusBar(), "Follow execution and results",
         "The bottom bar reports project and execution information as it becomes available. "
         "Use Jobs on the Data toolbar for job details and progress, "
         "then open Docking Results to inspect completed docking calculations."),
        ("appearance", window.findChild(QToolButton, "menuBarTheme"), "Interface theme",
         "Use this button to choose the interface theme. "
         "The theme applies throughout the application."),
        ("help", window.menuBar(), "Help and documentation" if with_project else "Continue inside a project",
         "Use Help > Documentation for the full documentation, or Web Site to visit the project website. "
         "You can repeat this project tour from Help > Guided tour… whenever you need it."
         if with_project else
         "You have completed the welcome tour. A separate project tour is available once a project is open. "
         "Open or create one through File > Projects, then select Help > Guided tour… again. "
         "The project tour explains the catalogs, calculation tools, jobs and results."),
    )
    # Project-only toolbars are hidden on the welcome screen; do not reveal or enable them.
    visible = [section for section in sections if section[1] is not None and section[1].isVisible()]
    controller.targets = {key: TutorialTarget(widget) for key, widget, _title, _text in visible}
    for key, menu_text in (("projects", "&File"), ("help", "&Help")):
        action = next((action for action in window.menuBar().actions() if action.text() == menu_text), None)
        if action is None:  # a renamed menu: the step points at the whole menu bar instead
            continue
        controller.targets[key] = TutorialTarget(
            window.menuBar(), rect=lambda action=action: window.menuBar().actionGeometry(action)
        )
    title = "AMDockVS · Project tour" if with_project else "AMDockVS · Welcome tour"
    tutorial = Tutorial("interface-overview", title, tuple(
        TutorialStep(key, key, title, text) for key, _widget, title, text in visible
    ))
    controller.start(tutorial)
    return controller
