from __future__ import annotations

from pathlib import Path

from amdockvs.integrations.contracts import ToolArtifact, ToolResult


# Meeko opens every ring of 7+ atoms into CG0/G0 closure pseudo-atoms; only Vina's own scoring
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
    from meeko import MoleculePreparation, PDBQTMolecule, PDBQTWriterLegacy, RDKitMolCreate

    mol = RDKitMolCreate.from_pdbqt_mol(PDBQTMolecule(text, poses_to_read=1, skip_typing=True))[0]
    if mol is None:
        raise RuntimeError(f"Meeko could not rebuild the ligand to keep its rings rigid: {path.name}")
    rigid, ok, error = PDBQTWriterLegacy.write_string(
        MoleculePreparation(rigid_macrocycles=True).prepare(mol)[0]
    )
    if not ok:
        raise RuntimeError(str(error or f"Meeko could not write the rigid-ring ligand: {path.name}"))
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / f"{path.stem}.rigid.pdbqt"
    out.write_text(rigid, encoding="utf-8")
    return out


# AutoDock and Vina have no boron type. As EasyDock does, a boron docks as a carbon; the carbon
# carries isotope BORON_MARK + the boron's formal charge so the pose can turn it back unambiguously
# (Meeko keeps isotopes in its REMARK SMILES).
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
    """Undo `boron_as_carbon` on a pose rebuilt by Meeko (in place; returns `mol`)."""
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() == 6 and BORON_MARK - 4 <= atom.GetIsotope() <= BORON_MARK + 4:
            atom.SetFormalCharge(atom.GetIsotope() - BORON_MARK)
            atom.SetAtomicNum(5)
            atom.SetIsotope(0)
    mol.UpdatePropertyCache(strict=False)
    return mol


def boron_in_sd(sd_string: str) -> str:
    """The SD text Meeko wrote from a pose, its marked carbons back to boron."""
    if "M  ISO" not in sd_string:
        return sd_string
    from io import StringIO

    from rdkit import Chem

    supplier = Chem.SDMolSupplier()
    supplier.SetData(sd_string, sanitize=False, removeHs=False)
    out = StringIO()
    writer = Chem.SDWriter(out)
    for mol in supplier:
        if mol is not None:
            writer.write(carbon_as_boron(mol))
    writer.close()
    return out.getvalue()


def _ligand_has_3d(mol) -> bool:
    if mol is None or mol.GetNumConformers() == 0:
        return False
    return any(bool(mol.GetConformer(index).Is3D()) for index in range(mol.GetNumConformers()))


def _prepare_ligand_3d_with_hs(mol):
    from rdkit import Chem

    if not _ligand_has_3d(mol):
        raise ValueError(
            "Ligand preparation for Vina requires an existing 3D conformer. "
            "Run the ligand 3D generation step before Meeko preparation."
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
    try:
        from meeko import MoleculePreparation, PDBQTWriterLegacy
    except ImportError as exc:
        raise RuntimeError("Python package 'meeko' is not available in the current environment.") from exc

    prepared_mol = boron_as_carbon(_prepare_ligand_3d_with_hs(mol))
    mk_prep = MoleculePreparation()
    molsetups = mk_prep.prepare(prepared_mol)
    if not molsetups:
        raise RuntimeError("Meeko did not produce any ligand setup.")
    pdbqt_string, is_ok, error_msg = PDBQTWriterLegacy.write_string(molsetups[0])
    if not is_ok:
        raise RuntimeError(str(error_msg or "Meeko could not write ligand PDBQT."))
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


def _flex_meeko_id(key: str) -> str | None:
    # Our stored key is "chain:resname:resnum"; Meeko's monomer id is "chain:resnum"
    # (empty chain stays empty, e.g. ":225"). Our "_" sentinel means "no chain".
    parts = str(key).split(":")
    if len(parts) != 3:
        return None
    chain, _resname, resnum = parts
    return f"{'' if chain == '_' else chain}:{resnum}"


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


def _polymer_from_pdb(receptor_text: str, templates, mk_prep):
    """Meeko's own PDB reader, falling back to ProDy for the structures it refuses."""
    from meeko import Polymer

    try:
        return Polymer.from_pdb_string(
            receptor_text, templates, mk_prep, allow_bad_res=True, default_altloc="A",
        )
    except ValueError:
        import prody
        from ms_flow.payloads import scratch_file

        # ProDy parses a path, not a string: hand it the filtered text, not the file on disk.
        with scratch_file(receptor_text.encode(), ".pdb") as filtered:
            structure = prody.parsePDB(str(filtered), altloc="all")
        return Polymer.from_prody(structure, templates, mk_prep, allow_bad_res=True, default_altloc="A")


def prepare_receptor_vina_pdbqt(
    *,
    source_path: str | Path,
    output_path: str | Path,
    flexible_residues: list[str] | None = None,
    exclude_resnames: frozenset[str] = frozenset(),
) -> ToolResult:
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

    try:
        from meeko import MoleculePreparation, PDBQTWriterLegacy, ResidueChemTemplates
    except ImportError as exc:
        raise RuntimeError("Python package 'meeko' is not available in the current environment.") from exc

    mk_prep = MoleculePreparation()
    templates = ResidueChemTemplates.create_from_defaults()
    from amdockvs.io.formats import as_pdb

    with as_pdb(source) as readable:
        receptor_text = _without_resnames(readable.read_text(encoding="utf-8"), exclude_resnames)
        polymer = _polymer_from_pdb(receptor_text, templates, mk_prep)
    # Mark the user-picked residues flexible before writing; this is what makes Meeko emit a
    # non-empty flex block. A bad/missing key is skipped, not fatal — one typo shouldn't sink prep.
    flex_applied: list[str] = []
    flex_skipped: list[str] = []
    if flexible_residues:
        valid = polymer.get_valid_monomers()
        for key in flexible_residues:
            mid = _flex_meeko_id(key)
            if mid is None or mid not in valid:
                flex_skipped.append(str(key))
                continue
            try:
                polymer.flexibilize_sidechain(mid, mk_prep)
                flex_applied.append(mid)
            except Exception:
                flex_skipped.append(str(key))

    rigid_pdbqt, flex_pdbqt = PDBQTWriterLegacy.write_string_from_polymer(polymer)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rigid_pdbqt, encoding="utf-8")
    extra: dict[str, object] = {"source_format": source.suffix.lstrip("."), "copied": False}
    if flex_applied:
        extra["flex_residues"] = flex_applied
    if flex_skipped:
        extra["flex_skipped"] = flex_skipped
    if flex_pdbqt.strip():
        flex_path = output.with_name(f"{output.stem}__flex.pdbqt")
        flex_path.write_text(flex_pdbqt, encoding="utf-8")
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
