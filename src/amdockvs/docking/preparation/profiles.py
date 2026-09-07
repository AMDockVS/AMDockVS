"""Preparation profiles shared by one or more docking programs.

A profile owns the engine-specific ligand, receptor and shard transformations. The
Molsuite jobs only select a profile and persist its result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PreparationProfile:
    key: str
    prepare_entities: Callable[..., dict[str, Any]]
    prepare_shard: Callable[..., dict[str, Any]] | None = None


def _prepare_autodock_entities(**kwargs) -> dict[str, Any]:
    from amdockvs.docking.preparation.entities import prepare_entities_rows

    return prepare_entities_rows(**kwargs)


def _prepare_autodock_shard(*args, **kwargs) -> dict[str, Any]:
    from amdockvs.docking.preparation.shards import prepare_ligand_shard

    return prepare_ligand_shard(*args, **kwargs)


_PROFILES: dict[str, PreparationProfile] = {
    "ad4": PreparationProfile(
        key="ad4",
        prepare_entities=_prepare_autodock_entities,
        prepare_shard=_prepare_autodock_shard,
    ),
}


def register_preparation_profile(profile: PreparationProfile, *, replace: bool = False) -> None:
    key = str(profile.key or "").strip().lower()
    if not key:
        raise ValueError("A preparation profile requires a non-empty key.")
    if key in _PROFILES and not replace:
        raise ValueError(f"Preparation profile '{key}' is already registered.")
    _PROFILES[key] = profile


def get_preparation_profile(key: str) -> PreparationProfile:
    normalized = str(key or "").strip().lower()
    try:
        return _PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported docking preparation profile '{key}'. "
            f"Registered profiles: {sorted(_PROFILES)}"
        ) from exc


def list_preparation_profiles() -> tuple[PreparationProfile, ...]:
    return tuple(_PROFILES.values())


__all__ = [
    "PreparationProfile",
    "get_preparation_profile",
    "list_preparation_profiles",
    "register_preparation_profile",
]
