"""One reader for every molecule file AMDock accepts, and the canonical format to store it in.

Before this, fourteen modules each dispatched on ``path.suffix`` with a different subset of formats
and a different idea of what to do with it. Two consequences worth naming, both measured:

* ``.pdbqt`` was read with ``Chem.MolFromPDBFile``, which returns None — the element column holds
  AutoDock types (``A``, ``NA``, ``OA``, ``HD``), not elements. Every PDBQT read silently failed.
  Those types are chemistry, not noise, so they are read back rather than discarded.
* ``.pdb`` was read straight, which loses all bond orders. Against the CCD over 150 distinct
  deposited ligands: RDKit alone recovers the right molecule 3% of the time, PyMOL's geometric
  perception 41%, OpenBabel 65%. Looking the residue name up in the CCD recovers 100%, so this
  reads rather than perceives, and only falls back to a plain parse for a name the CCD lacks.

``.cif``/``.sdf`` are the canonical stored formats (see :func:`canonical_suffix`) because they are
the only two here that carry bond orders for their molecule class without a dictionary lookup.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Aliases first: everything downstream reasons about the canonical spelling only.
SUFFIX_ALIASES = {
    ".ent": ".pdb",      # the PDB archive's own extension for the same bytes
    ".pdb1": ".pdb",     # biological assembly
    ".sd": ".sdf",
    ".mdl": ".mol",
    ".mmcif": ".cif",
    ".cif.gz": ".cif",
    ".smiles": ".smi",
}
READABLE_SUFFIXES = frozenset({".sdf", ".mol", ".mol2", ".pdb", ".pdbqt", ".cif", ".smi"})
#: What a molecule of each kind is stored as once imported. Anything else is a conversion input.
CANONICAL_SUFFIXES = frozenset({".sdf", ".cif"})
#: Qt file-dialog filter. Table-ish text formats are import-only (SMILES columns), not readers.
QT_FILE_FILTER = (
    "Molecule files (*.sdf *.sd *.mol *.mdl *.mol2 *.pdb *.ent *.pdb1 *.pdbqt *.cif *.mmcif "
    "*.smi *.smiles *.txt *.csv *.tsv);;All files (*)"
)

# AutoDock atom types are not element symbols: NA is an acceptor nitrogen, not sodium, and A is an
# aromatic carbon. Anything unlisted falls back to its first letter, which is right for C/N/O/S/P/H.
_AUTODOCK_ELEMENTS = {
    "A": "C", "C": "C", "N": "N", "NA": "N", "NS": "N", "OA": "O", "OS": "O",
    "SA": "S", "S": "S", "HD": "H", "H": "H", "F": "F", "CL": "Cl", "BR": "Br",
    "I": "I", "P": "P", "MG": "Mg", "ZN": "Zn", "CA": "Ca", "FE": "Fe", "MN": "Mn",
    "CU": "Cu", "K": "K", "W": "H",  # W = the dummy water hydrogen AutoDock uses
}


def normalized_suffix(path: str | Path) -> str:
    """``.ent`` -> ``.pdb``, ``.sd`` -> ``.sdf``, and so on. Lowercased."""
    name = Path(path).name.lower()
    for alias, canonical in SUFFIX_ALIASES.items():
        if name.endswith(alias):
            return canonical
    return Path(name).suffix


def is_readable(path: str | Path) -> bool:
    return normalized_suffix(path) in READABLE_SUFFIXES


def canonical_suffix(molecule_kind: str) -> str:
    """Where a molecule of this kind belongs: SDF for small molecules, mmCIF for everything else.

    A small molecule is a complete chemical graph and SDF stores it losslessly in one small file.
    A receptor is a structure — chains, residues, altlocs, assemblies, symmetry — and mmCIF is the
    only format here that keeps all of it.
    """
    from amdockvs.core.vocab import MoleculeType

    return ".sdf" if str(molecule_kind) == MoleculeType.SMALL_MOLECULE else ".cif"


def read_mol(
    path: str | Path,
    *,
    sanitize: bool = True,
    remove_hs: bool = False,
    index: int = 0,
    resname: str = "",
) -> Any | None:
    """One RDKit Mol from any supported file, or None.

    ``index`` picks a record out of a multi-record file (an SDF, or a multi-pose docking output).
    ``resname`` overrides the CCD lookup key; by default it is read from the file.
    """
    source = Path(path).expanduser()
    if not source.exists():
        return None
    suffix = normalized_suffix(source)
    reader = {
        ".sdf": _read_sdf, ".mol": _read_mol_block, ".mol2": _read_mol2,
        ".pdb": _read_pdb, ".pdbqt": _read_pdbqt, ".cif": _read_cif, ".smi": _read_smi,
    }.get(suffix)
    if reader is None:
        return None
    try:
        return reader(source, sanitize=sanitize, remove_hs=remove_hs, index=index, resname=resname)
    except Exception:  # noqa: BLE001 - RDKit/gemmi/meeko each raise their own unrelated families
        return None


def read_mols(path: str | Path, *, sanitize: bool = True, remove_hs: bool = False) -> Iterator[Any]:
    """Every record in the file. Single-record formats yield at most one."""
    source = Path(path).expanduser()
    if normalized_suffix(source) == ".sdf":
        from rdkit import Chem

        for mol in Chem.SDMolSupplier(str(source), sanitize=sanitize, removeHs=remove_hs):
            if mol is not None:
                yield mol
        return
    mol = read_mol(source, sanitize=sanitize, remove_hs=remove_hs)
    if mol is not None:
        yield mol


# --- per-format readers -------------------------------------------------------------------------


def _read_sdf(path: Path, *, sanitize: bool, remove_hs: bool, index: int, resname: str):
    from rdkit import Chem

    supplier = Chem.SDMolSupplier(str(path), sanitize=sanitize, removeHs=remove_hs)
    position = max(0, int(index))
    if supplier is None or len(supplier) <= position:
        return None
    return supplier[position]


def _read_mol_block(path: Path, *, sanitize: bool, remove_hs: bool, index: int, resname: str):
    from rdkit import Chem

    return Chem.MolFromMolFile(str(path), sanitize=sanitize, removeHs=remove_hs)


def _read_mol2(path: Path, *, sanitize: bool, remove_hs: bool, index: int, resname: str):
    from rdkit import Chem

    return Chem.MolFromMol2File(str(path), sanitize=sanitize, removeHs=remove_hs)


def _read_smi(path: Path, *, sanitize: bool, remove_hs: bool, index: int, resname: str):
    from rdkit import Chem

    for position, line in enumerate(path.read_text(errors="replace").splitlines()):
        token = line.split()[0] if line.split() else ""
        if token and position >= max(0, int(index)):
            return Chem.MolFromSmiles(token, sanitize=sanitize)
    return None


def _read_pdb(path: Path, *, sanitize: bool, remove_hs: bool, index: int, resname: str):
    from rdkit import Chem

    from amdockvs.io.ccd_bonds import ligand_with_ccd_bonds

    mol = ligand_with_ccd_bonds(path, resname=resname)
    if mol is not None:
        return Chem.RemoveHs(mol) if remove_hs else mol
    # No CCD entry: a receptor, a multi-component file, or a ligand a tool named UNL. Bond orders
    # are not recoverable here — see the module docstring for what perception would buy.
    return Chem.MolFromPDBFile(str(path), sanitize=sanitize, removeHs=remove_hs)


def _read_cif(path: Path, *, sanitize: bool, remove_hs: bool, index: int, resname: str):
    """Coordinates via gemmi (RDKit has no mmCIF reader), bond orders via _chem_comp_bond."""
    from rdkit import Chem

    from amdockvs.io.ccd_bonds import ligand_from_cif

    with tempfile.TemporaryDirectory() as scratch:
        scratch_pdb = Path(scratch) / "structure.pdb"
        if not to_pdb(path, scratch_pdb):
            return None
        code = str(resname or "").strip().upper()
        if not code:
            from amdockvs.io.ccd_bonds import resname_of

            code = resname_of(scratch_pdb)
        if code:
            mol = ligand_from_cif(path, scratch_pdb, code)
            if mol is not None:
                return Chem.RemoveHs(mol) if remove_hs else mol
        return Chem.MolFromPDBFile(str(scratch_pdb), sanitize=sanitize, removeHs=remove_hs)


def _read_pdbqt(path: Path, *, sanitize: bool, remove_hs: bool, index: int, resname: str):
    """Exact via meeko's ``REMARK SMILES``; otherwise rebuilt from the AutoDock atom types."""
    from rdkit import Chem

    text = path.read_text(errors="replace")
    if "REMARK SMILES" in text:
        try:
            from meeko import PDBQTMolecule, RDKitMolCreate

            molecules = RDKitMolCreate.from_pdbqt_mol(
                PDBQTMolecule(text, is_dlg=False, skip_typing=True)
            )
            candidates = [mol for mol in molecules if mol is not None]
            if candidates:
                mol = candidates[min(max(0, int(index)), len(candidates) - 1)]
                return Chem.RemoveHs(mol) if remove_hs else mol
        except Exception:  # noqa: BLE001 - fall through to the type-based path
            pass
    with tempfile.TemporaryDirectory() as scratch:
        scratch_pdb = Path(scratch) / "converted.pdb"
        scratch_pdb.write_text(pdbqt_to_pdb_text(text))
        mol = _read_pdb(scratch_pdb, sanitize=sanitize, remove_hs=False, index=index, resname=resname)
        if mol is not None and mol.GetNumAtoms() == len(_autodock_types(text)):
            typed = _apply_autodock_types(mol, _autodock_types(text))
            if typed is not None:
                mol = typed
        return Chem.RemoveHs(mol) if (mol is not None and remove_hs) else mol


