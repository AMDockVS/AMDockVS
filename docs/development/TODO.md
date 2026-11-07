# TODO

## MoleculeFragment

- UI fragment selection: there is no way to change the fragment from the UI; it is unclear whether
  this is needed (noted 2026-10-14). The API exists: `molecules.select_fragment(id, index)` extracts
  the fragment from the originally imported structure (`stored_path`) and writes it only then.
  Fragments are no longer materialized during import; `extra_data.fragmentation.components` lists
  `{fragment_index, role, smiles}`.
- Manual deduplication (`inchikey` / `canonical_smiles` columns already exist; not for shards).
  Projects imported before 2026-10-14 have these columns empty and need a backfill.

## Docking programs

### List
- AutoDock Vina (Vina + ad4, newer version)
- AutoDock-GPU
- SMINA (somewhat old and outdated; keeping it for now)
- GNINA
- DOCK6
- LeDock
- RxDock
- DiffDock

### Requirements
#### AutoDock Vina
- receptor pdbqt
- ligand pdbqt
- box

#### AutoDock-GPU
- receptor pdbqt
- ligand pdbqt
- box

#### SMINA (somewhat old and outdated; keeping it for now)
- receptor pdbqt
- ligand pdbqt
- box

#### GNINA
- receptor pdbqt
- ligand pdbqt
- box

#### DOCK6 (too complex for now)
- parameters
- receptor
  - Spheres file (*.sph)
  - Energy Grid (*.nrg, *.bmp, *.cnt)
  - box in pdb
- ligand mol2

#### LeDock
- receptor pdb
- ligand mol2
- box

#### RxDock (looks robust but quite complicated; excluded for now)

#### DiffDock
- receptor sequence (pdb optional)
- ligand smiles

## Receptor preparation (noted 2026-09-01)

Three bugs verified with 4UWH (p2). All three were **fixed on 2026-09-01**: the
`io/receptor_preview.py` writer no longer blindly applies its allowlist, and what reaches PDBQT is
now controlled in *Docking Studio > Receptors* ("Include structural waters" / "Include cofactors",
off by default). Covered by `test/test_receptor_hetero.py`.

1. ~~**`remove_cofactors` does nothing** - `io/receptor_preview.py:315`~~ **DONE 2026-09-01**
   `_components_to_remove()` already made the right decision, but the writer later applied
   `if not _is_polymer_residue(resname) and resname not in _COORDINATION_METAL_CODES: continue`,
   so cofactors were always deleted, whether or not the checkbox was selected. Meeko handles them
   without problems: tested with JXM (30 atoms) -> PDBQT with 31. *Fixed:* the filter now keeps
   everything in `component_keys`, and classifies by resname (not `residue_key`, because a
   biological assembly renames copied chains).

2. ~~**`remove_non_structural_waters` does nothing** - `io/receptor_preview.py:317`~~
   **DONE 2026-09-01**
   **All** waters were deleted, including structural waters marked `is_selected` by the preview.
   Meeko accepts them: with real coordinates they appear in PDBQT as `OW/HW1/HW2`, types `OA/HD`,
   and TIP3P charges (-0.412 / +0.206). Note when debugging: waters with invented coordinates
   that overlap the protein cause Meeko to fail with `adjacent_mol doesn't contain the mapped atoms`
   (spurious inter-residue bonds); this is not a water-support issue.

3. ~~**`retained_structural_waters` is misleading and nobody consumes it** -
   `io/receptor_preview.py:331`~~ **DONE 2026-09-01, partial**: it is no longer misleading (after
   (2) was fixed, retained waters are actually in the file), but the field **still has no consumer**.
   Remove it once we decide that nobody will read it.

### Related pending item (not a bug): metal-aware protonation

