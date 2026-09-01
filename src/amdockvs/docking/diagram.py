"""2D protein-ligand interaction diagrams via :mod:`ms_contactmap`.

``ms_contactmap`` detects the contacts with its own native detector and renders the
2D diagram with Qt. Two entry points:

* :func:`render_interaction_diagram` -- pure, in-process render. Safe to call from
  the GUI thread for a single on-demand diagram (reuses the running QApplication).
* :func:`render_diagram_subprocess` -- runs the same render in a fresh interpreter.
  Qt + RDKit inside an MF fork-worker segfault when a QApplication was inherited
  across the fork, so jobs/inline-docking use this isolated path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def diagram_path_for(pose_path: str | Path, pose_rank: int = 1, *, suffix: str = ".png") -> Path:
    """Deterministic diagram file next to the pose, resolvable without a DB lookup."""
    pose = Path(pose_path)
    return pose.with_name(f"{pose.name}.rank{int(pose_rank or 1)}.diagram{suffix}")


# RDKit writes the ligand with a blank chain and residue 1; ms_contactmap looks the residue up by
# (resname, chain, resnum) for its SASA pass and finds nothing. Stamp an unused chain instead.
LIGAND_CHAIN, LIGAND_RESNUM = "Z", 900


def _ligand_pdb_residue(ligand_pdb: Path) -> tuple[str, str | None, int | None]:
    """Read the resname/chain/resnum RDKit wrote for the ligand HETATM/ATOM block."""
    for raw in ligand_pdb.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw.startswith(("HETATM", "ATOM")):
            continue
        resname = raw[17:20].strip() or "UNL"
        chain = raw[21:22].strip() or None
        try:
            resnum: int | None = int(raw[22:26])
        except ValueError:
            resnum = None
        return resname, chain, resnum
    return "UNL", None, None


def _ligand_smiles(mol) -> str | None:
    try:
        from rdkit import Chem

        copy = Chem.Mol(mol)
        Chem.SanitizeMol(copy)
        return Chem.MolToSmiles(Chem.RemoveHs(copy))
    except Exception:
        return None


def build_pose_diagram(
    *,
    pose_path: str | Path,
    receptor_path: str | Path,
    pose_rank: int = 1,
    smiles: str | None = None,
    name: str | None = None,
):
    """The diagram *model* for one pose (interactions + chemistry, no Qt items) or ``None``.

    Builds a receptor+ligand complex PDB the same way the interaction path does
    and derives the ligand SMILES from the pose when not supplied. Nothing here touches
    the scene graph, so it is safe to call off the GUI thread -- which is the point:
    it is the slow half, and the viewer runs it in a worker before drawing.
    """
    from amdockvs.docking.interactions import (
        _rdkit_mol,
        _write_ligand_pdb,
        as_pdb_block,
        atom_count,
    )

    pose = Path(pose_path).expanduser().resolve()
    receptor = Path(receptor_path).expanduser().resolve()
    if not pose.exists() or not receptor.exists():
        return None
    mol = _rdkit_mol(pose, pose_rank=pose_rank)
    if mol is None:
        return None
    if not smiles:
        smiles = _ligand_smiles(mol)
    if not smiles:
        return None

    from ms_contactmap import build_diagram

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ligand_pdb = tmp_path / "ligand.pdb"
        if not _write_ligand_pdb(pose, pose_rank=pose_rank, output_path=ligand_pdb):
            return None
        resname, _, _ = _ligand_pdb_residue(ligand_pdb)
        chain, resnum = LIGAND_CHAIN, LIGAND_RESNUM
        complex_pdb = tmp_path / "complex.pdb"
        # as_pdb_block strips the PDBQT tail (which buries the element column) and the
        # per-half END records. The TER keeps the ligand a residue of its own instead of the
        # last residue of the receptor chain.
        receptor_text = as_pdb_block(receptor.read_text(encoding="utf-8", errors="ignore"))
        ligand_text = as_pdb_block(
            ligand_pdb.read_text(encoding="utf-8", errors="ignore"),
            chain=chain,
            resnum=resnum,
            first_serial=atom_count(receptor_text) + 1,
        )
        complex_pdb.write_text(f"{receptor_text}\nTER\n{ligand_text}\nEND\n", encoding="utf-8")
        return build_diagram(
            str(complex_pdb), resname, smiles, name=name or pose.stem, chain=chain, resnum=resnum
        )


def save_pose_diagram(
    pose_path: str | Path,
    pose_rank: int,
    diagram,
    layout,
    *,
    output_dir: str | Path | None = None,
) -> Path:
    """Save the solved diagram next to the pose in ms_contactmap's own JSON document.

    Analysis + layout in one versioned, self-contained file: the viewer needs neither
    the complex PDB nor a detector run to draw it again. A missing or rejected document
    is not an error -- :func:`load_pose_diagram` returns ``None`` and the caller rebuilds.
    """
    from ms_contactmap import save_json

    out = diagram_path_for(pose_path, pose_rank, suffix=".json")
    if output_dir is not None:
        out = Path(output_dir) / out.name
    out.parent.mkdir(parents=True, exist_ok=True)
    return save_json(out, diagram, layout)


def pose_interactions(pose_path: str | Path, pose_rank: int = 1) -> list[dict] | None:
    """The interaction list of a saved diagram, or ``None`` when there is no document.

    A plain JSON read: the detection pass already wrote them, so listing them needs neither
    ms_contactmap nor a DB table. This is what the results panel shows.
    """
    path = diagram_path_for(pose_path, pose_rank, suffix=".json")
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # broken/half-written file: same as "not built"
        return None
    return list((document.get("diagram") or {}).get("interactions") or [])


def load_pose_diagram(pose_path: str | Path, pose_rank: int = 1):
    """``(diagram, layout)`` from the saved document, or ``None`` when unusable.

    A layout-less document (analysis only) counts as unusable here: solving is the slow
    half and the dock's Build button already runs it off the GUI thread.
    """
    path = diagram_path_for(pose_path, pose_rank, suffix=".json")
    if not path.exists():
        return None
    from ms_contactmap import load_json

    try:
        diagram, layout, _view = load_json(path)
    except Exception:  # noqa: BLE001 - older schema/broken file: rebuild instead
        return None
    return None if layout is None else (diagram, layout)


def render_interaction_diagram(
    *,
    pose_path: str | Path,
    receptor_path: str | Path,
    output_path: str | Path,
    pose_rank: int = 1,
    smiles: str | None = None,
    name: str | None = None,
    artifact_dir: str | Path | None = None,
) -> Path | None:
    """Export one pose's diagram to PNG/SVG; returns the written file or ``None``.

    The QApplication is created *before* ``build_scene`` -- Qt font metrics segfault
    otherwise.
    """
    diagram = build_pose_diagram(
        pose_path=pose_path,
        receptor_path=receptor_path,
        pose_rank=pose_rank,
        smiles=smiles,
        name=name,
    )
    if diagram is None:
        return None

    from ms_contactmap import build_scene, export_png, export_svg, solve_layout
    from ms_contactmap.export import ensure_app

    ensure_app()  # QApplication must exist before build_scene (font-metric segfault otherwise)
    layout = solve_layout(diagram)
    # The image is for reports; the viewer wants the model, and re-deriving it costs another
    # detection run plus a layout solve. Both come out of this one pass, so both get written.
    save_pose_diagram(pose_path, pose_rank, diagram, layout, output_dir=artifact_dir)
    scene = build_scene(diagram, layout.positions, layout.ligand_coords).scene
    out = Path(output_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".svg":
        export_svg(scene, out)
    else:
        export_png(scene, out)
    return out if out.exists() else None


def render_diagram_subprocess(
    *,
    pose_path: str | Path,
    receptor_path: str | Path,
    output_path: str | Path,
    pose_rank: int = 1,
    smiles: str | None = None,
    name: str | None = None,
    artifact_dir: str | Path | None = None,
    timeout: float = 600.0,
) -> Path | None:
    """Isolated render in a fresh interpreter (Qt-safe inside fork workers)."""
    spec = {
        "pose_path": str(pose_path),
        "receptor_path": str(receptor_path),
        "output_path": str(output_path),
        "pose_rank": int(pose_rank or 1),
        "smiles": smiles,
        "name": name,
        "artifact_dir": None if artifact_dir is None else str(artifact_dir),
    }
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Hand the child our import roots so `amdockvs` resolve.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "amdockvs.docking.diagram", json.dumps(spec)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = Path(output_path).expanduser()
    return out if proc.returncode == 0 and out.exists() else None


def render_diagrams_for_result_rows(
    rows,
    *,
    fmt: str = "png",
    replace_existing: bool = False,
    output_dir: str | Path | None = None,
) -> int:
    """Render one diagram per docking-result row; returns how many were written.

    Each row is a ``DockingResultRecord``-shaped mapping (``pose_path`` plus
    ``metrics.receptor_path``). Files land next to the pose via
    :func:`diagram_path_for`, so no DB column is needed to find them again.
    Shared by the standalone job and the inline docking hook. Failures are
    swallowed per row -- a diagram is never allowed to break a docking run.
    """
    suffix = ".svg" if str(fmt).lower() == "svg" else ".png"
    written = 0
    for row in rows:
        pose_path = str(row.get("pose_path") or "")
        receptor_path = str((row.get("metrics") or {}).get("receptor_path") or "")
        pose_rank = int(row.get("pose_rank") or 1)
        if not pose_path or not receptor_path:
            continue
        out = diagram_path_for(pose_path, pose_rank, suffix=suffix)
        cached = diagram_path_for(pose_path, pose_rank, suffix=".json")
        if output_dir is not None:
            out = Path(output_dir) / out.name
            cached = Path(output_dir) / cached.name
        # Both artifacts or none: an image left over from before the model cache existed would
        # otherwise skip the row and leave the viewer with nothing to open.
        if out.exists() and cached.exists() and not replace_existing:
            written += 1
            continue
        try:
            if render_diagram_subprocess(
                pose_path=pose_path,
                receptor_path=receptor_path,
                output_path=out,
                pose_rank=pose_rank,
                artifact_dir=output_dir,
            ):
                written += 1
        except Exception:
            continue
    return written


def _main(argv: list[str]) -> int:
    if not argv:
        return 2
    spec = json.loads(argv[0])
    result = render_interaction_diagram(
        pose_path=spec["pose_path"],
        receptor_path=spec["receptor_path"],
        output_path=spec["output_path"],
        pose_rank=int(spec.get("pose_rank") or 1),
        smiles=spec.get("smiles"),
        name=spec.get("name"),
        artifact_dir=spec.get("artifact_dir"),
    )
    return 0 if result is not None else 1


def _selfcheck() -> None:
    # Path convention + ligand-residue parsing (the only branchy logic here).
    assert diagram_path_for("/x/pose_1.pdbqt", 2).name == "pose_1.pdbqt.rank2.diagram.png"
    assert diagram_path_for("/x/p.sdf", suffix=".svg").name == "p.sdf.rank1.diagram.svg"
    with tempfile.TemporaryDirectory() as tmp:
        lig = Path(tmp) / "l.pdb"
        lig.write_text(
            "HETATM    1  C1  LIG A 407      1.0   2.0   3.0  1.00  0.00           C\n",
            encoding="utf-8",
        )
        assert _ligand_pdb_residue(lig) == ("LIG", "A", 407)
        empty = Path(tmp) / "e.pdb"
        empty.write_text("REMARK nothing\n", encoding="utf-8")
        assert _ligand_pdb_residue(empty) == ("UNL", None, None)
        # Saved-document roundtrip: a minimal but real Diagram/LayoutResult, so the check
        # fails if ms_contactmap's schema and what the viewer reads back drift apart.
        from rdkit import Chem
        from rdkit.Chem import AllChem

        from ms_contactmap import Diagram, LayoutResult

        mol = Chem.MolFromSmiles("CCO")
        AllChem.Compute2DCoords(mol)
        xy = [(p.x, p.y) for p in (mol.GetConformer().GetAtomPosition(i) for i in range(3))]
        diagram = Diagram(name="d", ligand_name="LIG", mol=mol, coords_2d=xy)
        layout = LayoutResult({}, xy, 0.0, False, 0.0, {}, 0)
        pose = Path(tmp) / "pose.sdf"
        assert save_pose_diagram(pose, 2, diagram, layout).name == "pose.sdf.rank2.diagram.json"
        loaded, loaded_layout = load_pose_diagram(pose, 2)
        assert loaded.ligand_name == "LIG" and loaded.mol.GetNumAtoms() == 3
        assert loaded_layout.ligand_coords == [tuple(p) for p in xy]
        assert load_pose_diagram(pose, 7) is None  # never rendered

    # The complex PDB handed to ms_contactmap: PDBQT tail gone, element restored, hydrogens dropped,
    # serials renumbered from first_serial, chain/resnum stamped.
    from amdockvs.docking.interactions import as_pdb_block, atom_count

    pdbqt = (
        "ATOM      7  N   ASP A 285     -3.256 -3.631 -1.685  1.00  0.00    -0.273 NA\n"
        "ATOM      8  HD1 ASP A 285     -3.100 -3.100 -1.100  1.00  0.00     0.100 HD\n"
        "ATOM      9 CL1  LIG A 285     -4.000 -4.000 -2.000  1.00  0.00     0.000 Cl\n"
        "END\n"
    )
    block = as_pdb_block(pdbqt, chain="Z", resnum=900, first_serial=42)
    assert atom_count(block) == 2, block  # the HD hydrogen is gone
    line, halogen = block.splitlines()
    assert int(line[6:11]) == 42 and line[21] == "Z" and int(line[22:26]) == 900, line
    assert line[76:78].strip() == "N" and len(line) == 78, repr(line)
    # Two-letter elements keep both letters, or every chlorine reads as a carbon.
    assert halogen[76:78] == "Cl", repr(halogen)
    # RDKit stamps the formal charge onto the element field of a charged atom. Keeping
    # it made ms_contactmap read "N1+" as its own element, and every ligand with a
    # protonated amine failed its composition check instead of drawing.
    charged = as_pdb_block(
        "HETATM   32  N7  UNL     1      18.3  46.8  22.8  1.00  0.00           N1+\n"
    )
    assert charged[76:78].strip() == "N", repr(charged)

    from amdockvs.docking.interactions import _interaction_type

    # ms_contactmap's vocabulary reaches the table intact; only these two are renamed.
    assert _interaction_type("pi_stacking") == "pi_stacking"
    assert _interaction_type("hbond") == "hydrogen_bond"
    assert _interaction_type("metal_coordination") == "metal_complex"
    print("diagram selfcheck ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        _selfcheck()
    else:
        raise SystemExit(_main(sys.argv[1:]))
