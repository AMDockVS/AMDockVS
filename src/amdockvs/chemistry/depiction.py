"""2D depictions (SVG) of stored structures, for the tables' preview column.

RDKit holds the GIL while it reads and draws, so the GUI runs these in a spawned process
(`ui.catalog.common.MoleculePreviews`): no Qt here, the child does not need it.
"""

from __future__ import annotations

from pathlib import Path

from amdockvs.core.configuration import DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS

_PREVIEW_WIDTH = 256
_PREVIEW_HEIGHT = 192


def _with_preview_frame(svg: str, *, width: int, height: int) -> str:
    if not svg:
        return ""
    rect = (
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
        f'rx="8" ry="8" fill="transparent"/>'
    )
    svg_tag_start = svg.find("<svg")
    if svg_tag_start < 0:
        return svg
    svg_tag_end = svg.find(">", svg_tag_start)
    if svg_tag_end < 0:
        return svg
    return svg[: svg_tag_end + 1] + rect + svg[svg_tag_end + 1 :]


def molecule_svg_for_path(
    path_text: str,
    width: int = _PREVIEW_WIDTH,
    height: int = _PREVIEW_HEIGHT,
    theme: str = "light",
    max_heavy_atoms: int = DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS,
) -> str:
    path = Path(str(path_text or "")).expanduser().resolve()
    if not path.exists():
        return ""
    try:
        from rdkit import Chem
        from rdkit.Chem.Draw import rdMolDraw2D
    except Exception:
        return ""

    mol = None
    suffix = path.suffix.lower()
    try:
        from amdockvs.io.formats import read_mol

        mol = read_mol(path)

    except Exception:
        mol = None

    if mol is None:
        return ""

    try:
        if mol.GetNumHeavyAtoms() > max_heavy_atoms:
            return ""
        mol = Chem.RemoveHs(mol)
    except Exception:
        return ""

    try:
        from rdkit.Chem import AllChem

        AllChem.Compute2DCoords(mol)
    except Exception:
        pass

    try:
        rdMolDraw2D.PrepareMolForDrawing(mol)
        drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
        options = drawer.drawOptions()
        options.clearBackground = False
        if theme == "dark":
            try:
                rdMolDraw2D.SetDarkMode(drawer)
            except (AttributeError, TypeError):
                pass
        drawer.DrawMolecule(mol)
        drawer.FinishDrawing()
        return _with_preview_frame(drawer.GetDrawingText(), width=width, height=height)
    except Exception:
        return ""


def molecule_svg_for_payload(
    data: bytes,
    kind: str,
    width: int = _PREVIEW_WIDTH,
    height: int = _PREVIEW_HEIGHT,
    theme: str = "light",
    max_heavy_atoms: int = DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS,
) -> str:
    """The depiction of one stored structure. The reader takes paths (`io.formats.read_mol` is the
    one reader of the app), so the bytes pass through a scratch file that lives only here."""
    from ms_flow.payloads import scratch_file

    with scratch_file(data, f".{kind or 'sdf'}") as path:
        return molecule_svg_for_path(str(path), width=width, height=height, theme=theme,
                                      max_heavy_atoms=max_heavy_atoms)


__all__ = ["molecule_svg_for_path", "molecule_svg_for_payload"]
