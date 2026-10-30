"""Saved QSAR models open as project results without a receptor setup tab."""
from types import SimpleNamespace

import amdockvs.runtime  # noqa: F401 - initialize Pydantic before PySide6

from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.view_ids import COMPLEXES_VIEW_ID, QSAR_MODELS_VIEW_ID, RECEPTOR_VIEW_ID


def test_default_views_prioritize_saved_results_over_receptors():
    class WindowStub:
        def __init__(self, *, docking, models):
            self.views = SimpleNamespace(_library_shape=lambda: (False, 2))
            self.runtime = SimpleNamespace(
                docking=SimpleNamespace(has_results=lambda: docking),
                qsar=SimpleNamespace(list_models=lambda: models),
            )
            self.opened = []

        def open_or_focus_view(self, view_id):
            self.opened.append(view_id)

    docking_and_qsar = WindowStub(docking=True, models=[object()])
    AMDockVSMainWindow._open_default_views(docking_and_qsar)
    assert RECEPTOR_VIEW_ID not in docking_and_qsar.opened
    assert docking_and_qsar.opened[-2:] == [QSAR_MODELS_VIEW_ID, COMPLEXES_VIEW_ID]

    qsar_only = WindowStub(docking=False, models=[object()])
    AMDockVSMainWindow._open_default_views(qsar_only)
    assert RECEPTOR_VIEW_ID not in qsar_only.opened
    assert qsar_only.opened[-1] == QSAR_MODELS_VIEW_ID
