from __future__ import annotations


def normalize_kind(kind: str) -> str:
    """Return a supported import kind."""
    value = str(kind or "").strip().lower()
    if value not in {"ligand", "receptor", "molecule"}:
        raise ValueError("kind must be 'ligand', 'receptor' or 'molecule'.")
    return value


def normalize_role(value: str | None) -> str:
    """Return a normalized role string."""
    return str(value or "").strip().lower()


def normalize_context(value: str | None) -> str:
    """Return a normalized import context string."""
    return str(value or "").strip().lower()


def normalize_molecule_kind(value: str | None, *, kind: str) -> str:
    """Return the explicit molecule kind or the default for the import kind."""
    normalized = str(value or "").strip().lower()
    if normalized:
        return normalized
    if kind == "ligand":
        return "small_molecule"
    return "unknown"


__all__ = [
    "normalize_context",
    "normalize_kind",
    "normalize_molecule_kind",
    "normalize_role",
]
