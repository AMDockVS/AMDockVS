"""QSAR activity ingestion helpers: pIC50 transform, structural keys, CSV column detection."""
import pytest

from amdockvs.qsar.activities import _structural_keys_from_smiles, _to_pchem
from amdockvs.qsar.api import QSARAPI


def test_pchem_transform():
    assert _to_pchem(100, "nM") == 7.0      # 100 nM -> -log10(1e-7)
    assert _to_pchem(1, "uM") == 6.0
    assert _to_pchem(1, "M") == 0.0
    assert _to_pchem(50, "nM") == pytest.approx(7.301, abs=1e-3)
    assert _to_pchem(100, "weird-unit") is None  # unknown unit
    assert _to_pchem(0, "nM") is None            # non-positive concentration


def test_structural_keys_from_smiles():
    pytest.importorskip("rdkit")
    # different SMILES of ethanol canonicalize to the same inchikey
    _c1, ik1 = _structural_keys_from_smiles("CCO")
    _c2, ik2 = _structural_keys_from_smiles("OCC")
    assert ik1 and ik1 == ik2
    assert _structural_keys_from_smiles("not-a-smiles") == (None, None)


def test_detect_match_prefers_structural():
    # auto: inchikey > smiles > name
    assert QSARAPI._detect_match(["SMILES", "InChIKey", "value"], "auto", None) == ("inchikey", "InChIKey")
    assert QSARAPI._detect_match(["smiles", "value"], "auto", None) == ("smiles", "smiles")
    assert QSARAPI._detect_match(["name", "value"], "auto", None) == ("name", "name")
    # explicit match_by finds its column
    assert QSARAPI._detect_match(["canonical_smiles", "v"], "smiles", None)[0] == "smiles"
    with pytest.raises(ValueError):
        QSARAPI._detect_match(["foo", "bar"], "inchikey", None)  # no usable column


def test_sdf_activity_spec_from_property():
    from amdockvs.io.payloads import ImportPrefilterPolicy

    props = [{"key": "IC50", "value_text": "100"}, {"key": "MW", "value_text": "46"}]
    # with transform: 100 nM -> pIC50 7.0, endpoint defaults to the transform name
    spec = ImportPrefilterPolicy(activity_property="IC50", activity_unit="nM", activity_transform="pIC50").activity_spec_from_properties(props)
    assert spec == {"value": 7.0, "unit": "pIC50", "activity_type": "pIC50", "description": "raw=100.0 nM", "source": "sdf:IC50"}
    # raw (no transform): endpoint defaults to the tag name
    raw = ImportPrefilterPolicy(activity_property="IC50").activity_spec_from_properties(props)
    assert raw == {"value": 100.0, "unit": "", "activity_type": "IC50", "source": "sdf:IC50"}
    # missing tag / no property -> None
    assert ImportPrefilterPolicy(activity_property="IC50").activity_spec_from_properties([{"key": "X", "value_text": "1"}]) is None
    assert ImportPrefilterPolicy().activity_spec_from_properties(props) is None


def test_import_activity_builds_the_activity_spec():
    from amdockvs.io.payloads import ImportPrefilterPolicy
    from amdockvs.io.transformers.rows import _apply_sdf_activity

    row = {"source_properties": [{"key": "IC50", "value_text": "100"}]}
    policy = ImportPrefilterPolicy(activity_property="IC50", activity_unit="nM", activity_transform="pIC50")

    _apply_sdf_activity(row, policy)

    assert row["activity_specs"][0]["activity_type"] == "pIC50"


def test_smiles_csv_column_becomes_activity(tmp_path):
    pytest.importorskip("rdkit")
    from amdockvs.io.parsers.readers import iter_import_entries
    from amdockvs.io.payloads import ImportPrefilterPolicy

    csv = tmp_path / "lig.smi"
    csv.write_text("SMILES,Name,IC50\nCCO,ethanol,100\nc1ccccc1,benzene,50\n")
    fmt, entries = iter_import_entries(kind="ligand", file_path=csv)
    rows = list(entries)
    assert fmt == "smiles" and len(rows) == 2
    assert rows[0]["source_properties"]["IC50"] == "100"
    spec = ImportPrefilterPolicy(
        activity_property="IC50", activity_unit="nM", activity_transform="pIC50"
    ).activity_spec_from_properties(
        [{"key": k, "value_text": v} for k, v in rows[0]["source_properties"].items()]
    )
    assert spec["value"] == 7.0 and spec["unit"] == "pIC50"

    # a header-less .smi still parses as plain SMILES with no spurious properties
    plain = tmp_path / "plain.smi"
    plain.write_text("CCO ethanol\nCCC propane\n")
    _f, e2 = iter_import_entries(kind="ligand", file_path=plain)
    rows2 = list(e2)
    assert len(rows2) == 2 and not rows2[0].get("source_properties")


def test_smiles_activity_import_completes_and_persists_records(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    from sqlmodel import select

    from amdockvs.models import ActivityRecord, MoleculeRecord
    from amdockvs.runtime import AMDockVSRuntime

    # Keep all MolSuite and AMDock state isolated from the user's normal project registry.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("AMDOCK_CCD_CACHE", str(tmp_path / "ccd"))

    source = tmp_path / "ligands.smi"
    source.write_text(
        "SMILES,Name,IC50\nCCO,ethanol,100\nc1ccccc1,benzene,50\n",
        encoding="utf-8",
    )
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="activity import", folder=tmp_path / "project")
        jobs = runtime.loader.load_ligands(
            [source],
            executor_name="thread",
            prefilter={
                "activity_property": "IC50",
                "activity_endpoint": "pIC50",
                "activity_unit": "nM",
                "activity_transform": "pIC50",
            },
        )
        statuses = runtime.wait_for_jobs(list(jobs), timeout_s=60)
        assert all(str(job.status).lower().endswith("completed") for job in statuses.values()), statuses

        with runtime.workspace.project_db.get_session() as session:
            molecules = session.exec(select(MoleculeRecord)).all()
            activities = session.exec(select(ActivityRecord)).all()

        assert len(molecules) == 2
        assert len(activities) == 2
        assert {activity.activity_type for activity in activities} == {"pIC50"}
        assert sorted(activity.value for activity in activities) == pytest.approx([7.0, 7.30103], abs=1e-3)
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    test_pchem_transform()
    test_structural_keys_from_smiles()
    test_detect_match_prefers_structural()
    test_sdf_activity_spec_from_property()
    print("OK")


def test_has_activity_is_a_subquery_on_the_activities(tmp_path):
    from ms_flow.core.database import ProjectStore
    from ms_flow.query import db_count

    from amdockvs.models import LigandActivity, MoleculeRecord
    from amdockvs.molecules.api import MoleculeScope, _molecule_spec

    db = ProjectStore.open_at(tmp_path / "project.db")
    with db.get_session() as s:
        s.add_all([MoleculeRecord(id=i, name=f"m{i}", is_ligand=True) for i in (1, 2, 3)])
        s.add(LigandActivity(molecule_id=2, value=5.0))
        s.commit()
    for flag, expected in ((True, 1), (False, 2)):
        assert db_count(db, _molecule_spec(MoleculeScope(filters={"has_activity": flag}))) == expected