def _autodock_types(text: str) -> list[str]:
    return [
        line[77:79].strip().upper()
        for line in text.splitlines()
        if line.startswith(("ATOM  ", "HETATM"))
    ]


def _apply_autodock_types(mol: Any, types: list[str]) -> Any | None:
    """Recover bond orders from the AutoDock typing, which is chemistry and not just elements.

    ``A`` is an aromatic carbon and ``OA``/``SA`` are acceptors, so a terminal acceptor with no
    polar hydrogen on it is a carbonyl. Those two rules alone rebuild the molecule exactly for a
    PDBQT that no ``REMARK SMILES`` came with. Returns None if the result does not sanitize.
    """
    from rdkit import Chem

    editable = Chem.RWMol(mol)
    try:
        Chem.SanitizeMol(
            editable, Chem.SANITIZE_ALL ^ Chem.SANITIZE_KEKULIZE ^ Chem.SANITIZE_SETAROMATICITY
        )
    except Exception:  # noqa: BLE001 - a broken connectivity is not worth an abort
        return None

    aromatic = {position for position, kind in enumerate(types) if kind == "A"}
    for ring in editable.GetRingInfo().AtomRings():
        # Half the ring typed A is enough: AutoDock has no aromatic type for ring heteroatoms.
        if sum(1 for position in ring if position in aromatic) * 2 < len(ring):
            continue
        for position in ring:
            editable.GetAtomWithIdx(position).SetIsAromatic(True)
        for first, second in zip(ring, ring[1:] + ring[:1]):
            bond = editable.GetBondBetweenAtoms(first, second)
            if bond is not None:
                bond.SetBondType(Chem.BondType.AROMATIC)

    for position, kind in enumerate(types):
        if kind not in {"OA", "SA"}:
            continue
        atom = editable.GetAtomWithIdx(position)
        neighbours = list(atom.GetNeighbors())
        if len(neighbours) != 1:
            continue  # an ether or hydroxyl bridge, not a carbonyl
        if any(types[n.GetIdx()] == "HD" for n in neighbours if n.GetSymbol() == "H"):
            continue  # carries its own polar hydrogen: it is a donor, so single
        bond = editable.GetBondBetweenAtoms(position, neighbours[0].GetIdx())
        if bond is not None:
            bond.SetBondType(Chem.BondType.DOUBLE)

    result = editable.GetMol()
    try:
        Chem.SanitizeMol(result)
    except Exception:  # noqa: BLE001 - the plain read is still better than nothing
        return None
    return result


