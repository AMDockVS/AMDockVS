# Examples

The examples use the current public API:

```python
from amdockvs import AMDockVSRuntime
```

Examples based on the old `amdockvs.api.AMDockRuntime` API are no longer maintained.

- `pipeline_demo.py`: a short import, preparation, and docking workflow
- `runtime_quickstart.py`: a more explicit example of submitting and waiting for jobs

## Guided tutorial in PySide6

`guided_tutorial_demo.py` is a standalone app. It highlights controls, dims the rest of
the window, lets the user interact with the highlighted control, and advances when the
action is complete. It includes Back, Exit/Esc, and restart controls, and recalculates
positions when the window is resized. It uses the shared `ms_components.ms_tutorial`
component and includes two tours: an informational tour (Next/Back only, with no actions)
and an interactive tour (waits for signals). The test application's slots do not call the
tutorial directly.

Run it from the repository root:

```bash
conda activate MSDD_312
PYTHONPATH="../ms_components/src:../ms_flow/src:../ms_env/src${PYTHONPATH:+:$PYTHONPATH}" \
  python examples/guided_tutorial_demo.py
```

These paths point to the current checkouts (`ms_components` and `ms_flow`), previously
named `molsuite_components` and `molsuite_flow`. If these packages are already installed
in the environment, `PYTHONPATH` is not needed.

The demo uses the real **`ProjectsMenuWidget`** and **`ProjectCatalogBackend`** from the
MolSuite API. It creates a temporary catalog and project under its own `MOLSUITE_HOME`,
which are removed during normal shutdown. It does not use the personal project catalog.
Double-click the practice project to open an illustrative work screen. “Load sample data”
and “Validate example” only change the visual state: they do not import molecules, submit
jobs, or run docking. Project creation is disabled so the tour stays within one window.

Test interaction with Qt and the real project catalog:

```bash
QT_QPA_PLATFORM=offscreen \
  PYTHONPATH="../ms_components/src:../ms_flow/src:../ms_env/src${PYTHONPATH:+:$PYTHONPATH}" \
  python -m pytest -q examples/test_guided_tutorial_demo.py
```

### Feasibility in AMDockVS and ms_projects

This can be implemented with Qt Widgets and does not require an additional tutorial
dependency. `TutorialStep` describes the target, text, and an optional advance event.
`TutorialTarget` binds the target to a widget or resolver and an optional UI navigation
function. `TutorialController` observes existing signals from outside the slots.
Informational steps block interaction with the target; interactive steps allow real clicks.
The controller does not rely on fixed positions or on finding controls by their text.
Content can be written in Python or TOML; widget and signal bindings are defined in Python.
The usage guide is in `../ms_components/docs/ms_tutorial.md`, which also includes a separate
demo of both modes and a tour loaded from TOML.

The proposed integration, checked against the current code:

- **Shared component:** `ms_components.ms_tutorial` already provides an application-agnostic
  controller. Each application registers its targets, events, and steps.
- **Projects:** `src/amdockvs/ui/shell/projects.py` already creates `ProjectsMenuWidget` and
  connects `project_requested`. Tours can be chained with this public signal and
  `project_created`. The demo exercises the first link.
- **Dialog and main window:** AMDockVS hosts the browser in a `QDialog` that is hidden when a
  project opens. It needs a per-window layer and must transfer the current step when the
  window changes. `ms_tutorial` already supports host changes and tests them with a modal
  dialog; this AMDock demo still embeds the browser to test the connection in a small setup.
  The tour still needs to be registered in the scientific application.
- **Dynamic screens:** `ToolCoordinator.open_tool` and
  `MainContentWidget.open_or_focus_view` can show a screen before its target is resolved.
  `TutorialTarget` can register that navigation and resolve the widget when the step begins,
  since docks and dialogs may be created or destroyed later.
- **Chemistry actions:** use the existing AMDockVS/MolSuite APIs and verify their results.
  `monitor_bridge.job_finished` already publishes the job ID and status. The tour can listen
  through `TutorialEvent` and verify the expected job and its success without changing
  `JobFeedbackController.on_job_finished`; submitting a job alone does not complete the step.
- **Stable targets:** `title_label` and `project_tree.view` currently exist as attributes,
  but `ms_projects` should expose a public method for tutorial targets to avoid depending on
  the component's internal structure.
- **Progress:** save the tour version and ID in UI preferences; offer “Help > Tutorials” and
  a separate practice project. Do not modify the user's scientific project to demonstrate
  the workflow.

Recommended first integration: open or create a project, import a receptor and a few sample
ligands, and verify the import. Add preparation and docking after tours can transfer between
windows. Switching projects while one is open starts another process; continuing the tour
there would require saving and restoring progress in the application. Floating docks,
screen-reader focus and accessibility, HiDPI scaling, and the PyMOL viewer still need testing.
The demo preserves its data state across restarts.
