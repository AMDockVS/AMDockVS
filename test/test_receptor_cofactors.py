"""Cofactors at receptor import: each one is offered in its own column, and one taken off leaves.

Before the Cofactors column a kept cofactor was invisible in the import dialog (1D3G's FMN showed
in no column), and the only control was a global checkbox that stripped every cofactor of every
receptor at import while its label said "during preparation".
"""
from __future__ import annotations

import gemmi
import pytest

from amdockvs.io.receptor_preview import (
    ReceptorImportOptions,
    build_receptor_import_maps,
    build_receptor_import_preview,
    scan_receptor_structure,
    write_processed_receptor,
)
from amdockvs.io.transformers.structures import _receptor_import_variant


def _atom(serial, name, resname, resseq, x, y, element, record="ATOM"):
    return (f"{record:<6}{serial:>5} {name:<4} {resname:>3} A{resseq:>4}    "
            f"{x:>8.3f}{y:>8.3f}{0.0:>8.3f}  1.00 20.00          {element:>2}")


def _receptor(path):
    """A Gly chain, two FMN copies (A 401, A 402) and one HEM (A 403): three cofactors."""
    lines, serial = [], 1
    for resseq in range(1, 4):
        for name, element, dx in (("N", "N", 0.0), ("CA", "C", 1.5), ("C", "C", 3.0), ("O", "O", 4.0)):
            lines.append(_atom(serial, name, "GLY", resseq, 4 * resseq + dx, 0.0, element))
            serial += 1
    for resseq, resname, x in ((401, "FMN", 20.0), (402, "FMN", 30.0), (403, "HEM", 40.0)):
        for name, element, dy in (("C2", "C", 0.0), ("N3", "N", 1.4), ("O4", "O", 2.8)):
            lines.append(_atom(serial, name, resname, resseq, x, 10.0 + dy, element, "HETATM"))
            serial += 1
    path.write_text("\n".join(lines + ["END", ""]))
    return path


def _kept(source, scan, options, tmp_path):
    out = tmp_path / "out.pdb"
    write_processed_receptor(source, out, scan=scan, options=options)
    return sorted((res.name, res.seqid.num) for chain in gemmi.read_structure(str(out))[0] for res in chain
                  if res.name in {"FMN", "HEM"})


def test_every_cofactor_is_offered_and_kept_by_default(tmp_path):
    source = _receptor(tmp_path / "rec.pdb")
    scan = scan_receptor_structure(source)
    preview = build_receptor_import_preview(scan, ReceptorImportOptions())
    assert preview["workflow"]["cofactor_candidates"] == ["FMN:A:401", "FMN:A:402", "HEM:A:403"]
    assert _kept(source, scan, ReceptorImportOptions(), tmp_path) == [("FMN", 401), ("FMN", 402), ("HEM", 403)]


def test_a_cofactor_taken_off_leaves_the_structure(tmp_path):
    source = _receptor(tmp_path / "rec.pdb")
    scan = scan_receptor_structure(source)
    options = ReceptorImportOptions(selected_cofactors=("FMN:A:401", "HEM:A:403"))
    preview = build_receptor_import_preview(scan, options)
    assert preview["workflow"]["cofactor_candidates"] == ["FMN:A:401", "FMN:A:402", "HEM:A:403"]  # still offered
    assert [c["selector"] for c in preview["structure"]["components"]["cofactors"]] == ["FMN:A:401", "HEM:A:403"]
    assert preview["structure"]["import_profile"]["selected_cofactors"] == ["FMN:A:401", "HEM:A:403"]
    assert _kept(source, scan, options, tmp_path) == [("FMN", 401), ("HEM", 403)]
    assert _kept(source, scan, ReceptorImportOptions(selected_cofactors=()), tmp_path) == []


