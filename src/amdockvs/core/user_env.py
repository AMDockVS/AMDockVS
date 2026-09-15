"""Persist a user environment variable where the user's own shell reads it, on explicit request.

AMDock keeps no copy: the value lives in the user's shell setup, not in any AMDock config.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def shell_profile() -> Path:
    # ponytail: the interactive shell's rc; a desktop launcher that skips it needs ~/.profile.
    return Path.home() / (".zshrc" if os.environ.get("SHELL", "").endswith("zsh") else ".bashrc")


def _use_windows(profile: Path | None) -> bool:
    return sys.platform == "win32" and profile is None


def remember_env_var(name: str, value: str, *, profile: Path | None = None) -> str:
    """Write `export NAME=value` (replacing an older one); an empty value removes it. Returns where."""
    if _use_windows(profile):
        if value:
            subprocess.run(["setx", name, value], check=True, capture_output=True)
        else:
            subprocess.run(["reg", "delete", r"HKCU\Environment", "/v", name, "/f"], capture_output=True)
        return "the Windows user environment"
    target = profile or shell_profile()
    prefix = f"export {name}="
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    lines = [line for line in lines if not line.strip().startswith(prefix)]
    if value:
        lines.append(prefix + shlex.quote(value))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(target)


def is_remembered(name: str, *, profile: Path | None = None) -> bool:
    if _use_windows(profile):
        return False  # ponytail: not read back from the registry; the checkbox just starts unticked
    target = profile or shell_profile()
    prefix = f"export {name}="
    return target.exists() and any(line.strip().startswith(prefix) for line in target.read_text(encoding="utf-8").splitlines())


__all__ = ["is_remembered", "remember_env_var", "shell_profile"]
