from __future__ import annotations

import pydantic.fields  # noqa: F401 - load before PySide6's feature importer
import pytest
from PySide6.QtWidgets import QApplication

from amdockvs import app
from ms_components import opengl


def test_incompatible_graphics_stops_before_starting_runtime(monkeypatch, capsys):
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.delenv("AMDOCK_DISABLE_PYMOL", raising=False)
    monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))

    def incompatible():
        raise RuntimeError("No compatible OpenGL context is available for PyMOL.")

    monkeypatch.setattr(opengl, "prepare_pymol_opengl", incompatible)
    assert app.main([]) == 1
    assert "No compatible OpenGL" in capsys.readouterr().err


@pytest.mark.parametrize("env", [
    {"QT_QPA_PLATFORM": "offscreen"},
    {"AMDOCK_DISABLE_PYMOL": "1"},
])
def test_headless_startup_skips_graphics_probe(monkeypatch, env):
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.delenv("AMDOCK_DISABLE_PYMOL", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    class ReachedApplication(Exception):
        pass

    class FakeApplication:
        @staticmethod
        def instance():
            return None

        def __init__(self, *args):
            raise ReachedApplication

    def unexpected_probe():
        pytest.fail("Headless startup must not access a graphics driver")

    monkeypatch.setattr(opengl, "prepare_pymol_opengl", unexpected_probe)
    monkeypatch.setattr("PySide6.QtWidgets.QApplication", FakeApplication)
    with pytest.raises(ReachedApplication):
        app.main([])