def test_remove_cofactors_still_takes_every_one_for_a_script(tmp_path):
    """The dialog lost its checkbox; load_receptors(remove_cofactors=True) keeps working."""
    source = _receptor(tmp_path / "rec.pdb")
    scan = scan_receptor_structure(source)
    options = ReceptorImportOptions(remove_cofactors=True, selected_cofactors=("FMN:A:401",))
    assert _kept(source, scan, options, tmp_path) == []
    assert build_receptor_import_preview(scan, options)["structure"]["components"]["cofactors"] == []


def test_an_unknown_cofactor_is_refused(tmp_path):
    source = str(_receptor(tmp_path / "rec.pdb"))
    with pytest.raises(ValueError, match=r"no cofactor NAD:A:1 \(has: FMN:A:401, FMN:A:402, HEM:A:403\)"):
        build_receptor_import_maps([source], base_options=ReceptorImportOptions(),
                                   per_file={source: {"selected_cofactors": ["NAD:A:1"]}})


def test_the_storage_key_changes_only_when_cofactors_are_chosen():
    """A key written before this column existed has to match a re-import that keeps everything."""
    default = _receptor_import_variant(ReceptorImportOptions())
    assert "FMN" not in default and default.endswith("None, None)")
    assert _receptor_import_variant(ReceptorImportOptions(selected_cofactors=("FMN:A:401",))) != default


def test_import_panel_shows_cofactors_beside_metals_and_forwards_the_ones_taken_off(tmp_path, monkeypatch):
    import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.importing.receptors import COFACTORS_COLUMN, ReceptorImportPanel

    QApplication.instance() or QApplication(["cofactors-test"])
    # The scan is fed in below; the panel's own would still be running when a later test
    # processes events, and its thread would be torn down mid-run.
    monkeypatch.setattr(ReceptorImportPanel, "start_scan", lambda self: None)
    path = str(_receptor(tmp_path / "rec.pdb").resolve())
    panel = ReceptorImportPanel(file_paths=[path])
    assert not hasattr(panel, "remove_cofactors"), "the column replaces the global checkbox"
    header = panel.table.horizontalHeader()
    assert panel.table.horizontalHeaderItem(COFACTORS_COLUMN).text() == "Cofactors"
    assert header.visualIndex(COFACTORS_COLUMN) == header.visualIndex(4) + 1 == header.visualIndex(5) - 1

    panel._show_scan(1, 1, path, scan_receptor_structure(path))
    panel._scan_complete = True
    panel.refresh_preview()
    cofactors = panel.table.cellWidget(0, COFACTORS_COLUMN)
    assert not panel.table.isColumnHidden(COFACTORS_COLUMN)
    assert cofactors.selected_items() == ["FMN:A:401", "FMN:A:402", "HEM:A:403"]
    cofactors._remove("FMN:A:402")
    request = panel.import_request()
    assert request["per_file"][path]["selected_cofactors"] == ["FMN:A:401", "HEM:A:403"]
    assert "remove_cofactors" not in request
    panel.close()


def test_every_column_fits_the_dialog_without_scrolling(tmp_path, monkeypatch):
    """Three chip columns at 190 px squeezed Ligands to nothing and scrolled the table sideways in
    the dialog (~975 px for the panel). One chip wide, their chips stack down instead."""
    import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.importing.receptors import COFACTORS_COLUMN, ReceptorImportPanel

    app = QApplication.instance() or QApplication(["cofactors-test"])
    monkeypatch.setattr(ReceptorImportPanel, "start_scan", lambda self: None)  # the scan is fed in below
    path = str(_receptor(tmp_path / "rec.pdb").resolve())
    panel = ReceptorImportPanel(file_paths=[path])
    panel._show_scan(1, 1, path, scan_receptor_structure(path))
    panel._scan_complete = True
    panel.refresh_preview()
    panel.resize(975, 560)
    panel.show()
    app.processEvents()
    assert panel.table.horizontalScrollBar().maximum() == 0
    assert panel.table.columnWidth(3) >= 120, "Ligands keeps room for one chip"
    assert all(panel.table.columnWidth(column) == 130 for column in (4, 5, COFACTORS_COLUMN))
    panel.close()
