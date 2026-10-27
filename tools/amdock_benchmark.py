from __future__ import annotations

import argparse
import json
from pathlib import Path

from amdockvs import AMDockVSRuntime
from screening_benchmark import BenchmarkConfig, run_screening_benchmark


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amdockvs-benchmark")
    parser.add_argument("--workdir", required=True, help="Working directory for generated inputs and project data.")
    parser.add_argument("--policy", default="balanced", help="Batch policy: latency, balanced or throughput.")
    parser.add_argument("--ligands", type=int, default=240, help="Total ligand count to generate.")
    parser.add_argument("--receptors", type=int, default=2, help="Total receptor count to generate.")
    parser.add_argument("--ligand-files", type=int, default=2, help="How many ligand files to generate.")
    parser.add_argument("--import-executor", default="thread")
    parser.add_argument("--descriptor-executor", default="thread")
    parser.add_argument("--docking-executor", default="thread")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--poll", type=float, default=0.05)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    runtime = AMDockVSRuntime()
    try:
        result = run_screening_benchmark(
            runtime,
            config=BenchmarkConfig(
                ligand_count=max(1, int(args.ligands)),
                receptor_count=max(1, int(args.receptors)),
                ligand_file_count=max(1, int(args.ligand_files)),
                policy=str(args.policy),
                import_executor_name=str(args.import_executor),
                descriptor_executor_name=str(args.descriptor_executor),
                docking_executor_name=str(args.docking_executor),
                timeout_s=float(args.timeout),
                poll_s=float(args.poll),
            ),
            workdir=Path(args.workdir),
        )
    finally:
        runtime.shutdown()

    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
