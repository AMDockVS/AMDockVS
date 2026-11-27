"""Seeded Uni-Dock runs: one GPU batch per size group, and nothing silently scored when not.

Uni-Dock 1.2.0 fills a GPU batch from the free VRAM it reads at start-up, and a pose's random
stream is its place in that batch, so a seeded run only repeats while each size group of a task
docks as a single batch. These tests pin the arithmetic that sizes such a task, the parsing of
what Uni-Dock reports, and that a task which split anyway fails instead of returning scores.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.docking.api import DockingAPI
from amdockvs.docking.engines import unidock
from amdockvs.docking.engines.programs import get_docking_program
from amdockvs.docking.engines.unidock_batches import (
    DEFAULT_VRAM_FLOOR_MIB,
    GRID_BYTES,
    MAX_PAIRS_PER_TASK,
    UniDockBatchSplit,
    exhaustiveness_for,
    groups_that_split,
    pairs_that_fitted,
    peak_batch_mib,
    reported_batch_sizes,
    single_batch_pairs,
)

#: What Uni-Dock 1.2.0 printed on an 8 GiB card for 700 InhA DUD-E decoys (Vina, Fast, one
#: parser thread): the free VRAM it reported, and the first GPU batch it then formed. Those
#: ligands run ~18-40 PDBQT atoms, and the batch a real mix fills is never smaller than the one
#: the worst-case ligand size asks for -- which is the margin a seeded task is sized with.
MEASURED_FIRST_BATCH = ((7128, 400), (5490, 327), (4428, 271), (4444, 264))


def _source_fill_loop(free_mib: int, ligand_atoms: int, exhaustiveness: int) -> int:
    """`template_batch_docking`'s loop, transcribed: it predicts before it appends."""
    budget = int(free_mib * 0.95)
    pairs = 0
    while peak_batch_mib(pairs, ligand_atoms=ligand_atoms, exhaustiveness=exhaustiveness) < budget:
        pairs += 1
    return pairs


@pytest.mark.parametrize("free_mib", [3072, 4096, 4428, 5490, 7128, 24_576])
@pytest.mark.parametrize("ligand_atoms", [18, 25, 40, 80])
def test_the_task_size_is_the_batch_uni_dock_would_fill(free_mib, ligand_atoms):
    assert single_batch_pairs(free_mib, search_mode="fast", ligand_atoms=ligand_atoms) == min(
        MAX_PAIRS_PER_TASK, _source_fill_loop(free_mib, ligand_atoms, 128))


@pytest.mark.parametrize(("free_mib", "measured"), MEASURED_FIRST_BATCH)
def test_the_task_it_asks_for_fits_the_batch_the_card_really_formed(free_mib, measured):
    assert single_batch_pairs(free_mib, search_mode="fast") <= measured


def test_a_harder_search_and_a_lower_floor_both_shrink_the_task():
    at_floor = single_batch_pairs(DEFAULT_VRAM_FLOOR_MIB, search_mode="fast")
    assert at_floor < single_batch_pairs(2 * DEFAULT_VRAM_FLOOR_MIB, search_mode="fast")
    assert single_batch_pairs(DEFAULT_VRAM_FLOOR_MIB, search_mode="detail") < at_floor
    assert 1 <= at_floor <= MAX_PAIRS_PER_TASK
    with pytest.raises(ValueError):
        single_batch_pairs(0)


def test_an_unknown_search_mode_is_charged_uni_docks_own_default():
    assert exhaustiveness_for("fast") == 128 and exhaustiveness_for("detail") == 512
    assert exhaustiveness_for(None) == exhaustiveness_for("whatever") == 384
    assert peak_batch_mib(0, ligand_atoms=40, exhaustiveness=128) == GRID_BYTES // 1024**2


