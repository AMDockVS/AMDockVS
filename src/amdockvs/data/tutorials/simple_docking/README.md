# Simple docking example

Extracted from the supplied `Vps34_human_4oys.pdb` (PDB 4OYS).
`example.json` records the input checksum, atom counts, selection, and box.

- `Vps34_receptor.pdb`: chain A ATOM records in their original order and coordinates.
  The bound ligand, sulfate ions, and waters are omitted.
- `SAR405.sdf`: residue 1TT A 905, with the 30 deposited heavy-atom coordinates.
  Bond orders come from the RCSB Chemical Component Dictionary entry 1TT.
  RDKit adds hydrogens at the deposited coordinates and assigns stereochemistry
  from that geometry. No energy minimization is performed during extraction.
- The 22 Å cube is centered on the midpoint of the bound ligand's coordinate
  bounds. The tutorial gives these values to the user, who enters them in the
  Grid Box controls, saves the site, and makes it active before preparation.

The tutorial starts on the welcome page. The user opens Projects, clicks New
project, enters a name, chooses a folder with Browse, and clicks Save. Only then
are the bundled examples copied into the new project’s `inputs` folder.

The import dialogs start empty. The user clicks Add files, chooses the receptor
or ligand from the practice project's `inputs` folder, reviews it, and imports
it. The tutorial supplies instructions and validates completion; it does not
select files, choose programs, fill box coordinates, or launch calculations.

The supplied PDB has a CAVEAT reporting wrong chirality at atom C6 of 1TT A 905.
The example retains the supplied geometry, including that caveat; it is a UI
practice dataset, not a corrected structure or an affinity validation benchmark.

The distributed PDB and SDF are sufficient at runtime; no CCD or structure
download is required. Source: https://www.rcsb.org/structure/4OYS
