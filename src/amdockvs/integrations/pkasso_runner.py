"""Thin JSON/SDF bridge executed by pKasso's isolated Python interpreter.

Standalone on purpose: that environment has pKasso, PyTorch and RDKit but not AMDockVS, so this
file imports only the standard library and those. Called as

    <env python> pkasso_runner.py --input in.json --output out.sdf --ph 7.4 [--model mixed --gpu]
    <env python> pkasso_runner.py --probe-gpu
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def gpu_support() -> dict:
    """Whether this interpreter's PyTorch can run on the GPU here, and what it found.

    The answer is a tiny real operation on the GPU, not CUDA's compatibility rules worked out
    again: it is the only test that holds for every card, driver and PyTorch build. CUDA can
    report itself available and still run nothing -- a card outside the architectures the build
    has kernels for ("no kernel image is available"), a driver older than the build -- and then
    every molecule fails.
    """
    found: dict = {"usable": False}
    try:
        import torch
    except Exception as exc:  # noqa: BLE001 - whatever stops it, the answer is "no GPU"
        found["error"] = f"PyTorch does not load: {exc}"
        return found
    found["torch"] = str(torch.__version__)
    found["archs"] = [str(arch) for arch in torch.cuda.get_arch_list()]
    if not torch.cuda.is_available():
        return found
    major, minor = torch.cuda.get_device_capability(0)
    found["name"] = str(torch.cuda.get_device_name(0))
    found["capability"] = f"{major}.{minor}"
    try:
        (torch.ones(2, device="cuda") * 2).sum().item()
    except Exception as exc:  # noqa: BLE001 - "no kernel image", a broken driver: all mean CPU
        found["error"] = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return found
    found["usable"] = True
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--ph", type=float)
    parser.add_argument("--model", choices=("molgpka", "mixed"), default="molgpka")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--probe-gpu", action="store_true", help="print gpu_support() as JSON and exit")
    args = parser.parse_args()
    if args.probe_gpu:
        print(json.dumps(gpu_support()))
        return 0
    if not args.input or not args.output or args.ph is None:
        parser.error("--input, --output and --ph are required")

    gpu = bool(args.gpu)
    if gpu and args.model == "mixed":
        # Asked for a GPU this PyTorch cannot run on: Uni-pKa on the CPU gives the same states,
        # only slower, where the GPU would fail every molecule. This runs where the job does,
        # so it holds on a worker whose card differs from the machine that submitted it.
        support = gpu_support()
        if not support["usable"]:
            why = support.get("error") or "PyTorch sees no CUDA GPU"
            card = f"{support['name']}: " if support.get("name") else ""
            print(f"pKasso: the GPU cannot be used ({card}{why}); running Uni-pKa on the CPU.",
                  file=sys.stderr)
            gpu = False

    from pkasso import batch_protonate
    from rdkit import Chem

    entries = json.loads(Path(args.input).read_text(encoding="utf-8"))
    smiles = [str(item["smiles"]) for item in entries]
    model = None
    if args.model == "mixed":
        model = {"molgpka": {}, "unipka": {"folds": (1,), "gpu": gpu}}
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