pdb2pqr 3.7.1 does not detect metals (PROPKA works only on the biomolecule). To freeze the state
of coordinating residues, two verified options are available:
- Rename the residue in the input: `HID/HIE/HIP`, `CYM`, `ASH`, `GLH`, `LYN` (tested: HIS A:370 ->
  HIE produces HIE, while the other 153 HIS remain HIS).
- `-i/--titrate_only "A:10,A:11"` to limit which residues are titratable.

Geometric detection already exists (`receptor_preview.py:518`: >=2 donors within 2.8 A of the metal).

## Diagrams and SMILES (noted 2026-09-16)

- Setting to export diagram images (PNG/SVG): prioritize a subprocess on the 2 spare logical CPUs
  vs. a deferred job in MF. Current state: JSON is generated in-process, images always in a
  subprocess (`export_diagram_image`). Decision pending; not implemented.
- The `smiles` column is not updated when chemistry changes (protonation, tautomer, 3D, etc.).
  Decide whether to refresh it at each step or generate it on demand from the saved SDF. For docking
  output, the Meeko SMILES is sufficient.
- Diagram export (`ms_contactmap export.py`), note only; current behavior is fine:
  - DONE 2026-09-16: SVG without rasterization (including the ligand) and `export_png(dpi=...)` /
    `--dpi`. AMDockVS still uses `scale=2` (192 dpi); decide whether to switch to `dpi=300`.
  - `QSvgGenerator` SVG is about 370 KB (ribbon is hundreds of segments, droplets have 4 shadow
    layers, ligand is repainted). This matters only for web; if needed, consider an efficient render
    mode.

## Table interactions (noted 2026-10-08)

Decision by Mario; no work until the orchestration engine (ms_flow) is finished.

- Interactions live in `interaction_results`. The docking checkbox (`compute_diagram`) currently
  only writes `<pose>.rank<N>.diagram.json` plus an image beside the pose, leaving the table empty;
  only `amdock_interactions_job` fills it.
- With the option checked, docking should run a single task that returns rows for both
  `docking_results` and `interaction_results` through a `graph_sink` (the child row references its
  pose, and the sink sets `docking_result_id`).
- Decided 2026-10-17: identify `interaction_results` by (receptor payload, ligand pose payload), not
  by `docking_result_id`. This lets a reference complex (cocrystal, with no docking row) have its
  diagram in the same table without a separate structure. Docking writes the row with payloads it
  already knows; no `graph_sink` relation to `docking_results` is needed.
- Do not filter by interactions (queries such as "poses with an H-bond to ASP86"); only save and
  display them. Perhaps later; do not design for it now.
- Do not generate the image during docking: the UI draws the diagram from the interactions. Add
  image generation only if it is needed later.
- The results panel reads interactions from the table (`list_interactions`), not from JSON
  (`pose_interactions`, `ui/catalog/results.py:864`); the file may not exist.
- The "Build diagram" button and `amdock_diagram_job` detect interactions but do not write them to
  the table; fix this.
- Depends on ms_contactmap (in development): detection-only input and faster SASA. Measured on
  2026-10-08 for one pose (1iep): detection 0.04 s; exposure 3.0 s; layout 0.15 s; image 1.8 s.
  The interaction job currently spends those 3 s because it calls `build_pose_diagram`
  (`docking/results/interactions.py:57`).

## Edit an imported receptor (noted 2026-10-15)

- Chains, assembly, altloc, waters, and cofactors are fixed at import; there is no action to change
  them. Reimporting the same file with different options creates a new variant (another storage
  key, `_receptor_import_variant`), rather than replacing the existing one. Binding sites can be
  edited afterward (Binding Sites tool).
- Everything needed to reprocess is already saved: the original copy (`original_path`) and the
  options used (`extra_data.structure.import_profile`).
- Proposed row action: "Edit import…" opens the import panel with that row filled in; on apply,
  choose "Edit a copy" (new variant) or "Replace" (same pattern as the Ketcher ligand editor).
  "Replace" warns that dependent dockings, binding sites, and preparations will be deleted.

