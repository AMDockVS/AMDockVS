from amdockvs.core.user_env import is_remembered, remember_env_var


def test_remember_replaces_quotes_and_forgets(tmp_path):
    profile = tmp_path / ".bashrc"
    profile.write_text("alias ll='ls -l'\nexport ESM_API_KEY=old\n")
    assert is_remembered("ESM_API_KEY", profile=profile)

    remember_env_var("ESM_API_KEY", "a b$c", profile=profile)
    assert profile.read_text() == "alias ll='ls -l'\nexport ESM_API_KEY='a b$c'\n"

    remember_env_var("ESM_API_KEY", "", profile=profile)
    assert profile.read_text() == "alias ll='ls -l'\n"
    assert not is_remembered("ESM_API_KEY", profile=profile)
