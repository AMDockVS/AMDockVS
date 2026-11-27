"""How many pairs one Uni-Dock process docks in a single GPU batch, and what it reports back.

Uni-Dock 1.2.0 does not dock a run's ligands in one go. It sorts them into size groups (Small,
Medium, Large, Extra Large, Max) and fills each group's GPU batch greedily: it keeps appending
ligands while its own `predict_peak_memory` stays under `max_memory`, which is 0.95 x the free
VRAM it read with `cudaMemGetInfo` as the process started (`unidock/src/main/main.cpp`). A pose's
random stream is its position inside its batch (`curand_init(seed, pose_id, 0, &states[pose_id])`
in `unidock/src/cuda/monte_carlo.cu`), and changing a batch's size changes every score in it --
not only the ligands past the boundary.

So a fixed seed repeats only while each size group in a task docks as exactly one batch with the
same members. Free VRAM is not ours to fix: a desktop, a PyMOL view or a browser moves it by
hundreds of MiB, and `--max_gpu_memory` is overwritten whenever a CUDA device is present, so it
cannot pin the budget from outside. What we can fix is the number of pairs we hand one process:
`single_batch_pairs` sizes a seeded task from a *declared* memory floor instead of the live
reading, and `groups_that_split` reads the batch sizes Uni-Dock printed so a task that split
anyway is reported instead of quietly returning scores that will not come back.

Measured against Uni-Dock 1.2.0 on an 8 GiB card (InhA, Vina, Fast, 700 DUD-E decoys), the first
batch this predicts is within 2% of the one it really formed, and always on the low side: 395 vs
400 pairs at 7128 MiB free, 326 vs 327 at 5490 MiB, 266 vs 271 at 4428 MiB.
"""
from __future__ import annotations

import re
from typing import Mapping

#: Pairs one task may hold, whatever the memory model says.
MAX_PAIRS_PER_TASK = 10_000

#: Free VRAM a seeded run assumes when the project names none: half of an 8 GiB card, which
#: leaves room for a desktop, a PyMOL view and a browser without the batches moving.
DEFAULT_VRAM_FLOOR_MIB = 4096

#: Uni-Dock's search presets, as `main.cpp` maps them to exhaustiveness. Its own fallback is 384.
SEARCH_MODE_EXHAUSTIVENESS = {"fast": 128, "balance": 384, "balanced": 384, "detail": 512, "detailed": 512}
DEFAULT_EXHAUSTIVENESS = 384

#: `sizeof(precalculate_element_cuda_t_)`: the per-atom-pair table, and the term that dominates.
#: `predict_peak_memory` charges it (ligand atoms + movable receptor atoms)^2 per ligand, and a
#: rigid receptor contributes nothing (Vina keeps its atoms in `grid_atoms`, not `atoms`).
PRECALC_BYTES_PER_ATOM_PAIR = 24_616
#: `sizeof(ig_cuda_t_)`: the grid, allocated once per batch rather than per ligand.
GRID_BYTES = 78_655_940

#: The PDBQT atom count a screening ligand is assumed not to exceed: SmallConfig's own bound.
#: Capacity falls with the square of this, so a run whose ligands are bigger must say so.
MAX_SMALL_MOLECULE_ATOMS = 40

# (atoms the group holds, bytes per ligand, bytes per pose), from the compiled `sizeof` of the
# Uni-Dock 1.2.0 Config structs: m_cuda + p_cuda, and the per-pose allocations of
# `predict_peak_memory` (rand_molec_struc, results, m_cuda_global, h_cuda_global, 6x change_aux,
# 5x results_aux, pot_aux, and 64 bytes of cuRAND state).
_SIZE_GROUPS = (
    (40, 7_356, 12_076),
    (80, 14_068, 22_924),
    (120, 25_196, 38_876),
    (160, 48_300, 68_292),
    (300, 111_900, 147_276),
)

#: "Small Batch 1 size: 427", and the same for the other groups ("Extra Large" holds a space).
_BATCH_SIZE_LINE = re.compile(r"^(?P<group>.+?) Batch (?P<index>\d+) size:\s*(?P<size>\d+)\s*$")


class UniDockBatchSplit(RuntimeError):
    """A seeded task did not dock as one GPU batch per size group, so its scores will not repeat."""


def exhaustiveness_for(search_mode: str | None) -> int:
    """The exhaustiveness Uni-Dock runs a search mode at: it sizes the per-pose allocations."""
    return SEARCH_MODE_EXHAUSTIVENESS.get(str(search_mode or "").strip().lower(), DEFAULT_EXHAUSTIVENESS)


def _bytes_per_ligand(ligand_atoms: int, exhaustiveness: int) -> int:
    atoms = max(1, int(ligand_atoms))
    for group_atoms, ligand_bytes, pose_bytes in _SIZE_GROUPS:
        if atoms <= group_atoms:
            break
    else:  # a ligand past MaxConfig is Uni-Dock's own business; charge it the largest group
        _, ligand_bytes, pose_bytes = _SIZE_GROUPS[-1]
    return atoms * atoms * PRECALC_BYTES_PER_ATOM_PAIR + ligand_bytes + exhaustiveness * pose_bytes


def peak_batch_mib(pairs: int, *, ligand_atoms: int, exhaustiveness: int) -> int:
    """What Uni-Dock's `predict_peak_memory` returns for a batch of `pairs` ligands this size."""
    total = max(0, int(pairs)) * _bytes_per_ligand(ligand_atoms, exhaustiveness) + GRID_BYTES
    return total // 1024**2


def single_batch_pairs(
    free_mib: int,
    *,
    search_mode: str | None = "fast",
    ligand_atoms: int = MAX_SMALL_MOLECULE_ATOMS,
) -> int:
    """The most pairs Uni-Dock docks in one GPU batch with `free_mib` MiB of VRAM free.

    Mirrors the fill loop of `template_batch_docking`: it checks its prediction before appending,
    so the batch it forms is one ligand past the last prediction that fit.
    """
    if free_mib <= 0:
        raise ValueError("Free VRAM must be positive.")
    budget = int(free_mib * 0.95)  # Uni-Dock leaves 5% of the card to itself
    per_ligand = _bytes_per_ligand(ligand_atoms, exhaustiveness_for(search_mode))
    room = budget * 1024**2 - GRID_BYTES
    if room <= 0:  # the grid alone does not fit; Uni-Dock still tries one ligand
        return 1
    return max(1, min(MAX_PAIRS_PER_TASK, (room - 1) // per_ligand + 1))


def reported_batch_sizes(output: str) -> dict[str, list[int]]:
    """The GPU batches Uni-Dock printed, as size group -> the size of each of its batches."""
    batches: dict[str, list[int]] = {}
    for line in str(output or "").splitlines():
        found = _BATCH_SIZE_LINE.match(line.strip())
        if found is not None:
            batches.setdefault(found["group"], []).append(int(found["size"]))
    return batches


def groups_that_split(output: str) -> dict[str, list[int]]:
    """The size groups that needed more than one GPU batch, so this run is not reproducible."""
    return {group: sizes for group, sizes in reported_batch_sizes(output).items() if len(sizes) > 1}


def pairs_that_fitted(split: Mapping[str, list[int]]) -> int:
    """A task size the card had room for, read from a split it just made.

    The first batch of a group is what the memory free at that moment paid for; the ones after
    it only hold what was left over. The smallest of those first batches is a size every group
    of this run would have docked in one go.
    """
    return min((sizes[0] for sizes in split.values() if sizes), default=1)
