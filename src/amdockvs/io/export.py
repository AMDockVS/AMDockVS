"""Export stored structures: poses as one SDF with their scores, any structure as a file (or a ZIP).

Pure functions over a `PayloadStore` (`core.payloads.payload_store(project)`): nothing here knows
about Qt, so the UI calls them from `run_async` and a script can call them as they are.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, Sequence

# What a pose row carries into the SDF: property name -> DockingHitSummary attribute.
POSE_PROPERTIES = {
    "Ligand": "ligand_name",
    "Receptor": "receptor_name",
    "Engine": "engine",
    "Protocol": "protocol_label",
    "Score": "score",
    "RMSD_vs_reference": "rmsd_vs_reference",
    "Ligand_efficiency": "ligand_efficiency",
    "Predicted_pKi": "predicted_pki",
    "Fit_quality": "fit_quality",
}


def _record(text: str, title: str, properties: dict[str, Any]) -> str:
    """One SDF record with a title (kept when it has one) and the properties after its last item."""
    lines = text.rstrip().split("\n")
    if lines and lines[-1].strip() == "$$$$":
        lines.pop()
    if lines and not lines[0].strip():
        lines[0] = title
    items = "".join(f"> <{name}>\n{value}\n\n" for name, value in properties.items())
    return "\n".join(lines) + "\n" + items + "$$$$\n"


def export_poses_sdf(store, hits: Iterable[Any], dest: str | Path) -> int:
    """Write the pose of every hit (a `DockingHitSummary`) to the SDF `dest`, with `POSE_PROPERTIES`
    as data items; a pose in a format other than SDF is skipped. Returns the number written."""
    hits = [hit for hit in hits if getattr(hit, "pose_payload_id", None)]
    by_id = {int(hit.pose_payload_id): hit for hit in hits}
    kinds = store.kinds(by_id)
    count = 0
    with open(dest, "w", encoding="utf-8", newline="\n") as out:
        for payload_id, data in store.iter_bytes(i for i in by_id if kinds.get(i) == "sdf"):
            hit = by_id[payload_id]
            properties = {name: getattr(hit, attr) for name, attr in POSE_PROPERTIES.items()
                          if getattr(hit, attr, None) not in (None, "")}
            out.write(_record(data.decode(), str(hit.ligand_name), properties))
            count += 1
    return count


def _safe(name: str) -> str:
    return re.sub(r"[^\w.+-]+", "_", name).strip("._") or "structure"


def export_structures(store, items: Sequence[tuple[str, int]], dest: str | Path) -> int:
    """Write each `(name, payload_id)` as `<name>.<kind>`: into the ZIP `dest` when it ends in
    `.zip`, else into the directory `dest`. Two items with one name keep both (`name_<id>`).
    Returns the number written."""
    names = {int(payload_id): name for name, payload_id in items if payload_id}
    kinds = store.kinds(names)
    taken: set[str] = set()

    def filename(payload_id: int) -> str:
        stem = _safe(names[payload_id])
        file = f"{stem}.{kinds[payload_id]}"
        if file in taken:
            file = f"{stem}_{payload_id}.{kinds[payload_id]}"
        taken.add(file)
        return file

    wanted = [i for i in names if i in kinds]
    dest = Path(dest)
    if dest.suffix.lower() == ".zip":
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
            for payload_id, data in store.iter_bytes(wanted):
                archive.writestr(filename(payload_id), data)
    else:
        dest.mkdir(parents=True, exist_ok=True)
        for payload_id, data in store.iter_bytes(wanted):
            (dest / filename(payload_id)).write_bytes(data)
    return len(wanted)


__all__ = ["POSE_PROPERTIES", "export_poses_sdf", "export_structures"]
