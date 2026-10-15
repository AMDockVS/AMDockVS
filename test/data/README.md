ccd_ligand.{cif,pdb}: 4UWH cocrystal JXM — the _chem_comp_bond loop trimmed out of the RCSB entry, plus the extracted ligand as the importer stores it. Fixture for test_cocrystal_descriptors.py.

1s63_zn_site.pdbqt, 1s63_ligand.pdbqt: farnesyltransferase zinc site from the AutoDock-Vina zinc example (Apache-2.0, commit 3c65c0b): the example's receptor PDBQT trimmed to the residues within 18 Å of its box center (Zn kept), and its ligand prepared with Meeko (H added). Fixture for the zinc test in test_docking_methods.py.
