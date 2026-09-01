from __future__ import annotations

from amdockvs.ui.workspace import ComplexWidget, LigandActivityWidget

COMPLEXES_VIEW_ID = "workspace.complexes"
LIGAND_ACTIVITY_VIEW_ID = "workspace.ligand_activity"


class ComplexResultsViewWidget(ComplexWidget):
    def refresh(self) -> None:
        self.refresh_view()


class LigandActivityViewWidget(LigandActivityWidget):
    def refresh(self) -> None:
        self.refresh_view()


def register_complexes_workspace(window) -> None:
    # Imported inside the factory: the pivots live under ui.tools.docking, which imports
    # ui.workspace, which imports this package.
    from amdockvs.ui.tools.docking.results_pivot import ResultsPivotWidget

    window.register_main_view(
        COMPLEXES_VIEW_ID,
        "Docking Results",
        lambda: ResultsPivotWidget(
            runtime=window.runtime,
            load_hit_in_pymol=getattr(window, "load_hit_in_pymol", None),
            parent=window.central_widget,
        ),
    )


def register_ligand_activity_workspace(window) -> None:
    window.register_main_view(
        LIGAND_ACTIVITY_VIEW_ID,
        "Activity",
        lambda: LigandActivityViewWidget(
            runtime=window.runtime,
            open_results_view=window.open_complex_results,
            show_histogram=getattr(window, "show_activity_histogram", None),
            parent=window.central_widget,
        ),
    )



__all__ = [
    "COMPLEXES_VIEW_ID",
    "LIGAND_ACTIVITY_VIEW_ID",
    "ComplexResultsViewWidget",
    "LigandActivityViewWidget",
    "register_complexes_workspace",
    "register_ligand_activity_workspace",
]
