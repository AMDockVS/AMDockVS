"""Docking Studio's step check marks follow the data, not the last visit to their step."""
import time

import pytest
from PySide6.QtTest import QTest

from amdockvs.models import MoleculeRecord
from test_receptor_missing_3d import receptor_studio  # noqa: F401  (fixture)


def test_receptor_check_mark_follows_the_data_from_another_step(receptor_studio):
    """Preparation moves Studio on to Preview & Run, and jobs can finish while it is hidden:
    the Receptors check mark must follow the data without a revisit of its step."""
    from amdockvs.models import ReceptorPrep

    scene = receptor_studio
    studio = scene.studio

    def wait_for_done(expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            scene.app.processEvents()
            if studio.step_receptors.is_done() == expected:
                return
            QTest.qWait(10)
        pytest.fail(f"Receptors check mark stayed {studio.step_receptors.is_done()}")

    scene.window.selection.set("receptor", scene.ids[:1])  # boxed, not yet prepared
    studio.stepper.set_current_index(3)
    wait_for_done(False)
    studio.hide()
    with scene.runtime.workspace.project_db.get_session() as session:
        receptor = session.get(MoleculeRecord, scene.ids[0])
        session.add(ReceptorPrep(receptor_molecule_id=int(receptor.id), family="ad4",
                                 rigid_payload_id=receptor.current_payload_id))
        session.commit()
    studio.show()  # reopened on Preview & Run, without visiting Receptors
    wait_for_done(True)
    assert studio.stepper.current_index == 3
