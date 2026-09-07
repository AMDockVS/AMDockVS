"""Thin JSON/SDF bridge executed by pKasso's isolated Python interpreter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ph", required=True, type=float)
    parser.add_argument("--model", choices=("molgpka", "mixed"), default="molgpka")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--gpu", action="store_true")
    args = parser.parse_args()

    from pkasso import batch_protonate
    from rdkit import Chem

    entries = json.loads(Path(args.input).read_text(encoding="utf-8"))
    smiles = [str(item["smiles"]) for item in entries]
    model = None
    if args.model == "mixed":
        model = {"molgpka": {}, "unipka": {"folds": (1,), "gpu": bool(args.gpu)}}
    _smiles_out, molecules = batch_protonate(
        smiles,
        pH=float(args.ph),
        model=model,
        nthreads=max(1, int(args.threads)),
        cutoff_export=1.0,
        progress=False,
    )
    writer = Chem.SDWriter(str(Path(args.output)))
    try:
        for entry, candidates in zip(entries, molecules):
            if not candidates:
                continue
            molecule = Chem.Mol(candidates[0])
            molecule.SetProp("_AMDockID", str(entry["id"]))
            writer.write(molecule)
    finally:
        writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
