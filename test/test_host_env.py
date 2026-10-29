"""What another machine is given to run the jobs, and what a job looks for again when it runs there."""
import sys

from ms_env import Host

from amdockvs.core.constants import vina_backend, vina_command
from amdockvs.docking.engines.vina import _vina_here
from amdockvs.integrations import hosts


def test_a_vina_that_is_not_on_the_machine_that_runs_is_looked_for_again():
    assert _vina_here("python", "/nowhere/vina") == ("python", "/nowhere/vina")
    assert _vina_here("binary", sys.executable) == ("binary", sys.executable)
    assert _vina_here("binary", "/nowhere/vina") == (vina_backend(), vina_command())


def test_a_host_gets_the_env_and_the_tools_under_its_amdockvs_home(monkeypatch):
    asked = {}
    monkeypatch.setattr(hosts, "app_recipe", lambda name, packages, imports, **where: asked.update(where) or "the env")
    home = Host(None).home()
    recipes = hosts.host_recipes(Host(None))
    assert recipes[0] == "the env"
    assert (asked["prefix"], asked["tools_home"]) == (f"{home}/.amdockvs/env", f"{home}/.amdockvs/tools")
    assert all(recipe.prefix.startswith(f"{home}/.amdockvs/tools/") for recipe in recipes[1:])

    ticked = hosts.host_recipes(Host(None), install=["unidock"])  # what is ticked, installed here or not
    assert [recipe.name for recipe in ticked[1:]] == ["unidock"]
    assert "ketcher" not in hosts.HOST_PROGRAMS and "unidock" in hosts.HOST_PROGRAMS  # an editor stays here


def test_the_engine_asks_the_app_for_the_env_of_a_host_and_a_busy_one_is_not_changed(monkeypatch):
    from concurrent.futures import Future

    class Recipe:
        prefix = "/there/env"

    def ensure(recipe, host, busy):
        ended = Future()
        ended.set_exception(RuntimeError(busy())) if busy and busy() else ended.set_result(None)
        return type("Install", (), {"future": ended, "lines": ["a line"], "done": 1, "total": 1, "step": ""})

    said = []
    monkeypatch.setattr(hosts, "host_recipes", lambda host, runtime, install: [Recipe])
    monkeypatch.setattr(hosts, "ensure", ensure)
    assert hosts.prepare_host("pc2", said.append) == "/there/env" and said == [
        "[0/0] pc2: reading what runs here", "a line", "[1/1] pc2: ready"]
    try:
        hosts.prepare_host("pc2", said.append, busy=lambda: "2 jobs run there")
        raise AssertionError("changed under its jobs")
    except RuntimeError as error:
        assert "2 jobs run there" in str(error)
