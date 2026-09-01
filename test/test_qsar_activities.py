"""QSAR activity ingestion helpers: pIC50 transform, structural keys, CSV column detection."""
import pytest

from amdockvs.qsar.api import QSARAPI, _structural_keys_from_smiles, _to_pchem


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


def test_import_activity_marks_molecule_has_activity():
    from amdockvs.io.payloads import ImportPrefilterPolicy
    from amdockvs.io.transformers.materializers import _apply_sdf_activity

    row = {"source_properties": [{"key": "IC50", "value_text": "100"}], "has_activity": False}
    policy = ImportPrefilterPolicy(activity_property="IC50", activity_unit="nM", activity_transform="pIC50")

    _apply_sdf_activity(row, policy)

    assert row["has_activity"] is True
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


if __name__ == "__main__":
    test_pchem_transform()
    test_structural_keys_from_smiles()
    test_detect_match_prefers_structural()
    test_sdf_activity_spec_from_property()
    print("OK")
