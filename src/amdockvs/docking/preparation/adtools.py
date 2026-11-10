from __future__ import annotations

from pathlib import Path

from amdockvs.integrations.contracts import ToolArtifact, ToolResult


# Ligand preparation opens every ring of 7+ atoms into CG0/G0 closure pseudo-atoms; only Vina's own scoring
# functions close them again. Every other program (and scoring) docks those rings rigid.
OPEN_RING_SCORING: dict[str, tuple[str, ...]] = {"vina": ("vina", "vinardo")}


def dockable_ligand(path: Path, *, program: str, scoring_function: str | None, output_dir: Path) -> Path:
    """The prepared ligand `program` can read: as prepared, or a rigid-ring copy under the run's
    `output_dir`."""
    if str(scoring_function or "").lower() in OPEN_RING_SCORING.get(str(program), ()):
        return path
    return rigid_ring_ligand(path, Path(output_dir) / "_rigid")


def _has_open_rings(text: str) -> bool:
    return any(
        line.startswith(("ATOM", "HETATM")) and line.split()[-1].startswith(("G", "CG"))
        for line in text.splitlines()
    )


def rigid_ring_ligand(path: Path, work_dir: Path) -> Path:
    """A copy of the ligand with its open rings rigid (same atoms and coordinates) in
    `work_dir`; a ligand without open rings is returned unchanged."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not _has_open_rings(text):
        return path
    from ms_adtools import pdbqt_to_rdkit, prepare_ligand

    try:
        mol = pdbqt_to_rdkit(text)
        rigid = prepare_ligand(mol, conf_id=mol.GetConformers()[0].GetId())
    except Exception as exc:  # noqa: BLE001 - the ligand is unusable rigid, say which one
        raise RuntimeError(f"Could not rebuild the ligand to keep its rings rigid: {path.name} ({exc})") from exc
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / f"{path.stem}.rigid.pdbqt"
    out.write_text(rigid, encoding="utf-8")
    return out


# AutoDock and Vina have no boron type. As EasyDock does, a boron docks as a carbon; the carbon
# carries isotope BORON_MARK + the boron's formal charge so the pose can turn it back unambiguously
# (the REMARK SMILES keeps isotopes).
BORON_MARK = 500


def boron_as_carbon(mol):
    """`mol` with every boron a marked carbon (same bonds and explicit Hs); unchanged without boron."""
    from rdkit import Chem

    if not any(atom.GetAtomicNum() == 5 for atom in mol.GetAtoms()):
        return mol
    mol = Chem.Mol(mol)
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() == 5:
            atom.SetIsotope(BORON_MARK + atom.GetFormalCharge())
            atom.SetAtomicNum(6)
            atom.SetFormalCharge(0)
            atom.SetNoImplicit(True)  # a 3-bonded boron must not become a CH
    Chem.SanitizeMol(mol)
    return mol


def carbon_as_boron(mol):
    """Undo `boron_as_carbon` on a pose rebuilt from its PDBQT (in place; returns `mol`)."""
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() == 6 and BORON_MARK - 4 <= atom.GetIsotope() <= BORON_MARK + 4:
            atom.SetFormalCharge(atom.GetIsotope() - BORON_MARK)
            atom.SetAtomicNum(5)
            atom.SetIsotope(0)
    mol.UpdatePropertyCache(strict=False)
    return mol


def poses_sd(pdbqt_text: str, name: str, limit: int | None = None) -> tuple[str, list[dict]]:
    """The poses of a docked PDBQT (from its REMARK SMILES) as SD text, marked carbons back to
    boron, and each pose's conformer properties (score, and a DLG's energies and cluster)."""
    from io import StringIO

    from ms_adtools import pdbqt_to_rdkit
    from rdkit import Chem

    mol = carbon_as_boron(pdbqt_to_rdkit(pdbqt_text))
    mol.SetProp("_Name", name)
    conformers = list(mol.GetConformers())[:limit]
    out = StringIO()
    writer = Chem.SDWriter(out)
    for conformer in conformers:
        writer.write(mol, confId=conformer.GetId())
    writer.close()
    return out.getvalue(), [conformer.GetPropsAsDict() for conformer in conformers]


def _ligand_has_3d(mol) -> bool:
    if mol is None or mol.GetNumConformers() == 0:
        return False
    return any(bool(mol.GetConformer(index).Is3D()) for index in range(mol.GetNumConformers()))


def _prepare_ligand_3d_with_hs(mol):
    from rdkit import Chem

    if not _ligand_has_3d(mol):
        raise ValueError(
            "Ligand preparation for Vina requires an existing 3D conformer. "
            "Run the ligand 3D generation step before preparing it."
        )
    return Chem.AddHs(Chem.Mol(mol), addCoords=True)


def _load_ligand_mol(source_path: Path):
    from rdkit import Chem

    from amdockvs.io.formats import is_readable, normalized_suffix, read_mol

    if normalized_suffix(source_path) == ".pdbqt":
        return Chem.Mol()  # already prepared: the caller copies it through untouched
    if not is_readable(source_path):
        raise ValueError(f"Ligand preparation does not support format '{source_path.suffix}'.")
    mol = read_mol(source_path)
    if mol is None:
        raise RuntimeError(f"Could not parse ligand file: {source_path}")
    return mol


def prepare_ligand_vina_pdbqt_from_mol(mol) -> ToolResult:
    from ms_adtools import prepare_ligand

    # Rings of 7+ atoms open into CG/G closure atoms as meeko's default; `dockable_ligand` makes
    # the rigid-ring copy for the programs that cannot close them.
    pdbqt_string = prepare_ligand(boron_as_carbon(_prepare_ligand_3d_with_hs(mol)), open_macrocycles=True)
    return ToolResult(
        artifact=ToolArtifact(
            kind="vina_ligand_pdbqt",
            media_type="chemical/x-pdbqt",
            metadata={"source": "rdkit_mol"},
        ),
        payload=pdbqt_string,
        metadata={"engine": "vina", "format": "pdbqt"},
    )


def prepare_ligand_vina_pdbqt(
    *,
    source_path: str | Path,
    output_path: str | Path,
) -> ToolResult:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if source.suffix.lower() == ".pdbqt":
        return ToolResult(
            artifact=ToolArtifact(
                kind="vina_ligand_pdbqt",
                path=source,
                media_type="chemical/x-pdbqt",
                metadata={"source_format": "pdbqt", "copied": False},
            ),
            metadata={"engine": "vina", "format": "pdbqt"},
        )

    mol = _load_ligand_mol(source)
    result = prepare_ligand_vina_pdbqt_from_mol(mol)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(str(result.payload or ""), encoding="utf-8")
    metadata = dict(result.metadata)
    metadata.update({"source_format": source.suffix.lstrip("."), "copied": False})
    artifact_metadata = dict(result.artifact.metadata if result.artifact is not None else {})
    artifact_metadata.update({"source_format": source.suffix.lstrip("."), "copied": False})
    return ToolResult(
        artifact=ToolArtifact(
            kind="vina_ligand_pdbqt",
            path=output,
            media_type="chemical/x-pdbqt",
            metadata=artifact_metadata,
        ),
        payload=None,
        metadata=metadata,
        warnings=result.warnings,
    )


def _flex_spec(key: str) -> str | None:
    """ms_adtools' residue spec ("A:ARG239", "ARG239" without a chain) of our stored key
    "chain:resname:resnum", whose "_" chain means "no chain"."""
    parts = str(key).split(":")
    if len(parts) != 3:
        return None
    chain, resname, resnum = parts
    return f"{'' if chain == '_' else chain + ':'}{resname}{resnum}"


def _without_resnames(text: str, exclude: frozenset[str]) -> str:
    """Drop whole residues from a PDB block by residue name (columns 18-20).

    The receptor file keeps whatever import decided to keep (waters, cofactors, metals);
    which of those actually reach the PDBQT is a per-preparation choice, so the filter lives
    here and not in the stored structure.
    """
    if not exclude:
        return text
    kept = [
        line
        for line in text.splitlines(True)
        if not (line.startswith(("ATOM", "HETATM")) and line[17:20].strip().upper() in exclude)
    ]
    return "".join(kept)


def prepare_receptor_vina_pdbqt(
    *,
    source_path: str | Path,
    output_path: str | Path,
    flexible_residues: list[str] | None = None,
    exclude_resnames: frozenset[str] = frozenset(),
) -> ToolResult:
    """Receptor PDBQT (ms_adtools): hydrogens added to the residues that have none, Gasteiger
    charges, non-polar hydrogens merged. ``flexible_residues`` (stored keys) also write
    ``{stem}__flex.pdbqt``; a key not in the receptor, or a residue with nothing to rotate, is
    skipped, not fatal — one typo should not sink the preparation."""
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if source.suffix.lower() == ".pdbqt":
        return ToolResult(
            artifact=ToolArtifact(
                kind="vina_receptor_pdbqt",
                path=source,
                media_type="chemical/x-pdbqt",
                metadata={"source_format": "pdbqt", "copied": False},
            ),
            metadata={"engine": "vina", "format": "pdbqt"},
        )

    import warnings

    from ms_adtools import prepare_flexreceptor, prepare_receptor
    from ms_flow.payloads import scratch_file

    from amdockvs.io.formats import as_pdb

    with as_pdb(source) as readable:
        text = _without_resnames(readable.read_text(encoding="utf-8"), exclude_resnames)
    with scratch_file(text.encode(), ".pdb") as filtered:
        # waters and cofactors were filtered above, by the user's choice: the cleanup leaves them be
        rigid = prepare_receptor(filtered, repairs="hydrogens", cleanup="nphs_lps")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rigid, encoding="utf-8")
    present = {(l[21].strip(), l[17:20].strip() + l[22:27].strip()) for l in rigid.splitlines()
               if l.startswith(("ATOM", "HETATM"))}
    flex_applied: list[str] = []
    flex_skipped: list[str] = []
    for key in flexible_residues or []:
        spec = _flex_spec(key)
        chain, _, name = (spec or "").rpartition(":")
        (flex_applied if spec and (chain, name) in present else flex_skipped).append(str(key))
    flex = ""
    if flex_applied:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rigid, flex = prepare_flexreceptor(output, [_flex_spec(k) for k in flex_applied])
        rigid_ones = " ".join(str(w.message) for w in caught)
        for key in list(flex_applied):  # "PRO A 227 has no rotatable side chain: kept rigid"
            chain, resname, resnum = key.split(":")
            if " ".join(f"{resname} {'' if chain == '_' else chain} {resnum}".split()) in rigid_ones:
                flex_applied.remove(key)
                flex_skipped.append(key)
        output.write_text(rigid, encoding="utf-8")
    extra: dict[str, object] = {"source_format": source.suffix.lstrip("."), "copied": False}
    if flex_applied:
        extra["flex_residues"] = flex_applied
    if flex_skipped:
        extra["flex_skipped"] = flex_skipped
    if flex.strip():
        flex_path = output.with_name(f"{output.stem}__flex.pdbqt")
        flex_path.write_text(flex, encoding="utf-8")
        extra["flex_path"] = str(flex_path)
    return ToolResult(
        artifact=ToolArtifact(
            kind="vina_receptor_pdbqt",
            path=output,
            media_type="chemical/x-pdbqt",
            metadata=extra,
        ),
        metadata={"engine": "vina", "format": "pdbqt", **extra},
    )


__all__ = [
    "prepare_ligand_vina_pdbqt",
    "prepare_ligand_vina_pdbqt_from_mol",
    "prepare_receptor_vina_pdbqt",
]