## Duplicates in Ligands (noted 2026-10-17)

- Import does not detect duplicates today: importing the same compound twice creates two rows and
  docks it twice (this happened with intentionally duplicated data to enlarge the table).
- Do not check during import (per-molecule cost on the hot path). `molecules.inchikey` already
  exists and is indexed: duplicates can be found with `GROUP BY inchikey HAVING count(*) > 1`.
- Proposed action: "Find duplicates" in Ligands filters/selects repeated entries (like a scope),
  and the user decides whether to delete or keep them. Optional: show a warning after import if the
  query finds any. Not implemented.

## Cross-docking (noted 2026-10-17)

- Redocking docks each reference ligand into ITS receptor (the original complex pair).
  Cross-docking docks each reference ligand against receptors from OTHER complexes with the same
  target, to measure whether the protocol reproduces poses in a different protein conformation.
- Missing: select the receptor set (same binding sites / same protein), superpose each receptor on
  the original complex receptor so RMSD against the reference ligand is meaningful (today RMSD is
  measured in the frame of the docked receptor), and provide a matrix results view, ligand x
  receptor (success <= 2 A).
- Fits as another `run_kind` on the same docking engine. Not implemented.

## Docking storage (noted 2026-10-19)

From the benchmark `docs/benchmarks/vs_classic_2026-10-18/`. Today, 27 KB per ligand for 9 poses
(about 890 GB for 33 M). Per pose, `docking_results.metrics` contains 1199 B of JSON: 551 B for
the run, 352 B for the ligand, 247 B of derived score fields, and only 49 B for the pose.

- **Store run configuration once:** box, protocol, engine, backend, exhaustiveness, `run_id`, date,
  and receptor belong in a runs table (or in a volume if large); each result row references it by ID.
- **Move ligand properties out of `docking_results`:** MW, TPSA, cLogP, HBD/HBA, and heavy atoms
  are already in `molecules`. The ligand's 9 energies are repeated in each of its poses.
- **Score-derived fields (LE, pKi, BEI, SEI, LLE, etc.):** keep them because they are used for
  sorting and filtering. They are small, but would be better as columns than inside JSON. `lle` and
  `lipophilic_efficiency` are the same value twice.
- **Deep cleanup of duplicates or unused data:** repeated indexes
  (`idx_dr_ligand_molecule` = `ix_docking_results_ligand_molecule_id`,
  `idx_dr_receptor_molecule` = `ix_docking_results_receptor_molecule_id`), unused tables, and
  properties stored in 3 or 4 places.
- **Do we need 9 poses?** For VS, top 3 would probably be enough. Before deciding, measure with
  GNINA rescoring which original pose ranks best.
- **Pose compression:** each pose is its own payload (SDF with all H atoms, about 3.9 KB; zstd
  without a dictionary -> 848 B). Measured using 4446 poses: zstd-19 without a dictionary 784 B,
  with a trained dictionary (`PayloadStore.train_dictionary` already exists and AMDockVS does not
  use it) 566 B, all poses as one block 416 B. Storing topology once per ligand and only coordinates
  per pose: measured in `docs/benchmarks/vs_classic_2026-10-18/` (one payload per ligand = 50%;
  int16 at 1e-3 A = 33%; heavy atoms only = 20% of current size).
  Proposed (2026-10-19, undecided): the generic approach is **one payload per ligand with all its
  poses in today's format** (multi-molecule SDF, PDB/PDBQT with MODEL). Works for any molecule type
  and saves 50%. Lossless int32 is similar (49%), and a zstd dictionary adds nothing with grouped
  blocks (51%). int16 coordinates (33%) are worthwhile only for shard mode.
- **"Fire and forget" with a score cutoff**, as with shards: user sets a cutoff (e.g. -8.5 for Vina
  and derivatives) and poses for ligands worse than the cutoff (score > -8.5) are not stored on
  disk. Only their score record remains in the table. Not implemented.
