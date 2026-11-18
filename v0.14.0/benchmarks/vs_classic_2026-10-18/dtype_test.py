"""Same poses, same topology: raw and zstd-19 bytes of the coordinates per dtype, plus max error."""
import sqlite3
from collections import defaultdict

import numpy as np
import zstandard as z
from rdkit import Chem

DB = "/tmp/bench_vs/out_amdock/project"
CENTER = np.array([15.190, 53.903, 16.917])
rows = sqlite3.connect(f"{DB}/project.db").execute(
    "select d.ligand_molecule_id, p.volume_id, p.offset, p.length from docking_results d "
    "join ms_payloads p on p.id = d.pose_payload_id order by d.ligand_molecule_id, d.pose_rank").fetchall()
dz, c19 = z.ZstdDecompressor(), z.ZstdCompressor(level=19)
blocks = defaultdict(list)
for lig, vol, off, length in rows:
    with open(f"{DB}/volumes/{vol:09d}.vol", "rb") as f:
        f.seek(off + 16)
        blocks[lig].append(dz.decompress(f.read(length), max_output_size=1 << 20).decode())


def int_fixed(x, dtype):
    q = np.round(x * 1e3)
    info = np.iinfo(dtype)
    assert q.min() >= info.min and q.max() <= info.max, dtype
    return q.astype(dtype), q.astype(dtype) / 1e3


encoders = {
    "float32 absolute": lambda xyz: (xyz.astype(np.float32), xyz.astype(np.float32)),
    "float32 from center": lambda xyz: ((xyz - CENTER).astype(np.float32), (xyz - CENTER).astype(np.float32) + CENTER),
    "float16 absolute": lambda xyz: (xyz.astype(np.float16), xyz.astype(np.float16)),
    "float16 from center": lambda xyz: ((xyz - CENTER).astype(np.float16), (xyz - CENTER).astype(np.float16) + CENTER),
    "int32 1e-3 absolute": lambda xyz: int_fixed(xyz, np.int32),
    "int32 1e-3 from center": lambda xyz: (lambda e, d: (e, d + CENTER))(*int_fixed(xyz - CENTER, np.int32)),
    "int16 1e-3 from center": lambda xyz: (lambda e, d: (e, d + CENTER))(*int_fixed(xyz - CENTER, np.int16)),
}
raw, packed, err = defaultdict(int), defaultdict(int), defaultdict(float)
spread = 0.0
n_atoms = 0
for sdfs in blocks.values():
    xyz = np.stack([Chem.MolFromMolBlock(b, removeHs=False, sanitize=False).GetConformer().GetPositions() for b in sdfs])
    n_atoms += xyz.shape[0] * xyz.shape[1]
    spread = max(spread, float(np.abs(xyz - CENTER).max()))
    raw["SDF text (coordinates only, 30 chars/atom)"] += xyz.shape[0] * xyz.shape[1] * 30
    for name, enc in encoders.items():
        stored, back = enc(xyz)
        raw[name] += stored.nbytes
        packed[name] += len(c19.compress(stored.tobytes()))
        err[name] = max(err[name], float(np.abs(back - xyz).max()))
print(f"{len(blocks)} ligands, {n_atoms} atom positions; farthest atom from box center {spread:.2f} A")
print(f"{'':44s}{'raw B/atom':>11s}{'zstd B/atom':>12s}{'max error A':>13s}")
for name in raw:
    print(f"{name:44s}{raw[name] / n_atoms:11.1f}{packed[name] / n_atoms if name in packed else float('nan'):12.2f}"
          f"{err[name] if name in err else 0:13.4f}")
