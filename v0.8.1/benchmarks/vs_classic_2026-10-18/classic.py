"""The AMDock 1.x-style script: read SDF, per molecule protonate + 3D, meeko and qvina as subprocesses, Pool(14)."""
import csv
import os
import subprocess
import sys
from multiprocessing import Pool
from pathlib import Path

from dimorphite_dl import protonate_smiles
from rdkit import Chem
from rdkit.Chem import AllChem

HERE = Path(__file__).parent
OUT = Path(sys.argv[1])
RECEPTOR = HERE / "1iep_receptor.pdbqt"
QVINA = "/home/mario/.amdockvs/tools/docking/qvina/2.1/qvina2.1"
BOX = dict(center_x=15.190, center_y=53.903, center_z=16.917, size_x=20, size_y=20, size_z=20)


def work(item):
    name, smiles = item
    try:
        prot = Chem.MolFromSmiles(protonate_smiles([smiles], ph_min=7.4, ph_max=7.4, precision=0.0, max_variants=1)[0])
        mol = Chem.AddHs(prot)
        params = AllChem.ETKDGv3()
        params.randomSeed = 0xF00D
        if AllChem.EmbedMolecule(mol, params) != 0:
            return name, None, "embed failed"
        sdf, pdbqt, out = OUT / f"{name}.sdf", OUT / f"{name}.pdbqt", OUT / f"{name}_out.pdbqt"
        Chem.MolToMolFile(mol, str(sdf))
        subprocess.run(["mk_prepare_ligand.py", "-i", sdf, "-o", pdbqt, "--rigid_macrocycles"], check=True, capture_output=True)
        args = [QVINA, "--receptor", RECEPTOR, "--ligand", pdbqt, "--out", out, "--cpu", "1",
                "--exhaustiveness", "8", "--num_modes", "9", "--seed", "0"]
        for key, value in BOX.items():
            args += [f"--{key}", str(value)]
        subprocess.run(args, check=True, capture_output=True)
        for line in out.read_text().splitlines():
            if line.startswith("REMARK VINA RESULT"):
                return name, float(line.split()[3]), ""
        return name, None, "no pose"
    except Exception as error:  # noqa: BLE001
        return name, None, str(error)[:200]


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    items = [(m.GetProp("Catalog ID") if m.HasProp("Catalog ID") else f"mol{i}", Chem.MolToSmiles(m))
             for i, m in enumerate(Chem.SDMolSupplier(str(HERE / os.environ.get("LIGS", "ligs500.sdf")))) if m is not None]
    with Pool(14) as pool, open(OUT / "scores.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "score", "error"])
        for row in pool.imap_unordered(work, items):
            writer.writerow(row)
