"""2D protein-ligand interaction diagrams via :mod:`ms_contactmap`.

Detection, layout and the saved JSON document are Qt-free, so they run in-process
anywhere (GUI worker thread, MF fork worker). Only the PNG/SVG export needs Qt, and
Qt inside an MF fork worker segfaults when a QApplication was inherited across the
fork, so :func:`export_diagram_image` draws the saved document in a fresh interpreter.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def diagram_path_for(pose_path: str | Path, pose_rank: int = 1, *, suffix: str = ".png") -> Path:
    """Deterministic diagram file next to the pose, resolvable without a DB lookup."""
    pose = Path(pose_path)
    return pose.with_name(f"{pose.name}.rank{int(pose_rank or 1)}.diagram{suffix}")


def build_pose_diagram(
    *,
    pose_path: str | Path,
    receptor_path: str | Path,
    pose_rank: int = 1,
    name: str | None = None,
):
    """The diagram *model* for one pose (interactions + chemistry, no Qt items) or ``None``.

    The ligand chemistry is the pose's own (meeko writes it exactly), never a SMILES:
    the stored one describes the imported ligand, not its prepared protonation state.
    """
    from amdockvs.io.formats import read_mol

    pose = Path(pose_path).expanduser().resolve()
    receptor = Path(receptor_path).expanduser().resolve()
    if not pose.exists() or not receptor.exists():
        return None
    mol = read_mol(pose, sanitize=False, index=max(0, int(pose_rank or 1) - 1))
    if mol is None:
        return None
    from ms_contactmap import build_pose_diagram as build

    return build(receptor, mol, name=name or pose.stem)


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


def export_diagram_image(document: str | Path, output_path: str | Path, *, timeout: float = 120.0) -> Path | None:
    """Draw a saved diagram document to PNG/SVG in a fresh interpreter (Qt-safe in fork workers)."""
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Hand the child our import roots so `amdockvs` resolves.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p) + os.pathsep + env.get("PYTHONPATH", "")
    out = Path(output_path).expanduser()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "amdockvs.docking.results.diagram", str(document), str(out)],
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out if proc.returncode == 0 and out.exists() else None


def render_diagrams_for_result_rows(
    rows,
    *,
    fmt: str = "png",
    replace_existing: bool = False,
    output_dir: str | Path | None = None,
) -> int:
    """Build, save and export one diagram per docking-result row; returns how many were written.

    Each row is a ``DockingResultRecord``-shaped mapping (``pose_path`` plus
    ``metrics.receptor_path``). Files land next to the pose via
    :func:`diagram_path_for`, so no DB column is needed to find them again.
    Shared by the standalone job and the inline docking hook. Failures are
    swallowed per row -- a diagram is never allowed to break a docking run.
    """
    from ms_contactmap import solve_layout

    suffix = ".svg" if str(fmt).lower() == "svg" else ".png"
    written = 0
    for row in rows:
        pose_path = str(row.get("pose_path") or "")
        receptor_path = str((row.get("metrics") or {}).get("receptor_path") or "")
        pose_rank = int(row.get("pose_rank") or 1)
        if not pose_path or not receptor_path:
            continue
        out = diagram_path_for(pose_path, pose_rank, suffix=suffix)
        document = diagram_path_for(pose_path, pose_rank, suffix=".json")
        if output_dir is not None:
            out = Path(output_dir) / out.name
            document = Path(output_dir) / document.name
        if out.exists() and document.exists() and not replace_existing:
            written += 1
            continue
        try:
            if replace_existing or not document.exists():
                diagram = build_pose_diagram(
                    pose_path=pose_path, receptor_path=receptor_path, pose_rank=pose_rank
                )
                if diagram is None:
                    continue
                save_pose_diagram(pose_path, pose_rank, diagram, solve_layout(diagram), output_dir=output_dir)
            if export_diagram_image(document, out):
                written += 1
        except Exception:  # noqa: BLE001 - see docstring
            continue
    return written


def _main(argv: list[str]) -> int:
    """``python -m amdockvs.docking.results.diagram DOCUMENT.json OUT.png|OUT.svg``"""
    if len(argv) != 2:
        return 2
    from ms_contactmap import build_scene, export_png, export_svg, load_json
    from ms_contactmap.export import ensure_app

    diagram, layout, _view = load_json(Path(argv[0]))
    if layout is None:
        return 1
    ensure_app()  # QApplication must exist before build_scene (font-metric segfault otherwise)
    scene = build_scene(diagram, layout.positions, layout.ligand_coords).scene
    out = Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    (export_svg if out.suffix.lower() == ".svg" else export_png)(scene, out)
    return 0 if out.exists() else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
