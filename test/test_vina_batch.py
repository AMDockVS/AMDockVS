"""Ligands that share a receptor and a box dock in one `vina --batch` (the maps once); a ligand
the batch skipped docks alone, which says why it fails. Each ligand is reported as soon as the batch
has docked it."""
import shutil
from pathlib import Path

import pytest

from amdockvs.core.constants import vina_command
from amdockvs.docking.engines import vina

DATA = Path(__file__).resolve().parent / "data"
UNREADABLE = "ROOT\nATOM      1  C   LIG     1       0.000   0.000   0.000  0.00  0.00     0.000 Xx\nENDROOT\nTORSDOF 0\n"


@pytest.mark.skipif(not shutil.which(vina_command()), reason="the vina program is not installed")
def test_a_batch_docks_the_ligands_together_and_a_skipped_one_alone(tmp_path, monkeypatch):
    receptor = tmp_path / "site.pdbqt"  # the zinc example's site without its Zn: plain Vina scoring
    receptor.write_text("".join(line for line in (DATA / "1s63_zn_site.pdbqt").open() if " Zn" not in line[66:]))
    for name, text in {"a": (DATA / "1s63_ligand.pdbqt").read_text(), "b": UNREADABLE}.items():
        (tmp_path / f"{name}.pdbqt").write_text(text)
    shutil.copy(tmp_path / "a.pdbqt", tmp_path / "c.pdbqt")
    pairs = [{"ligand_id": index, "receptor_id": 9, "ligand_path": str(tmp_path / f"{name}.pdbqt"),
              "receptor_path": str(receptor), "exhaustiveness": 1, "num_modes": 2}
             for index, name in enumerate("abc", start=1)]
    commands, run = [], vina._run_vina
    monkeypatch.setattr(vina, "_run_vina", lambda command, *rest: commands.append(command) or run(command, *rest))
    events, done = [], []  # what the registry sees: rows, and progress past the reported pairs

    def rows_of(pair_rows):
        done.append(pair_rows[0]["ligand_molecule_id"])
        events.append(("row", len(commands)))

    def progress(fraction):
        events.append(("progress", len(done) + fraction))

    rows = vina.run_vina_docking_rows(pairs=pairs, output_dir=tmp_path / "out", box_center=[18.0, 134.0, -1.0],
                                      box_size=[15.0, 11.25, 18.75], seed=1, vina_command=vina_command(),
                                      pair_progress=progress, pair_callback=rows_of)

    assert [command.count("--batch") for command in commands] == [3, 0]  # the batch, then b alone
    scored = {row["ligand_molecule_id"] for row in rows if row["score"] is not None and row["score"] < -4}
    assert scored == {1, 3}
    (failed,) = [row for row in rows if row["ligand_molecule_id"] == 2]
    assert "Xx" in failed["error"]
    assert done == [1, 3, 2]  # the batch's ligands first, then b alone
    first_row, second_row = [index for index, event in enumerate(events) if event[0] == "row"][:2]
    assert events[first_row] == events[second_row] == ("row", 1)  # both before b docks alone
    assert any(kind == "progress" for kind, _ in events[first_row:second_row])  # c docked after a's row
    totals = [value for kind, value in events if kind == "progress"]
    assert totals == sorted(totals) and max(totals) <= 3  # progress never steps back
    assert len(list((tmp_path / "out").glob("*.dock.sdf"))) == 2
    assert not list((tmp_path / "out").glob("_batch_*"))  # the batch's folder is gone
