import locale
import shutil
from pathlib import Path

import pytest

from amdockvs.docking.engines.autogrid import AUTOGRID4
from amdockvs.docking.engines.programs import QVINA_PROGRAM, VINA_PROGRAM
from amdockvs.docking.engines.vina import run_vina_docking_rows
from amdockvs.docking.methods import STANDARD, ZINC, excludes, get_method, receptor_misfit
from amdockvs.docking.pairs import iter_docking_batches_from_rows
from amdockvs.docking.protocols import protocol_hash

DATA = Path(__file__).resolve().parent / "data"
# 1s63 (farnesyltransferase): the box and Zn of the AutoDock-Vina zinc example.
ZN_BOX = {"center": [18.0, 134.0, -1.0], "size": [15.0, 11.25, 18.75]}
ZINC_BOX, EMPTY_BOX = ([10, 10, 10], [4, 4, 4]), ([0, 0, 0], [4, 4, 4])


def _autogrid() -> bool:
    return bool(shutil.which(AUTOGRID4))


def _receptor(tmp_path: Path) -> Path:
    path = tmp_path / "rec.pdb"
    path.write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2 ZN    ZN A 101      10.000  10.000  10.000  1.00  0.00          ZN\n"
    )
    return path


def test_method_gates_programs_and_receptors(tmp_path):
    assert STANDARD.allows("anything") and ZINC.allows("vina", "ad4") and ZINC.allows("adgpu")
    assert not ZINC.allows("vina", "vinardo") and not ZINC.allows("qvina")
    with pytest.raises(ValueError):
        get_method("hydrated")

    rec = _receptor(tmp_path)
    assert receptor_misfit(ZINC, rec, *ZINC_BOX) == "" and receptor_misfit(STANDARD, rec, *EMPTY_BOX) == ""
    assert excludes("standard", rec, *ZINC_BOX) and excludes("zinc", rec, *EMPTY_BOX)
    assert not excludes("zinc", tmp_path / "missing.pdb", *EMPTY_BOX)


def test_method_config_and_identity():
    with pytest.raises(ValueError):
        VINA_PROGRAM.validate_config({"method": "zinc", "scoring_function": "vina"})
    assert VINA_PROGRAM.validate_config({"method": "zinc", "scoring_function": "ad4"})["method"] == "zinc"
    with pytest.raises(ValueError):
        QVINA_PROGRAM.validate_config({"method": "zinc"})

    config = {"scoring_function": "ad4", "exhaustiveness": 8}
    plain = protocol_hash(program="vina", config=config)
    assert protocol_hash(program="vina", config={**config, "method": "standard"}) == plain
    assert protocol_hash(program="vina", config={**config, "method": "zinc"}) != plain


