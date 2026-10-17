# Docking step by step: files vs volumes (2026-10-17)

Reference times of one small docking project, built and docked through the public runtime API
by `test/benchmarks/bench_docking_steps.py`, against the two storage generations.

- **files**: AMDockVS `a9a29c6` (main) + ms_flow `b371dbe` (main, 0.2.0). Poses and receptors as files on disk.
- **volumes**: AMDockVS `66c9e83` (`feat/volumes-integration`) + ms_flow `9719c98` (`feat/payload-volumes-v7`). Poses and receptors as payloads in the project's volumes.

Setup:

- 2 receptors × 10 ligands = 20 pairs.
- Receptors: 1iep (`data/docking/1iep_receptorH.pdb`, center 15.190, 53.903, 16.917), and 4O71 (center = centroid of the chain A CPB ligand, 24.659, 98.749, 31.691). Box 22 Å.
- Ligands: the first 10 records of `data/Enamine_H100K/Enamine_H100K.sdf`. They are 2D, so `generate_3d` is a real step.
- Docking: Vina (the API default), exhaustiveness 8, seed 1, the app's default executors.
- Runs alternated files/volumes, 3 each. A fresh project per run, under a temporary HOME, deleted afterwards.
- Machine: AMD Ryzen 7 3700X (8 cores / 16 threads), NVMe, no other load.
- Raw runs: `docking_steps_2026-10-17.jsonl`.

| step | files (s) | volumes (s) |
|---|---|---|
| create_project | 0.06 | 0.07 |
| import_receptors | 1.02 | 1.21 |
| import_ligands | 0.61 | 0.61 |
| generate_3d | 0.63 | 0.64 |
| prepare_receptors | 3.90 | 3.90 |
| prepare_ligands | 1.21 | 1.21 |
| set_grid | 0.03 | 0.03 |
| docking | 149.26 | 148.25 |
| read_poses | 0.10 | 0.05 |
| total | 156.86 | 155.77 |
| hits (poses) | 210 | 210 |
| project files | 118 | 18 |
| project size (MB) | 7.1 | 4.8 |
| bytes read by read_poses | 7.9 MB | 1.2 MB |

Median of 3 runs per method. Docking spread: files 146.8–150.9 s, volumes 145.6–154.3 s.

Reading the table:

- Both generations produce the same results: the same 210 poses and the same best scores (−10.77, −10.77, −10.74).
- Docking is ~95% of the wall time, and Vina's search sets it. Storage changes nothing there. The small difference is run-to-run noise.
- Every other step is within noise, except `import_receptors`: about +0.2 s in 2 of the 3 runs, likely from writing the receptors into the volumes.
- `read_poses` reads every hit's pose and each receptor once, which is what the viewer needs. It is 2× faster and reads 6× fewer bytes with volumes. In files, each pose was a whole multi-mode SDF read once per mode. Each volume payload holds only its own mode.
- The project has 100 fewer files and is 2.3 MB smaller at this size. The file-count gap grows with the number of pairs (see `bench_project_files.py`: 10 files for 100k results).
