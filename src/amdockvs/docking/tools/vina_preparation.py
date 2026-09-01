from __future__ import annotations

from pathlib import Path

from amdockvs.tools import ToolArtifact, ToolResult


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

    suffix = source_path.suffix.lower()
    if suffix in {".sdf", ".sd", ".mol"}:
        supplier = Chem.SDMolSupplier(str(source_path), removeHs=False)
        mol = supplier[0] if supplier and len(supplier) > 0 else None
    elif suffix == ".mol2":
        mol = Chem.MolFromMol2File(str(source_path), sanitize=True, removeHs=False)
    elif suffix == ".pdb":
        mol = Chem.MolFromPDBFile(str(source_path), sanitize=True, removeHs=False)
    elif suffix == ".pdbqt":
        return Chem.Mol()
    else:
        raise ValueError(f"Ligand preparation does not support format '{suffix}'.")
    if mol is None:
        raise RuntimeError(f"RDKit could not parse ligand file: {source_path}")
    return mol


def prepare_ligand_vina_pdbqt_from_mol(mol) -> ToolResult:
    try:
        from meeko import MoleculePreparation, PDBQTWriterLegacy
    except ImportError as exc:
        raise RuntimeError("Python package 'meeko' is not available in the current environment.") from exc

    prepared_mol = _prepare_ligand_3d_with_hs(mol)
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
        from meeko import MoleculePreparation, PDBQTWriterLegacy, Polymer, ResidueChemTemplates
    except ImportError as exc:
        raise RuntimeError("Python package 'meeko' is not available in the current environment.") from exc

    mk_prep = MoleculePreparation()
    templates = ResidueChemTemplates.create_from_defaults()
    receptor_text = _without_resnames(source.read_text(encoding="utf-8"), exclude_resnames)
    try:
        polymer = Polymer.from_pdb_string(
            receptor_text,
            templates,
            mk_prep,
            allow_bad_res=True,
            default_altloc="A",
        )
    except ValueError:
        try:
            import prody
        except ImportError:
            raise
        polymer = Polymer.from_prody(
            prody.parsePDB(str(_filtered_copy(source, receptor_text)), altloc="all"),
            templates,
            mk_prep,
            allow_bad_res=True,
            default_altloc="A",
        )
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


def _filtered_copy(source: Path, text: str) -> Path:
    """ProDy parses a path, not a string: hand it the filtered text, not the file on disk."""
    if text == source.read_text(encoding="utf-8"):
        return source
    tmp = source.with_name(f"{source.stem}__prep_filtered.pdb")
    tmp.write_text(text, encoding="utf-8")
    return tmp


__all__ = [
    "prepare_ligand_vina_pdbqt",
    "prepare_ligand_vina_pdbqt_from_mol",
    "prepare_receptor_vina_pdbqt",
]
