"""Ligands that share a receptor and a box dock in one `vina --batch` (the maps once); a ligand
the batch skipped docks alone, which says why it fails."""
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
    monkeypatch.setattr(vina, "_run_vina", lambda command, progress=None: commands.append(command) or run(command, progress))
    progress = []

    rows = vina.run_vina_docking_rows(pairs=pairs, output_dir=tmp_path / "out", box_center=[18.0, 134.0, -1.0],
                                      box_size=[15.0, 11.25, 18.75], seed=1, pair_progress=progress.append,
                                      vina_command=vina_command())

    assert [command.count("--batch") for command in commands] == [3, 0]  # the batch, then b alone
    scored = {row["ligand_molecule_id"] for row in rows if row["score"] is not None and row["score"] < -4}
    assert scored == {1, 3}
    (failed,) = [row for row in rows if row["ligand_molecule_id"] == 2]
    assert "Xx" in failed["error"]
    assert progress and max(progress) > 1  # a batch counts past its first ligand
    assert len(list((tmp_path / "out").glob("*.dock.sdf"))) == 2
    assert not list((tmp_path / "out").glob("_batch_*"))  # the batch's folder is gone
