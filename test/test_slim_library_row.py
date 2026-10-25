"""A library import writes a slim row: PAINS and identity stay unknown (NULL) until asked for,
a curated import computes them, a PAINS filter skips the unknown rows instead of excluding them,
and the alerts job fills them in."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.chemistry.filtering import (
    SmallMoleculeFilterCriteria,
    SmallMoleculeFilterField,
    SmallMoleculeFilterOperator,
    SmallMoleculeFilterRule,
)
from amdockvs.runtime import AMDockVSRuntime

# The second one is a catechol: a PAINS hit.
SMILES = "CCO a\nOc1ccccc1O b\n"


from amdockvs.models.codes import code_id


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _rows(rt, context: str):
    with rt.workspace.project_db.engine.connect() as conn:
        return {row[0]: (_json(row[1]), *row[2:4], str(row[4])) for row in conn.exec_driver_sql(
            "SELECT name, pains_matches, inchikey, canonical_smiles, extra_data FROM molecules "
            "WHERE usage_class = ?", (code_id("usage_class", context),))}


def test_library_rows_are_slim_until_the_alerts_job(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    library = tmp_path / "lib.smi"
    library.write_text(SMILES)
    curated = tmp_path / "ref.smi"
    curated.write_text(SMILES.replace(" a", " ra").replace(" b", " rb"))
    rt = AMDockVSRuntime()
    rt.create_project(name="slim", folder=tmp_path / "project")
    try:
        rt.wait_for_jobs(rt.loader.load_ligands([library]), timeout_s=300)
        rt.wait_for_jobs(rt.loader.load_ligands([curated], primary_context="reference"), timeout_s=300)

        general = _rows(rt, "general")
        assert set(general) == {"a", "b"}
        for pains, key, smiles, extra in general.values():
            assert (pains, key, smiles) == (None, None, None)
            assert '"smiles"' not in extra and "fragmentation" not in extra
        reference = _rows(rt, "reference")
        assert reference["ra"][0] == [] and reference["rb"][0]
        assert all(row[1] and row[2] for row in reference.values())

        scope = rt.molecules.select(role="ligand")
        criteria = SmallMoleculeFilterCriteria(rules=(SmallMoleculeFilterRule(
            field=SmallMoleculeFilterField.PAINS_MATCHES,
            operator=SmallMoleculeFilterOperator.IS_EMPTY,
        ),))
        assert criteria.needs_pains()
        counts = rt.molecules.evaluate_filter(scope, criteria)
        assert (counts["skipped"], counts["evaluable"], counts["matched"]) == (2, 0, 0)  # the library rows: unknown, so skipped
        assert rt.molecules.apply_filter(scope, criteria, action="enrich", reason="t") == (0, 0)

        rt.wait_for_jobs([rt.qsar.compute_descriptors(alerts=True, executor_name="thread")], timeout_s=300)
        general = _rows(rt, "general")
        assert general["a"][0] == [] and general["b"][0]
        assert all(row[1] and row[2] for row in general.values())
        counts = rt.molecules.evaluate_filter(scope, criteria)
        assert (counts["skipped"], counts["evaluable"], counts["matched"]) == (0, 2, 1)
    finally:
        rt.close_project()