# --- conversion ---------------------------------------------------------------------------------


def pdbqt_to_pdb_text(text: str) -> str:
    """Drop the AutoDock branch records and put real elements back in columns 77-78."""
    lines = []
    for line in text.splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        autodock_type = line[77:79].strip().upper()
        element = _AUTODOCK_ELEMENTS.get(autodock_type, autodock_type[:1].capitalize())
        lines.append(f"{line[:54]:<54}  1.00  0.00          {element:>2s}")
    lines.append("END")
    return "\n".join(lines)


def to_pdb(path: str | Path, output: str | Path) -> bool:
    """Write any readable structure out as PDB. Used to hand non-PDB inputs to PDB-only tools."""
    source, target = Path(path).expanduser(), Path(output).expanduser()
    suffix = normalized_suffix(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".pdb":
        target.write_text(source.read_text(errors="replace"))
        return True
    if suffix == ".pdbqt":
        target.write_text(pdbqt_to_pdb_text(source.read_text(errors="replace")))
        return True
    if suffix == ".cif":
        import gemmi

        try:
            structure = gemmi.read_structure(str(source))
            structure.setup_entities()
            target.write_text(structure.make_pdb_string())
        except (RuntimeError, ValueError, OSError):
            return False
        return True
    from rdkit import Chem

    mol = read_mol(source, sanitize=False)
    if mol is None:
        return False
    target.write_text(Chem.MolToPDBBlock(mol))
    return True


@contextlib.contextmanager
def as_pdb(path: str | Path) -> Iterator[Path]:
    """Yield this structure as a PDB file, converting only when it is not one already.

    PDB is what the preparation tools speak: meeko, ProDy, pdbfixer, pdb2pqr and reduce all
    parse PDB and none of them read mmCIF without losing something (ProDy 2.6.1 reads gemmi's
    label_asym_id as the chain id, so ``A`` becomes ``Axp`` and every fixed column shifts).
    Storage stays mmCIF; this is the rendering handed across the tool boundary, and it dies
    with the block.

    A structure too big for PDB (>99,999 atoms, >62 chains, 5-character CCD codes) is exactly
    the structure those tools cannot process either — the wall is theirs, not the archive's.
    """
    source = Path(path).expanduser().resolve()
    if normalized_suffix(source) == ".pdb":
        yield source
        return
    with tempfile.TemporaryDirectory(prefix="amdock_pdb_") as scratch:
        rendered = Path(scratch) / f"{source.stem}.pdb"
        if not to_pdb(source, rendered):
            raise ValueError(f"Cannot render {source.name} as PDB.")
        yield rendered


def to_canonical(path: str | Path, output: str | Path, *, molecule_kind: str = "") -> bool:
    """Rewrite a molecule into its canonical storage format (``.sdf`` or ``.cif``).

    A PDBQT is a docking artifact, not an archive format: it has no bond orders of its own and
    united-atom hydrogens. Storing the SDF/mmCIF alongside it is what makes the molecule readable
    by anything other than AutoDock.
    """
    source, target = Path(path).expanduser(), Path(output).expanduser()
    suffix = normalized_suffix(target)
    if suffix not in CANONICAL_SUFFIXES:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".sdf":
        from rdkit import Chem

        mol = read_mol(source)
        if mol is None:
            return False
        writer = Chem.SDWriter(str(target))
        try:
            writer.write(mol)
        finally:
            writer.close()
        return target.exists()
    import gemmi

    with tempfile.TemporaryDirectory() as scratch:
        scratch_pdb = Path(scratch) / "structure.pdb"
        if normalized_suffix(source) == ".cif":
            target.write_text(source.read_text(errors="replace"))
            return True
        if not to_pdb(source, scratch_pdb):
            return False
        try:
            structure = gemmi.read_structure(str(scratch_pdb))
            structure.setup_entities()
            document = structure.make_mmcif_document()
            document.write_file(str(target))
        except (RuntimeError, ValueError, OSError):
            return False
    return target.exists()
