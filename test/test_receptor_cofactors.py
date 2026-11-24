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


def test_receptor_cofactors_names_what_preparation_would_leave_out(tmp_path, monkeypatch):
    """The Receptors step warns when a receptor in scope carries a cofactor and "Include
    cofactors" is off: import showed it, preparation drops it unless that box is ticked. The
    API reads the stored structure, so it reports what import kept and nothing it took off."""
    pytest.importorskip("rdkit")
    from amdockvs.runtime import AMDockVSRuntime

    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = AMDockVSRuntime()
    runtime.create_project(name="cofactors", folder=tmp_path / "project")
    with_both = _receptor(tmp_path / "both.pdb")
    stripped = _receptor(tmp_path / "stripped.pdb")
    jobs = runtime.loader.load_receptors(
        [with_both, stripped], executor_name="thread",
        per_file={str(stripped.resolve()): {"selected_cofactors": []}})
    status = runtime.wait_for_jobs(list(jobs), timeout_s=120, poll_s=0.05)
    assert all(item.status == "completed" for item in status.values())

    found = runtime.docking.receptor_cofactors()
    assert sorted(codes for _name, codes in found.values()) == [("FMN", "HEM")]
    (name,) = [name for name, _codes in found.values()]
    assert name == "both"
    scope = runtime.molecules.select(role="receptor")
    assert runtime.docking.receptor_cofactors(scope) == found

    # Import recorded them: answering reads no structure, a proteome's worth included.
    def no_structure_reads(*_args):
        raise AssertionError("an imported receptor's structure was read")

    with monkeypatch.context() as patch:  # not undo(): that would hand HOME back mid-test
        patch.setattr(type(runtime.docking), "_receptor_cofactor_codes", no_structure_reads)
        assert runtime.docking.receptor_cofactors() == found

    # A receptor with no record (made some other way) is read instead, and still counted.
    from amdockvs.models import MoleculeRecord

    with runtime.workspace.project_db.get_session() as session:
        record = session.get(MoleculeRecord, next(iter(found)))
        extra = dict(record.extra_data)
        extra["structure"] = {key: value for key, value in extra["structure"].items() if key != "components"}
        record.extra_data = extra
        session.add(record)
        session.commit()
    assert runtime.docking.receptor_cofactors() == found


def test_include_cofactors_retypes_only_the_receptors_that_have_one(tmp_path, monkeypatch):
    """The CCD pass behind "Include cofactors" types every non-standard residue. In a mixed
    batch a receptor with no cofactor is prepared as with the box off: nothing of its changes."""
    from types import SimpleNamespace

    from amdockvs.docking.preparation import entities

    seen = {}

    def fake_prepare(*, source_path, output_path, ccd_components, **_kw):
        seen[source_path.read_text().count("HETATM")] = ccd_components
        output_path.write_text("REMARK\n")
        return SimpleNamespace(artifact=SimpleNamespace(path=output_path), metadata={}, warnings=[])

    monkeypatch.setattr(entities, "prepare_receptor_vina_pdbqt", fake_prepare)
    monkeypatch.setattr(entities, "_prepared", lambda text: text)
    with_cofactors = _receptor(tmp_path / "rec.pdb").read_bytes()
    dry = b"\n".join(l for l in with_cofactors.splitlines() if not l.startswith(b"HETATM")) + b"\n"
    assert entities.cofactor_codes(with_cofactors, "pdb") == ("FMN", "HEM")
    assert entities.cofactor_codes(dry, "pdb") == ()
    rows = [{"id": 1, "structure": with_cofactors, "structure_kind": "pdb"},
            {"id": 2, "structure": dry, "structure_kind": "pdb"}]
    result = entities.prepare_entities_rows(entity_kind="receptor", engine="ad4", rows=rows, keep_cofactors=True)
    assert result["failure_count"] == 0, result["failures"]
    assert seen == {9: True, 0: False}
