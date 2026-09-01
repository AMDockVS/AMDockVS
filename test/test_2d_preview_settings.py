from types import SimpleNamespace

import pytest
import toml


def _write_alkane(path, heavy_atoms: int) -> None:
    from rdkit import Chem

    molecule = Chem.MolFromSmiles("C" * heavy_atoms)
    assert molecule is not None
    Chem.MolToMolFile(molecule, str(path))


def test_amdock_config_declares_2d_preview_heavy_atom_limit():
    from amdockvs.configuration import (
        DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS,
        MoleculeDisplayConfiguration,
    )

    field = MoleculeDisplayConfiguration.model_fields["max_2d_preview_heavy_atoms"]

    assert DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS == 50
    assert field.annotation is int
    assert any(getattr(item, "ge", None) == 1 for item in field.metadata)


def test_2d_preview_skips_molecules_above_configured_heavy_atom_limit(tmp_path):
    pytest.importorskip("rdkit")
    from amdockvs.ui.catalog.common import _molecule_svg_for_path

    at_limit = tmp_path / "alkane_50.mol"
    above_limit = tmp_path / "alkane_51.mol"
    _write_alkane(at_limit, 50)
    _write_alkane(above_limit, 51)

    assert "<svg" in _molecule_svg_for_path(str(at_limit), max_heavy_atoms=50)
    assert _molecule_svg_for_path(str(above_limit), max_heavy_atoms=50) == ""
    assert "<svg" in _molecule_svg_for_path(str(above_limit), max_heavy_atoms=51)


def test_runtime_bound_2d_preview_reads_the_effective_setting(tmp_path):
    pytest.importorskip("rdkit")
    from amdockvs.configuration import MAX_2D_PREVIEW_HEAVY_ATOMS_PATH
    from amdockvs.ui.catalog.common import molecule_2d_preview_paint_for_runtime

    molecule_path = tmp_path / "alkane_51.mol"
    _write_alkane(molecule_path, 51)
    row = {
        "__raw__": SimpleNamespace(
            molecule_type="small_molecule",
            current_path=str(molecule_path),
            stored_path="",
        )
    }

    class ConfigurationStub:
        def __init__(self, owner):
            self._owner = owner

        def get_value(self, path):
            assert path == MAX_2D_PREVIEW_HEAVY_ATOMS_PATH
            return self._owner.limit

    class RuntimeStub:
        limit = 50

        @property
        def amdock_configuration(self):
            return ConfigurationStub(self)

    runtime = RuntimeStub()
    paint = molecule_2d_preview_paint_for_runtime(runtime)

    assert paint(row) == ""
    runtime.limit = 51
    assert "<svg" in paint(row)


def test_runtime_migrates_legacy_flat_preview_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy_path = tmp_path / ".molsuite" / "config.toml"
    legacy_path.parent.mkdir(parents=True)
    with legacy_path.open("w", encoding="utf-8") as handle:
        toml.dump(
            {"applications": {"amdockvs": {"max_2d_preview_heavy_atoms": 63}}},
            handle,
        )

    from amdockvs.configuration import MAX_2D_PREVIEW_HEAVY_ATOMS_PATH
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    try:
        assert runtime.amdock_configuration.get_value(MAX_2D_PREVIEW_HEAVY_ATOMS_PATH) == 63
        migrated = toml.load(tmp_path / ".config" / "AMDockVS" / "config.toml")
        assert migrated["molecule_display"]["max_2d_preview_heavy_atoms"] == 63
        cleaned = toml.load(legacy_path)
        assert "amdockvs" not in cleaned.get("applications", {})
    finally:
        runtime.shutdown()


