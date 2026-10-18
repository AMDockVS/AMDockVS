"""Metals at receptor import: which ones center a Grid Box, and taking one off a receptor."""
from __future__ import annotations

import gemmi

from amdockvs.io.receptor_preview import (
    ReceptorImportOptions,
    build_receptor_import_preview,
    scan_receptor_structure,
    write_processed_receptor,
)


def _atom(serial, name, resname, resseq, x, y, element, record="ATOM"):
    return (f"{record:<6}{serial:>5} {name:<4} {resname:>3} A{resseq:>4}    "
            f"{x:>8.3f}{y:>8.3f}{0.0:>8.3f}  1.00 20.00          {element:>2}")


def _receptor(path):
    """A Gly chain plus ZN 301 and CD 302/303, each held by two waters (coordinated)."""
    lines, serial = [], 1
    for resseq in range(1, 4):
        for name, element, dx in (("N", "N", 0.0), ("CA", "C", 1.5), ("C", "C", 3.0), ("O", "O", 4.0)):
            lines.append(_atom(serial, name, "GLY", resseq, 40.0 + 4 * resseq + dx, 40.0, element))
            serial += 1
    water = 401
    for resseq, metal, x in ((301, "ZN", 0.0), (302, "CD", 10.0), (303, "CD", 20.0)):
        lines.append(_atom(serial, metal, metal, resseq, x, 0.0, metal, "HETATM"))
        serial += 1
        for y in (-2.2, 2.2):
            lines.append(_atom(serial, "O", "HOH", water, x, y, "O", "HETATM"))
            serial, water = serial + 1, water + 1
    path.write_text("\n".join(lines + ["END", ""]))
    return path


def test_only_the_configured_metals_center_a_grid_box(tmp_path):
    scan = scan_receptor_structure(_receptor(tmp_path / "rec.pdb"))
    options = ReceptorImportOptions(create_binding_sites_from_components=True, grid_box_metals=("ZN",))
    preview = build_receptor_import_preview(scan, options)
    assert [spec["source_ref"] for spec in preview["binding_site_specs"]] == ["ZN:A:301"]
    assert preview["workflow"]["metal_candidates"] == ["ZN:A:301", "CD:A:302", "CD:A:303"]


def test_a_metal_taken_off_the_receptor_leaves_the_structure_and_its_box(tmp_path):
    source = _receptor(tmp_path / "rec.pdb")
    scan = scan_receptor_structure(source)
    options = ReceptorImportOptions(
        create_binding_sites_from_components=True, selected_metals=("ZN:A:301", "CD:A:303"),
    )
    preview = build_receptor_import_preview(scan, options)
    assert [spec["source_ref"] for spec in preview["binding_site_specs"]] == ["ZN:A:301", "CD:A:303"]
    assert preview["workflow"]["metal_candidates"] == ["ZN:A:301", "CD:A:302", "CD:A:303"]  # still offered

    out = tmp_path / "out.pdb"
    write_processed_receptor(source, out, scan=scan, options=options)
    kept = {(res.name, res.seqid.num) for chain in gemmi.read_structure(str(out))[0] for res in chain}
    assert ("ZN", 301) in kept and ("CD", 303) in kept and ("CD", 302) not in kept


def test_import_panel_forwards_the_metals_taken_off_and_the_grid_box_metals(tmp_path):
    import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.importing.receptors import ReceptorImportPanel

    QApplication.instance() or QApplication(["metals-test"])
    path = str(_receptor(tmp_path / "rec.pdb").resolve())
    panel = ReceptorImportPanel(file_paths=[path])
    panel._show_scan(1, 1, path, scan_receptor_structure(path))
    panel._scan_complete = True
    panel.refresh_preview()
    metals = panel.table.cellWidget(0, 4)
    assert metals.selected_items() == ["ZN:A:301", "CD:A:302", "CD:A:303"]
    metals._remove("CD:A:302")
    request = panel.import_request()
    assert request["per_file"][path]["selected_metals"] == ["ZN:A:301", "CD:A:303"]
    assert request["grid_box_metals"] == ["ZN", "FE", "MG", "MN", "CU", "CO", "NI"]  # the config's list