UNIDOCK_OUTPUT = """Set GPU device id to 0
Available Memory = 4428MiB   Total Memory = 7833MiB
Small Group:
Group size: 597
Small
Small Batch 1 size: 271
Batch 1 running time: 8433ms
Small Batch 2 size: 251
Batch 2 running time: 8120ms
Extra Large Batch 1 size: 3
Medium Batch 1 size: 103
"""


def test_the_batch_sizes_are_read_back_group_by_group():
    assert reported_batch_sizes(UNIDOCK_OUTPUT) == {
        "Small": [271, 251], "Extra Large": [3], "Medium": [103]}
    assert groups_that_split(UNIDOCK_OUTPUT) == {"Small": [271, 251]}
    # The first batch is what the free memory paid for; the rest is only the remainder.
    assert pairs_that_fitted(groups_that_split(UNIDOCK_OUTPUT)) == 271
    assert pairs_that_fitted({"Small": [101, 55], "Medium": [60, 40]}) == 60
    assert pairs_that_fitted({}) == 1
    assert groups_that_split("Small Batch 1 size: 32\nMedium Batch 1 size: 4\n") == {}
    assert reported_batch_sizes("") == {}


def _api(*, seeded_batch_size: int = 0, batch_size: int = 32, autobatch: int = 128,
         floor: int = DEFAULT_VRAM_FLOOR_MIB) -> DockingAPI:
    settings = SimpleNamespace(batch_size=batch_size, seeded_batch_size=seeded_batch_size,
                               seeded_vram_floor_mib=floor, autobatch=autobatch)
    config = SimpleNamespace(docking=SimpleNamespace(unidock=settings, vina=settings))
    api = DockingAPI(runtime=None)
    api._test_config = config
    return api


@pytest.fixture()
def patched_app_config(monkeypatch):
    def use(api: DockingAPI) -> DockingAPI:
        monkeypatch.setattr("amdockvs.docking.api.app_config", lambda _runtime=None: api._test_config)
        return api
    return use


def test_a_seeded_unidock_run_is_sized_from_the_memory_floor(patched_app_config):
    api = patched_app_config(_api())
    unidock_spec = get_docking_program("unidock")
    seeded = {"seed": 261124, "search_mode": "fast"}
    expected = single_batch_pairs(DEFAULT_VRAM_FLOOR_MIB, search_mode="fast")
    assert api.pairs_per_task(unidock_spec, engine_config=seeded) == expected
    assert expected > api.pairs_per_task(unidock_spec, engine_config={"seed": 0}) == 32
    # Growing a task would group the ligands differently from one run to the next.
    assert api.autobatch(unidock_spec, engine_config=seeded) == 0
    assert api.autobatch(unidock_spec, engine_config={"seed": 0}) == 4


def test_a_caller_or_the_project_may_pin_the_seeded_task_size(patched_app_config):
    unidock_spec = get_docking_program("unidock")
    seeded = {"seed": 261124, "search_mode": "fast"}
    # What the reproducibility bundle passes through the API wins over both settings.
    assert patched_app_config(_api()).pairs_per_task(unidock_spec, 32, engine_config=seeded) == 32
    assert patched_app_config(_api(seeded_batch_size=64)).pairs_per_task(
        unidock_spec, engine_config=seeded) == 64


def test_only_uni_dock_reads_the_seed_for_its_task_size(patched_app_config):
    api = patched_app_config(_api())
    vina_spec = get_docking_program("vina")
    assert api.pairs_per_task(vina_spec, engine_config={"seed": 261124}) == 32
    assert api.autobatch(vina_spec, engine_config={"seed": 261124}) == 4


def _pair(ligand: Path, receptor: Path, ligand_id: int) -> dict:
    return {"ligand_id": ligand_id, "receptor_id": 1, "ligand_path": str(ligand),
            "receptor_path": str(receptor), "box_center": [1.0, 2.0, 3.0], "box_size": [20.0, 20.0, 20.0]}