def test_amdock_configuration_nests_component_and_theme_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from amdockvs.configuration import (
        MonitorConfig,
        THEME_NAME_PATH,
        create_amdock_configuration,
    )

    config = create_amdock_configuration()
    # The component owns its defaults (MonitorConfig); AMDock only nests the model.
    assert config.get_value("monitor.poll_ms") == MonitorConfig().poll_ms
    assert config.get_value("theme.name") == "auto"

    # Overrides persist in AMDock's own global file, not in the component.
    config.set_value(THEME_NAME_PATH, "dracula")
    config.set_value("monitor.poll_ms", 250)

    reloaded = create_amdock_configuration()
    assert reloaded.get_value("theme.name") == "dracula"
    assert reloaded.get_value("monitor.poll_ms") == 250
    saved = toml.load(tmp_path / ".config" / "AMDockVS" / "config.toml")
    assert saved["theme"]["name"] == "dracula"
    assert saved["monitor"]["poll_ms"] == 250


def test_table_view_prefs_persist_per_table(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from amdockvs.configuration import create_amdock_configuration

    config = create_amdock_configuration()
    # No prefs saved yet: the table id is simply absent.
    with pytest.raises(KeyError):
        config.get_value("tables.LigandTableWidget")

    config.set_value(
        "tables.LigandTableWidget",
        {"columns": ["name", "mw"], "sort": [{"field": "mw", "descending": True}]},
    )

    reloaded = create_amdock_configuration()
    state = reloaded.get_value("tables.LigandTableWidget")
    assert state.columns == ["name", "mw"]
    assert state.sort[0].field == "mw" and state.sort[0].descending is True
    # Other tables stay on their code-defined defaults (no stray keys written).
    saved = toml.load(tmp_path / ".config" / "AMDockVS" / "config.toml")
    assert list(saved["tables"]) == ["LigandTableWidget"]


def test_theme_helpers_persist_choice_in_amdock_config(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("HOME", str(tmp_path))
    from PySide6.QtWidgets import QApplication
    from amdockvs.ui.theme import saved_theme_name, set_theme

    app = QApplication.instance() or QApplication(["amdockvs-theme-test"])
    assert saved_theme_name() == "auto"

    set_theme("nord", app=app)

    assert saved_theme_name() == "nord"
    saved = toml.load(tmp_path / ".config" / "AMDockVS" / "config.toml")
    assert saved["theme"]["name"] == "nord"


def test_retired_theme_preferences_migrate_to_available_themes(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("HOME", str(tmp_path))
    from PySide6.QtWidgets import QApplication
    from amdockvs.configuration import THEME_NAME_PATH, create_amdock_configuration
    from amdockvs.ui.theme import saved_theme_name, set_theme

    app = QApplication.instance() or QApplication(["amdockvs-retired-theme-test"])
    create_amdock_configuration().set_value(THEME_NAME_PATH, "atom_one")
    assert saved_theme_name() == "one_dark_two"

    set_theme("blender", app=app)
    assert saved_theme_name() == "catppuccin_mocha"


def test_amdock_file_menu_opens_and_saves_app_settings(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("HOME", str(tmp_path))

    from amdockvs.configuration import MAX_2D_PREVIEW_HEAVY_ATOMS_PATH
    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.ui.projects import ApplicationWidget
    from PySide6.QtWidgets import QApplication, QSpinBox

    app = QApplication.instance() or QApplication(["amdockvs-settings-test"])
    runtime = AMDockVSRuntime()
    menu = ApplicationWidget(runtime=runtime)
    saved: list[bool] = []
    menu.settings_saved.connect(lambda: saved.append(True))

    try:
        menu.open_settings()
        app.processEvents()

        dialog = menu._settings_dialog
        assert dialog is not None
        assert dialog.panel.tab_ids() == ("ms_flow", "amdockvs")
        # Tool installs live in their own page, not in each feature panel.
        tree = dialog.panel.tree
        titles = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
        assert "External tools" in titles
        editor = dialog.panel.editor("amdockvs", MAX_2D_PREVIEW_HEAVY_ATOMS_PATH)
        assert isinstance(editor, QSpinBox)
        assert editor.value() == 50

        editor.setValue(55)
        dialog.panel.save_settings()

        assert runtime.amdock_configuration.get_value(MAX_2D_PREVIEW_HEAVY_ATOMS_PATH) == 55
        assert saved == [True]
    finally:
        if menu._settings_dialog is not None:
            menu._settings_dialog.close()
        menu.close()
        app.processEvents()
        runtime.shutdown()
