"""Docking methods: protocols parametrized and validated on their own.

The method is chosen before anything is prepared. It decides which programs (and which of
their scoring functions) may run, and which receptors fit: a zinc site is docked with
AutoDock4Zn only, and nothing else docks into a zinc site. Methods never combine.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class DockingMethod:
    key: str
    label: str
    # program key -> scoring functions it may use (None = any of its own); None = every program.
    programs: Mapping[str, tuple[str, ...] | None] | None
    # True: the receptor needs a Zn inside the box. False: it must have none there.
    zinc_site: bool

    def allows(self, program: str, scoring_function: str | None = None) -> bool:
        if self.programs is None:
            return True
        if program not in self.programs:
            return False
        allowed = self.programs[program]
        return allowed is None or scoring_function is None or str(scoring_function).lower() in allowed


STANDARD = DockingMethod(
    key="standard",
    label="Standard",
    programs=None,
    zinc_site=False,
)
# AutoDock4Zn: TZ pseudo-atoms on the receptor and the AD4Zn parameters in the autogrid4
# maps. Only engines that score with those maps can use it.
ZINC = DockingMethod(
    key="zinc",
    label="Zinc metalloprotein (AutoDock4Zn)",
    programs={"vina": ("ad4",), "adgpu": None},
    zinc_site=True,
)
METHODS: dict[str, DockingMethod] = {method.key: method for method in (STANDARD, ZINC)}


def get_method(key: object) -> DockingMethod:
    name = str(key or STANDARD.key).strip().lower()
    if name not in METHODS:
        raise ValueError(f"Unknown docking method {key!r}; choose one of {sorted(METHODS)}.")
    return METHODS[name]


@lru_cache(maxsize=64)
def _zinc_atoms(path: str, _mtime: float) -> tuple[tuple[float, float, float], ...]:
    # ponytail: keyed by mtime so a re-prepared receptor is read again.
    atoms = []
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        fields = line.split()
        if line[12:16].strip().upper() != "ZN" and (not fields or fields[-1].upper() != "ZN"):
            continue
        try:
            atoms.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
        except ValueError:
            continue
    return tuple(atoms)


def zinc_in_box(receptor_path: Path, center: Sequence[float], size: Sequence[float]) -> bool:
    """A Zn atom (PDB atom name or PDBQT type ZN) inside the box (center ± size/2)."""
    path = Path(receptor_path)
    return any(
        all(abs(xyz[i] - float(center[i])) <= abs(float(size[i])) / 2.0 for i in range(3))
        for xyz in _zinc_atoms(str(path), path.stat().st_mtime)
    )


def check_program(method: object, program: str, scoring_function: str | None = None) -> DockingMethod:
    """The method, once it is known to run `program` (with `scoring_function`)."""
    docking_method = get_method(method)
    if not docking_method.allows(program, scoring_function):
        raise ValueError(
            f"The {docking_method.label} method does not run {program}"
            + (f" with {scoring_function!r} scoring." if scoring_function else ".")
        )
    return docking_method


def check_receptor(method: DockingMethod, receptor_path: Path, center, size) -> None:
    """Engines refuse a receptor + box the method does not fit (the planner leaves them out)."""
    misfit = receptor_misfit(method, receptor_path, center, size)
    if misfit:
        raise ValueError(misfit)


def receptor_misfit(method: DockingMethod, receptor_path: Path, center, size) -> str:
    """Why this receptor + box cannot be docked with `method` ("" when it can)."""
    has_zinc = zinc_in_box(receptor_path, center, size)
    if method.zinc_site and not has_zinc:
        return f"The {method.label} method needs a zinc atom inside the box."
    if not method.zinc_site and has_zinc:
        return f"The box holds a zinc atom: dock it with the {ZINC.label} method."
    return ""


def excludes(method: object, receptor_path: object, center, size) -> bool:
    """Planning gate: a receptor whose box does not fit the method is left out of the run.
    A missing file is not decided here; the docking step reports it."""
    path = Path(str(receptor_path))
    return path.is_file() and bool(receptor_misfit(get_method(method), path, center, size))


__all__ = [
    "DockingMethod",
    "METHODS",
    "STANDARD",
    "ZINC",
    "check_program",
    "check_receptor",
    "excludes",
    "get_method",
    "receptor_misfit",
    "zinc_in_box",
]