def _fake_run(output: str, poses: str = "REMARK VINA RESULT:   -9.1  0.0  0.0\nATOM\n"):
    """Stand in for the Uni-Dock process: feed `output` to the follower, write each pose file."""
    calls = []

    def run(command, progress=None, next_ligand=None, *, follow=None, cwd=None, env=None):
        import io

        calls.append({"command": list(command), "env": env, "cwd": cwd})
        work = Path(cwd)
        for name in (line.strip()[:-len(".pdbqt")] for line in (work / "ligands.txt").read_text().splitlines()):
            (work / f"{name}_out.pdbqt").write_text(poses)
        return follow(io.StringIO(output)) if follow is not None else output

    return run, calls


def test_a_seeded_task_that_split_fails_instead_of_returning_scores(tmp_path, monkeypatch):
    ligand, receptor = tmp_path / "l.pdbqt", tmp_path / "r.pdbqt"
    for path in (ligand, receptor):
        path.write_text("ROOT\nATOM      1  C   LIG A   1       0.000   0.000   0.000  1.00  0.00     0.000 C\nENDROOT\n")
    run, calls = _fake_run(UNIDOCK_OUTPUT)
    monkeypatch.setattr(unidock, "_run_vina", run)
    monkeypatch.setattr(unidock, "unidock_command", lambda *_a, **_k: "unidock")
    monkeypatch.setattr(unidock, "check_receptor", lambda *_a, **_k: None)
    monkeypatch.setattr(unidock, "dockable_ligand", lambda path, **_k: path)
    reported = []
    rows = unidock.unidock_dock_runner({
        "pairs": [_pair(ligand, receptor, i) for i in (1, 2)],
        "output_dir": str(tmp_path / "out"), "seed": 261124, "search_mode": "fast",
        "_pair_callback": reported.append,
    })
    assert len(rows) == 2 and all(row.get("error") for row in rows)
    assert "over several GPU batches" in rows[0]["error"] and "261124" in rows[0]["error"]
    assert "Pairs per seeded task to 271 or less" in rows[0]["error"]
    # Nothing was reported before the layout was known: no scored row reached the project.
    assert [call for batch in reported for call in batch] == rows
    assert not any(row.get("score") for row in rows)
    # One parser thread, so the index order is the order Uni-Dock docks in.
    assert calls[0]["env"]["OMP_NUM_THREADS"] == calls[0]["env"]["OMP_THREAD_LIMIT"] == "1"
    assert "--seed" in calls[0]["command"] and len(calls) == 1  # and no one-by-one retry


def test_an_unseeded_task_may_split_and_keeps_its_own_environment(tmp_path, monkeypatch):
    ligand, receptor = tmp_path / "l.pdbqt", tmp_path / "r.pdbqt"
    for path in (ligand, receptor):
        path.write_text("ROOT\nATOM      1  C   LIG A   1       0.000   0.000   0.000  1.00  0.00     0.000 C\nENDROOT\n")
    run, calls = _fake_run(UNIDOCK_OUTPUT)
    monkeypatch.setattr(unidock, "_run_vina", run)
    monkeypatch.setattr(unidock, "unidock_command", lambda *_a, **_k: "unidock")
    monkeypatch.setattr(unidock, "check_receptor", lambda *_a, **_k: None)
    monkeypatch.setattr(unidock, "dockable_ligand", lambda path, **_k: path)
    monkeypatch.setattr(unidock, "_parse_vina_pose_energies", lambda **_k: [[-9.1, 0.0, 0.0]])
    monkeypatch.setattr(unidock, "_convert_vina_pdbqt_to_sdf", lambda **_k: None)
    rows = unidock.unidock_dock_runner({
        "pairs": [_pair(ligand, receptor, 1)], "output_dir": str(tmp_path / "out"), "seed": 0,
    })
    assert [row["score"] for row in rows] == [-9.1]
    assert calls[0]["env"] is None and "--seed" not in calls[0]["command"]


def test_the_split_error_names_the_groups_it_found():
    error = UniDockBatchSplit("x")
    assert isinstance(error, RuntimeError)
