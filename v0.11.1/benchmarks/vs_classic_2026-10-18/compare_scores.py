"""Pair classic and AMDockVS best scores by SDF position (source_index); report coverage, correlation, top-50 overlap."""
import csv
import json
import sqlite3

from rdkit import Chem
from scipy.stats import pearsonr, spearmanr

ids = [m.GetProp("Catalog ID") for m in Chem.SDMolSupplier("/tmp/bench_vs/ligs500.sdf")]
classic = {r["name"]: float(r["score"]) for r in csv.DictReader(open("/tmp/bench_vs/out_classic/scores.csv")) if r["score"]}
# best rank-1 score per ligand straight from the project (top_hits counts poses, not ligands)
amdock = {ids[si]: s for si, s in sqlite3.connect("/tmp/bench_vs/out_amdock/project/project.db").execute(
    "select m.source_index, min(d.score) from docking_results d join molecules m on m.id = d.ligand_molecule_id "
    "where d.pose_rank = 1 group by m.id")}
both = sorted(set(classic) & set(amdock))
a, b = [classic[k] for k in both], [amdock[k] for k in both]
top = lambda d: set(sorted(d, key=d.get)[:50])
print(json.dumps({
    "classic_scored": len(classic), "amdock_scored": len(amdock), "paired": len(both),
    "classic_failures": [r["error"][:120] for r in csv.DictReader(open("/tmp/bench_vs/out_classic/scores.csv")) if r["error"]],
    "pearson": round(pearsonr(a, b)[0], 3), "spearman": round(spearmanr(a, b)[0], 3),
    "mean_classic": round(sum(a) / len(a), 2), "mean_amdock": round(sum(b) / len(b), 2),
    "identical_score": sum(x == y for x, y in zip(a, b)), "within_0.5": sum(abs(x - y) <= 0.5 for x, y in zip(a, b)),
    "top50_overlap": len(top(classic) & top(amdock)),
}, indent=1))
