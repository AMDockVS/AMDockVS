import time
from types import SimpleNamespace

import pytest


class _Cmd:
    """PyMOL's command module as far as the viewer goes: what was loaded, and from which bytes."""

    def __init__(self):
        self.objects: list[str] = []
        self.loaded: list[tuple[bytes, str, str]] = []

    def get_names(self, _kind):
        return list(self.objects)

    def delete(self, name):
        self.objects = [] if name == "all" else [o for o in self.objects if o != name]

    def load_raw(self, data, fmt, name):
        self.loaded.append((data, fmt, name))
        self.objects.append(name)

    def __getattr__(self, _name):  # zoom/show/set/color/select: don't care
        return lambda *a, **k: None


def test_a_pose_and_its_receptor_reach_pymol_as_bytes(tmp_path, monkeypatch):
    from amdockvs import AMDockVSRuntime  # before PySide: pydantic must finish importing first

    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.viewers.molecular_viewer import MolecularViewerController
    from payload_seed import seed_payload

    app = QApplication.instance() or QApplication(["amdockvs-viewer-test"])
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="viewer", folder=tmp_path / "viewer", description="")
        store = runtime.workspace.project_db
        receptor = seed_payload(store, "ATOM receptor\n", "pdb")
        poses = [seed_payload(store, f"pose {n}\n", "sdf") for n in (1, 2)]
        cmd = _Cmd()
        window = SimpleNamespace(runtime=runtime, pymol_dock=SimpleNamespace(cmd=cmd, show=lambda: None))
        viewer = MolecularViewerController(window)

        def hit(pose):
            return SimpleNamespace(run_kind="screening", receptor_id=7, receptor_payload_id=receptor,
                                   pose_payload_id=pose, ligand_payload_id=None)

        def show(pose, count):
            viewer.load_hit(hit(pose), 1)
            deadline = time.monotonic() + 10
            while len(cmd.loaded) < count and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            return cmd.loaded

        loaded = show(poses[0], 2)
        assert {(data, fmt, name) for data, fmt, name in loaded} == {
            (b"ATOM receptor\n", "pdb", "receptor_7"), (b"pose 1\n", "sdf", "amdock_result_pose")}

        loaded = show(poses[1], 3)  # the receptor stays; only the pose is read and swapped
        assert loaded[2] == (b"pose 2\n", "sdf", "amdock_result_pose") and cmd.objects.count("receptor_7") == 1
        cmd.delete("all")
        assert len(show(poses[1], 5)) == 5  # a wiped canvas gets the receptor back with the pose
    finally:
        runtime.shutdown()


def test_a_redocked_complex_keeps_its_own_camera_not_the_last_complex_view(monkeypatch):
    from amdockvs import AMDockVSRuntime  # noqa: F401 - pydantic before PySide
    from amdockvs.ui.viewers import molecular_viewer

    scenes = []
    monkeypatch.setattr(molecular_viewer, "load_structure", lambda cmd, data, name: cmd.objects.append(name))
    monkeypatch.setattr(molecular_viewer, "set_pymol_scene_context", lambda *a, **k: scenes.append(k))
    cmd = _Cmd()
    viewer = molecular_viewer.MolecularViewerController(
        SimpleNamespace(pymol_dock=SimpleNamespace(cmd=cmd, show=lambda: None)))
    viewer._read = lambda wanted, then: then({k: b"x" for k, v in wanted.items() if v})
    viewer.load_hit(SimpleNamespace(run_kind="redocking", pose_payload_id=3, reference_ligand_payload_id=2,
                                    ligand_payload_id=None, reference_receptor_payload_id=1,
                                    receptor_payload_id=1), 1)
    # Every complex shares the object names, so a remembered view is another complex's camera.
    assert scenes and scenes[-1]["restore_view"] is False
