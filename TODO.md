# TODO

What is still open. Anything finished, dropped or already built is in the git history rather
than here.

## Molecules

- **Fragment selection from the UI.** `molecules.select_fragment(id, index)` exists and
  extracts the fragment from the originally imported structure, writing it only then;
  `extra_data.fragmentation.components` lists `{fragment_index, role, smiles}`. Nothing in the
  UI calls it, and whether it is wanted at all is undecided (noted 2026-10-14).
- **Manual deduplication**, with a backfill of `inchikey` and `canonical_smiles` for projects
  imported before 2026-10-14, where those columns are empty. Not for shards.
- **The `smiles` column is not refreshed after a chemistry step.** It is written once at
  import (`molecules/storage.py`); protonation, tautomer and 3D steps write new structures
  without updating it, so the column can disagree with the structure it describes. Open
  question: refresh it at each step, or generate it on demand from the saved SDF. For docking
  output the Meeko SMILES is enough.
- **Editing a receptor after import** (residue or chain level) has no path in the UI.

## Protein preparation

- **Metal-aware protonation.** pdb2pqr 3.7.1 does not detect metals: PROPKA titrates the
  biomolecule only. Two options are verified for freezing the state of coordinating residues:
  rename the residue in the input (`HID`/`HIE`/`HIP`, `CYM`, `ASH`, `GLH`, `LYN` — tested:
  HIS A:370 to HIE produces HIE while the other 153 HIS remain HIS), or pass
  `--titrate_only "A:10,A:11"` to limit which residues are titratable.
- **`retained_structural_waters` has no consumer.** `io/receptor_preview.py` writes it and
  nothing reads it. Remove the field once it is settled that nothing will.

## Results

- **How diagram images get exported is undecided.** The images are written by a subprocess
  (`export_diagram_image`) while the JSON is built in-process. Whether to keep claiming the
  two spare logical CPUs for that subprocess or to defer the work to a job in MolSuite Flow
  has not been settled, and no setting exposes the choice.
