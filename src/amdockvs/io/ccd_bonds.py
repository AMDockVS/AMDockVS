"""Bond orders for a cocrystal ligand, read from the entry mmCIF instead of guessed.

A deposited PDB carries no bond orders (CONECT is connectivity only), so RDKit reads every bond as
single: for 4UWH's JXM that turns a pyrimidinone into a saturated ring and a benzene into a
cyclohexane — MW 436.52 / 0 aromatic rings instead of 424.42 / 2. Geometric perception (what PyMOL
does) gets ~90% of bonds and only ~62% of whole ligands right, measured over 150 distinct PDB
components, so it is not good enough to feed a descriptor column either.

RCSB entry mmCIF files embed the CCD subset for every component as ``_chem_comp_bond``, which is
the deposited answer rather than a guess. That block is present in the .cif the user already hands
the importer, so this reads it back instead of perceiving anything.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_CCD_ORDER = {"sing": 1, "doub": 2, "trip": 3, "quad": 4}
_CIF_SUFFIXES = {".cif", ".mmcif"}
_CCD_URL = "https://files.rcsb.org/ligands/download/{resname}.cif"
# Names that are never the ligand: solvent, buffer and the ions that share a HETATM record with it.
_NOT_A_LIGAND = frozenset({
    "HOH", "DOD", "WAT", "SO4", "PO4", "GOL", "EDO", "PEG", "PG4", "1PE", "MPD", "TRS", "ACT",
    "DMS", "IOD", "FMT", "BME", "CIT", "EPE", "MES", "NA", "K", "CL", "BR", "MG", "CA", "ZN",
    "MN", "FE", "CU", "NI", "CO", "CD", "HG",
})


def ccd_component_file(resname: str, *, allow_fetch: bool = True) -> Path | None:
    """The standalone CCD entry for a residue name, from the local cache or from the RCSB.

    A deposited PDB has no bond orders but it does have the residue name, and the residue name is
    the CCD key — so the entry mmCIF is a convenience, not a requirement. Each component is ~10 KB
    and never changes, so one fetch per ligand code is cached forever.
    """
    code = str(resname or "").strip().upper()
    if not code or not code.isalnum():
        return None
    cache = _ccd_cache_dir() / f"{code}.cif"
    if cache.exists():
        return cache
    if not allow_fetch or os.environ.get("AMDOCK_OFFLINE"):
        return None
    try:
        request = urllib.request.Request(_CCD_URL.format(resname=code), headers={"User-Agent": "AMDockVS"})
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not payload.startswith(b"data_"):
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(payload)
    return cache


def _ccd_cache_dir() -> Path:
    configured = str(os.environ.get("AMDOCK_CCD_CACHE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    xdg = str(os.environ.get("XDG_CACHE_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "AMDockVS" / "ccd"


def resname_of(structure_path: Path) -> str:
    """The one residue name in a single-ligand PDB/PDBQT, or "" if it is not unambiguous."""
    names: set[str] = set()
    try:
        with structure_path.open("r", errors="replace") as handle:
            for line in handle:
                if line.startswith(("ATOM  ", "HETATM")):
                    name = line[17:20].strip().upper()
                    if name and name not in _NOT_A_LIGAND:
                        names.add(name)
    except OSError:
        return ""
    return names.pop() if len(names) == 1 else ""


def ligand_with_ccd_bonds(
    structure_path: Path, *, source_file: Path | None = None, resname: str = ""
) -> Any | None:
    """The ligand in ``structure_path`` with deposited bond orders, or None if nothing can say.

    Bonds come from the entry mmCIF when the caller has one, else from the CCD component for the
    residue name (which the file itself supplies when the caller does not).
    """
    code = str(resname or "").strip().upper() or resname_of(structure_path)
    if not code:
        return None
    for lookup in (lambda: source_file, lambda: ccd_component_file(code)):
        cif = lookup()
        if cif is None:
            continue
        mol = ligand_from_cif(cif, structure_path, code)
        if mol is not None:
            return mol
    return None


def ligand_from_cif(source_file: Path, ligand_pdb: Path, resname: str) -> Any | None:
    """Rebuild the ligand as an RDKit Mol with real bond orders, or None if the CIF can't say.

    ``ligand_pdb`` supplies the atoms (already chain/resseq-filtered by the extraction step);
    ``source_file`` supplies their bonds. Returns None whenever anything is missing or the result
    does not sanitize — a wrong molecule is worse than no molecule once a filter reads its MW.
    """
    if source_file.suffix.lower() not in _CIF_SUFFIXES or not ligand_pdb.exists():
        return None
    bonds = _chem_comp_bonds(source_file, resname)
    if not bonds:
        return None

    import gemmi
    from rdkit import Chem

    try:
        structure = gemmi.read_structure(str(ligand_pdb))
        structure.remove_hydrogens()
        structure.remove_alternative_conformations()
    except (RuntimeError, ValueError, OSError):
        return None
    atoms = [atom for chain in structure[0] for residue in chain for atom in residue]
    names = [atom.name for atom in atoms]
    if not names or len(set(names)) != len(names):  # duplicate names: bonds cannot be matched
        return None

    editable = Chem.RWMol()
    conformer = Chem.Conformer(len(atoms))
    for position, atom in enumerate(atoms):
        editable.AddAtom(Chem.Atom(atom.element.name))
        conformer.SetAtomPosition(position, (atom.pos.x, atom.pos.y, atom.pos.z))
    index = {name: position for position, name in enumerate(names)}

    matched = 0
    for first, second, order, aromatic in bonds:
        if first not in index or second not in index:
            continue  # a bond to a hydrogen or to an atom the extraction dropped
        editable.AddBond(
            index[first],
            index[second],
            Chem.BondType.AROMATIC if aromatic else Chem.BondType.values[_CCD_ORDER.get(order, 1)],
        )
        matched += 1
    if matched < len(atoms) - 1:  # fewer bonds than a spanning tree: wrong component or partial CIF
        return None
    for atom in editable.GetAtoms():
        if any(bond.GetBondType() == Chem.BondType.AROMATIC for bond in atom.GetBonds()):
            atom.SetIsAromatic(True)

    mol = editable.GetMol()
    mol.AddConformer(conformer)
    try:
        Chem.SanitizeMol(mol)
    except Exception:  # noqa: BLE001 - RDKit raises a family of unrelated sanitization errors
        return None
    return mol


def _chem_comp_bonds(source_file: Path, resname: str) -> list[tuple[str, str, str, bool]]:
    import gemmi

    wanted = str(resname or "").strip().upper()
    if not wanted:
        return []
    try:
        block = gemmi.cif.read(str(source_file)).sole_block()
    except (RuntimeError, ValueError, OSError):
        return []
    columns = ["comp_id", "atom_id_1", "atom_id_2", "value_order", "pdbx_aromatic_flag"]
    return [
        (row[1].strip('"'), row[2].strip('"'), row[3].lower(), row[4] == "Y")
        for row in block.find("_chem_comp_bond.", columns)
        if row[0].strip('"').upper() == wanted
    ]
