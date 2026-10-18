from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from amdockvs.core.home import amdock_home


_DEFAULT_PROJECT_ROOT: Path | None = None


def tools_home(runtime=None) -> Path:
    """Where AMDock installs the third-party tools it manages (p2rank, protonation envs).

    Env var wins so an HPC job can be pointed elsewhere without touching a config file;
    then the setting; then ``tools`` in the AMDockVS home.
    """
    configured = str(os.environ.get("AMDOCK_TOOLS_HOME") or "").strip()
    if not configured:
        from amdockvs.core.configuration import app_config

        configured = str(app_config(runtime).tools_home or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (amdock_home() / "tools").resolve()


def set_default_project_root(project_root: str | Path | None) -> None:
    global _DEFAULT_PROJECT_ROOT
    _DEFAULT_PROJECT_ROOT = None if project_root is None else Path(project_root).expanduser().resolve()


def get_default_project_root() -> Path | None:
    return _DEFAULT_PROJECT_ROOT


def molecule_storage_key(kind: str, source_file: Path, source_index: int, variant: str = "") -> str:
    """`variant` distinguishes several molecules born from one source record (split fragments)."""
    stem = re.sub(r"[^a-zA-Z0-9_]+", "_", source_file.stem)[:32].strip("_") or "item"
    seed = f"{source_file.resolve()}::{source_index}::{variant}" if variant else f"{source_file.resolve()}::{source_index}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{str(kind).strip().lower()}_{stem}_{int(source_index):09d}_{digest}"


__all__ = ["get_default_project_root", "molecule_storage_key", "set_default_project_root", "tools_home"]
