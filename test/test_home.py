from amdockvs.core.configuration import create_amdock_configuration
from amdockvs.core.paths import tools_home
from amdockvs.io.ccd_bonds import _ccd_cache_dir


def test_everything_of_amdockvs_lives_in_its_one_home(tmp_path, monkeypatch):
    for variable in ("AMDOCK_HOME", "AMDOCK_TOOLS_HOME", "AMDOCK_CCD_CACHE"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".amdockvs"
    assert tools_home() == home / "tools"
    assert create_amdock_configuration().global_path == home / "config.toml"
    assert _ccd_cache_dir() == home / "cache" / "ccd"

    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "elsewhere"))
    assert tools_home() == tmp_path / "elsewhere" / "tools"
