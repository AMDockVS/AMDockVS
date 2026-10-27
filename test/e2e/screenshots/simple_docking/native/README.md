# Native X11 walkthrough evidence

Captured by `test_simple_docking_tutorial_native_pymol` on 2026-10-26
using MSDD_312, the published `ms_components==0.3.5` wheel, Qt 6.12.0,
PyMOL 3.2.0a, and an NVIDIA GeForce GTX 1660 SUPER with driver 580.178.04.
The test passed in 47.61 seconds on the real X11 display (`xcb`), with an
OpenGL 4.6 compatibility context.

`completed.png` shows the finished tutorial after a real Vina calculation:
one receptor/ligand pair, nine poses, and a selected pose with its score and
rendered structure. `pymol-native-window.png` shows PyMOL detached into its
own native window with the tutorial callout following it. `graphics.json`
records the renderer and framebuffer checks before detachment, while
detached, after reattachment, and after selecting a docking result.

To regenerate with a working desktop display and installed dependencies:

```sh
QT_QPA_PLATFORM=xcb AMDOCK_TEST_TUTORIAL_NATIVE=1 PYTHONPATH=src python -m pytest -q test/e2e/test_simple_docking_tutorial.py::test_simple_docking_tutorial_native_pymol
```

The walkthrough clicks the application's real Qt controls and uses isolated
project/settings directories. File selection uses Qt's non-native chooser;
the GTK portal chooser is not covered. Detachment and reattachment use the
public dock actions; manual window-manager dragging/snapping is not covered.
