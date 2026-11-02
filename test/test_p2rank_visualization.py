import gzip
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # noqa: F401 - import pydantic before PySide6
from amdockvs.models import BindingSite
import pymol2
from PySide6.QtWidgets import QApplication
from ms_components.ms_pymol import PymolControlBar
from amdockvs.ui.tools.binding_sites import detection


def test_p2rank_markers_survive_the_viewers_hydrogen_policy(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    points = tmp_path / "protein.pdb_points.pdb.gz"
    with gzip.open(points, "wt") as handle:
        handle.write(
            "HETATM    1 H    STP 1   1       0.000   1.000   2.000  0.50 0.100\n"
            "HETATM    2 H    STP 1   1      12.000  13.000  14.000  0.50 0.600\n"
            "HETATM    3 H    STP 1   2      15.000  16.000  17.000  0.50 0.700\n"
        )
    site = BindingSite(id=1, molecule_id=7, source="p2rank", source_ref="1",
                       extra_data={"rank": 1, "score": 9.5, "points_path": str(points)})
    with pymol2.PyMOL() as session:
        cmd = session.cmd
        bar = PymolControlBar(cmd)
        view = detection.BindingSitesWidget(runtime=SimpleNamespace())
        errors = []
        dock = SimpleNamespace(cmd=cmd, show=lambda: None)
        monkeypatch.setattr(view, "_pymol_command", lambda: (dock, cmd))
        monkeypatch.setattr(view, "_show_error", lambda *args: errors.append(args))
        monkeypatch.setattr(detection, "set_pymol_scene_context", lambda *args, **kwargs: None)
        view._sites = [site]
        receptor = (b"ATOM      1  H   ALA A   1       0.000   0.000   0.000  1.00  0.00           H\nEND\n", "pdb")
        view._draw_sites_in_pymol([site], site, receptor)
        assert not errors
        for _ in range(3):
            bar.apply_hydrogen_visibility()
            assert cmd.count_atoms("amdock_p2rank_pockets and visible") == 2
            assert cmd.count_atoms("amdock_p2rank_points_1 and visible") == 2
            if bar._hydrogen_mode == "none":
                assert cmd.count_atoms("amdock_p2rank_receptor and hydro and visible") == 0
            bar.toggle_hydrogens()
        view.close()
        bar.close()
    app.processEvents()
