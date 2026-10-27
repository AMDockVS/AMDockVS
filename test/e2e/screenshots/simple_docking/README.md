# Guided docking review screenshots

Captured from the eight-stage tutorial walkthrough on 2026-10-26, using the
Qt offscreen platform and the dark theme. The walkthrough creates an isolated
project through the normal Projects form, imports the bundled Vps34 and SAR405
files through file choosers, prepares both molecules, and completes a real Vina
calculation.

The test uses the real Grid Box editor in a test dock with PyMOL disabled.
These images document the tutorial presentation; desktop window management,
file choosers on other platforms, and the native PyMOL integration still need
manual review.

Regenerate screenshots with:

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python test/e2e/simple_docking_walkthrough.py /tmp/amdockvs-tutorial-review
```

The checked-in images correspond to `step-1.png`, `project-ready.png`,
`import-receptor.png`, and `import-ligand.png` from that output directory.