def test_zinc_method_narrows_the_docking_studio(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    from PySide6.QtWidgets import QApplication

    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.ui.tools.docking import preparation_panel, protocol_editor
    from amdockvs.ui.tools.docking.studio import DockingStudioWidget

    app = QApplication.instance() or QApplication([])  # noqa: F841  (kept alive for the widgets)
    monkeypatch.setattr(protocol_editor, "run_async", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(preparation_panel, "run_async", lambda *_args, **_kwargs: None)
    runtime = AMDockVSRuntime()
    runtime.create_project(name="p", folder=tmp_path / "p", description="")
    studio = DockingStudioWidget(runtime=runtime)

    studio.method_combo.setCurrentIndex(studio.method_combo.findData("zinc"))

    protocols = studio._selected_protocols()
    assert [p["program"] for p in protocols] == ["vina"]  # qvina was ticked; it cannot run zinc
    assert protocols[0]["config"]["method"] == "zinc" and protocols[0]["config"]["scoring_function"] == "ad4"
    assert studio.program_list.item([s.key for s in protocol_editor.list_docking_programs()].index("qvina")).isHidden()


def _planned(method: str, scoring: str, tmp_path: Path) -> list[tuple[int, int]]:
    rec = str(_receptor(tmp_path))
    receptors = [{"id": 1, "current_path": rec, "grid_engine_payload": {"center": c, "size": z}}
                 for c, z in (ZINC_BOX,)] + [{"id": 2, "current_path": rec,
                                              "grid_engine_payload": {"center": EMPTY_BOX[0], "size": EMPTY_BOX[1]}}]
    ligands = [{"id": 10, "current_path": "a.sdf"}, {"id": 11, "current_path": "b.sdf"}]
    chunks = iter_docking_batches_from_rows(ligands=ligands, receptors=receptors, output_dir=tmp_path / "out",
                                            batch_size=10, scoring_function=scoring,
                                            engine_config={"method": method})
    return [(pair["receptor_id"], pair["ligand_id"]) for chunk in chunks for pair in chunk["pairs"]]


def test_planning_leaves_out_the_receptors_that_do_not_fit(tmp_path):
    assert _planned("standard", "vina", tmp_path) == [(2, 10), (2, 11)]  # 1 has the Zn in its box
    assert _planned("zinc", "ad4", tmp_path) == [(1, 10), (1, 11)]


def test_api_lists_the_receptors_the_method_hides(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from amdockvs.models.docking import BindingSite
    from amdockvs.models.molecules import MoleculeRecord
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="p", folder=tmp_path / "p", description="")
    rec = str(_receptor(tmp_path))
    ids = {}
    with runtime.workspace.project_db.get_session() as session:
        for name, (center, size) in {"zinc": ZINC_BOX, "plain": EMPTY_BOX, "nobox": (None, None)}.items():
            row = MoleculeRecord(name=name, is_receptor=True, stored_path=rec)
            session.add(row)
            session.commit()
            if center is not None:
                site = BindingSite(molecule_id=row.id, name="active", center_x=center[0], center_y=center[1],
                                   center_z=center[2], size_x=size[0], size_y=size[1], size_z=size[2])
                session.add(site)
                session.commit()
                row.active_binding_site_id = site.id
                session.add(row)
                session.commit()
            ids[name] = row.id

    assert runtime.docking.method_misfit_receptor_ids(method="standard") == [ids["zinc"]]
    assert runtime.docking.method_misfit_receptor_ids(method="zinc") == [ids["plain"]]  # no box: not judged


@pytest.fixture
def c_numeric_locale():
    # A QApplication (the studio test) applies the user's LC_NUMERIC, e.g. es_CO with a decimal
    # comma; the in-process Vina library then misreads the ad4 maps. Jobs dock in a worker process.
    previous = locale.setlocale(locale.LC_NUMERIC)
    locale.setlocale(locale.LC_NUMERIC, "C")
    yield
    locale.setlocale(locale.LC_NUMERIC, previous)


@pytest.mark.skipif(not _autogrid(), reason="autogrid4 not installed")
def test_zinc_method_docks_into_a_zinc_site(tmp_path, c_numeric_locale):
    pair = {"ligand_id": 1, "receptor_id": 2, "ligand_path": str(DATA / "1s63_ligand.pdbqt"),
            "receptor_path": str(DATA / "1s63_zn_site.pdbqt"), "exhaustiveness": 8, "num_modes": 1}
    dock = lambda method, scoring: run_vina_docking_rows(
        pairs=[pair], output_dir=tmp_path / method, box_center=ZN_BOX["center"], box_size=ZN_BOX["size"],
        scoring_function=scoring, seed=1, method=method, vina_cpu=8)

    rows = dock("zinc", "ad4")
    assert rows[0]["score"] is not None and rows[0]["score"] < -9, rows[0]["metrics"].get("error")
    assert any(line.split()[-1] == "TZ" for path in (tmp_path / "zinc").rglob("*_TZ.pdbqt")
               for line in path.read_text().splitlines() if line.startswith(("ATOM", "HETATM")))
    refused = dock("standard", "ad4")[0]  # the same site with plain AD4 maps: that pair fails, nothing docks
    assert refused["score"] is None and "zinc" in refused["metrics"]["error"]
    with pytest.raises(ValueError):
        dock("zinc", "vina")  # AutoDock4Zn has no Vina-scoring form


def test_receptor_table_hides_the_misfits():
    from amdockvs.ui.tools.docking.preparation_panel import FilterOperator, PreparationPanel

    pushed = {}
    panel = PreparationPanel()
    panel._method = lambda: "zinc"
    panel._receptor_type = lambda: "protein"
    panel._unprepared_clause = lambda _role: None
    panel._bind_receptor_table_signals = lambda _widget: None
    widget = type("W", (), {"push_scope": lambda _self, _key, **kw: pushed.update(kw)})()

    panel._misfit_receptors = ("standard", [7])  # computed for another method: not applied
    panel._push_receptor_scope(widget)
    assert [f.field for f in pushed["filters"]] == ["molecule_type"]
    panel._misfit_receptors = ("zinc", [7, 8])
    panel._push_receptor_scope(widget)
    hidden = pushed["filters"][-1]
    assert (hidden.field, hidden.op, hidden.value) == ("id", FilterOperator.NOT_IN, [7, 8])
