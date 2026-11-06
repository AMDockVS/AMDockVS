"""A large table run is offered its best pose only, and that is still the same protocol."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.docking.planning import DockingProtocol
from amdockvs.ui.tools.docking.run_panel import LARGE_RUN_ROWS, one_pose, stored_rows


def test_rows_are_pairs_times_poses_and_one_pose_keeps_the_protocol():
    protocols = [{"program": "vina", "label": "Vina", "config": {"exhaustiveness": 8, "num_modes": 9}},
                 {"program": "qvina", "label": "QuickVina", "config": {"exhaustiveness": 8}}]  # default: 9
    pairs = [("vina", 100_000), ("qvina", 20_000)]
    assert stored_rows(pairs, protocols) == 9 * 120_000 > LARGE_RUN_ROWS
    best = one_pose(protocols)
    assert stored_rows(pairs, best) == 120_000
    assert protocols[0]["config"]["num_modes"] == 9  # the panel's own protocols are left alone
    for before, after in zip(protocols, best):
        assert DockingProtocol.from_mapping(before).hash == DockingProtocol.from_mapping(after).hash
