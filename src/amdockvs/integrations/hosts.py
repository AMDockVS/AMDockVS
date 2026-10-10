"""What another machine needs to run the jobs of AMDockVS: the environment this one runs in
and the programs the jobs call. All of it goes under the AMDockVS home of that machine."""
from __future__ import annotations

import time
from typing import Callable

from ms_env import Host, Recipe, app_recipe, ensure

from amdockvs.core.home import amdock_home
from amdockvs.integrations.envs import tool_spec
from amdockvs.integrations.registry import MANAGED_TOOLS

# The app whole, its windows included: what the jobs import is not told apart from the rest.
_PACKAGES = ("amdockvs[all]", "ms_flow[ray]", "ms_components", "ms_contactmap", "ms_env")
_IMPORTS = ("ms_flow.runner", "amdockvs")


def host_recipes(host: Host, runtime=None) -> list[Recipe]:
    """The environment of the app there, then every tool with a recipe that works here.

    Builds the wheels of what runs here from a checkout and runs each tool's check: not on the GUI thread.
    """
    # ponytail: `~/.amdockvs` there, whatever $AMDOCK_HOME says here; a setting per host when one needs another place.
    home = f"{host.home()}/.amdockvs"
    recipes = [
        app_recipe("amdockvs", _PACKAGES, _IMPORTS, prefix=f"{home}/env", tools_home=f"{home}/tools",
                   cache=amdock_home() / "cache" / "wheels")
    ]
    for tool in MANAGED_TOOLS:
        if tool.recipe is not None and tool.status(runtime).installed:
            recipes.append(tool_spec(tool.tool_id).recipe(f"{home}/tools"))
    return recipes


def prepare_host(address: str, on_line: Callable[[str], None] = print, runtime=None) -> str:
    """Give the machine `ssh` reaches at `address` what it needs to run the jobs of this app,
    making or updating only what is not as it is here. Returns the path of the environment there.

    Blocks, for minutes the first time. Raises what the first recipe that failed raised.
    """
    host = Host(address)
    recipes = host_recipes(host, runtime)
    for recipe in recipes:
        install, said = ensure(recipe, host), 0
        while True:
            ended = install.future.done()  # asked first: what it printed before ending is not lost
            lines = install.lines[said:]
            said += len(lines)
            for line in lines:
                on_line(line)
            if ended:
                break
            time.sleep(0.5)
        install.future.result()
    return recipes[0].prefix


__all__ = ["host_recipes", "prepare_host"]
