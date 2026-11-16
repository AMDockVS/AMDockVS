"""Store docking poses as topology once per ligand + coordinates per pose; compare sizes with today's payloads."""
import sqlite3
from collections import defaultdict

import numpy as np
import zstandard as z
from rdkit import Chem

DB = "/tmp/bench_vs/out_amdock/project"
rows = sqlite3.connect(f"{DB}/project.db").execute(
    "select d.ligand_molecule_id, d.pose_rank, p.volume_id, p.offset, p.length from docking_results d "
    "join ms_payloads p on p.id = d.pose_payload_id order by 1, 2").fetchall()
dz = z.ZstdDecompressor()
poses = defaultdict(list)  # ligand -> [(stored_len, sdf_bytes)]
for lig, _, vol, off, length in rows:
    with open(f"{DB}/volumes/{vol:09d}.vol", "rb") as f:
        f.seek(off + 16)
        stored = f.read(length)
    poses[lig].append((length, dz.decompress(stored, max_output_size=1 << 20)))

c19 = z.ZstdCompressor(level=19)
size = defaultdict(int)
mismatch = worst = 0
err = {}
for lig, items in poses.items():
    mols = [Chem.MolFromMolBlock(b.decode(), removeHs=False, sanitize=False) for _, b in items]
    topo = [(a.GetAtomicNum(), a.GetFormalCharge()) for a in mols[0].GetAtoms()]
    if any([(a.GetAtomicNum(), a.GetFormalCharge()) for a in m.GetAtoms()] != topo for m in mols):
        mismatch += 1
    xyz = np.stack([m.GetConformer().GetPositions() for m in mols])  # poses x atoms x 3
    heavy = np.array([a.GetAtomicNum() > 1 for a in mols[0].GetAtoms()])
    q4 = np.round(xyz * 1e4).astype(np.int32)  # SDF keeps 4 decimals: lossless
    q3 = np.round(xyz * 1e3).astype(np.int32)  # PDBQT precision
    worst = max(worst, float(np.abs(q4 / 1e4 - xyz).max()))
    # topology = first pose's mol block with coordinates zeroed, written once per ligand
    blank = Chem.Mol(mols[0])
    for i in range(blank.GetNumAtoms()):
        blank.GetConformer().SetAtomPosition(i, (0, 0, 0))
    topo_bytes = Chem.MolToMolBlock(blank, kekulize=False).encode()
    size["A today: 1 sdf payload per pose"] += sum(n for n, _ in items)
    size["B all sdf of the ligand, 1 zstd"] += len(c19.compress(b"".join(b for _, b in items)))
    size["C topology + float32 xyz, 1 zstd"] += len(c19.compress(topo_bytes + xyz.astype(np.float32).tobytes()))
    size["D topology + int32 1e-4 A (lossless)"] += len(c19.compress(topo_bytes + q4.tobytes()))
    size["E topology + int32 1e-3 A"] += len(c19.compress(topo_bytes + q3.tobytes()))
    size["F int32 1e-4, coords only (topology = input)"] += len(c19.compress(q4.tobytes()))
    center = np.array([15.190, 53.903, 16.917])
    r3 = np.round((xyz - center) * 1e3)
    assert np.abs(r3).max() < 32767
    i16 = r3.astype(np.int16)
    shuf = lambda a: a.view(np.uint8).reshape(-1, a.itemsize).T.tobytes()  # byte planes
    size["H int16 1e-3 A from box center (+topology)"] += len(c19.compress(topo_bytes + i16.tobytes()))
    size["I H + byte shuffle"] += len(c19.compress(topo_bytes + shuf(i16)))
    size["J I, heavy atoms only"] += len(c19.compress(topo_bytes + shuf(np.ascontiguousarray(i16[:, heavy]))))
    size["K J, coords only (topology = input)"] += len(c19.compress(shuf(np.ascontiguousarray(i16[:, heavy]))))
    f16a, f16r = xyz.astype(np.float16), (xyz - center).astype(np.float16)
    err["float16 absolute"] = max(err.get("float16 absolute", 0), float(np.abs(f16a - xyz).max()))
    err["float16 from box center"] = max(err.get("float16 from box center", 0), float(np.abs(f16r + center - xyz).max()))
    err["int16 1e-3 from box center"] = max(err.get("int16 1e-3 from box center", 0), float(np.abs(i16 / 1e3 + center - xyz).max()))
    size["L float16 absolute (+topology)"] += len(c19.compress(topo_bytes + f16a.tobytes()))
    size["M float16 from box center (+topology)"] += len(c19.compress(topo_bytes + f16r.tobytes()))
    size["G int32 1e-3, heavy atoms only (+topology)"] += len(c19.compress(topo_bytes + q3[:, heavy].tobytes()))

n_lig, n_pose = len(poses), len(rows)
print(f"ligands {n_lig}, poses {n_pose}, topology mismatches between poses {mismatch}, max quantization error {worst:.1e} A")
base = size["A today: 1 sdf payload per pose"]
for k, v in size.items():
    print(f"{k:46s} {v / n_lig:7.0f} B/ligand {v / n_pose:5.0f} B/pose  {v / base:5.0%}")
for k, v in err.items():
    print(f"max error {k:28s} {v:.4f} A")
