from __future__ import annotations

import ast
from pathlib import Path

import pytest


UI_ROOT = Path(__file__).resolve().parents[1] / "src" / "amdockvs" / "ui"


def test_widgets_do_not_import_sql_or_open_sessions():
    """Phase 3 guard: only the SmartTable adapter may receive project_db."""
    violations: list[str] = []
    for path in UI_ROOT.rglob("*.py"):
        relative = path.relative_to(UI_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [str(node.module or "")]
                )
                if any(name == "sqlalchemy" or name.startswith("sqlalchemy.") for name in modules):
                    violations.append(f"{relative}:{node.lineno}: SQLAlchemy import")
                if isinstance(node, ast.ImportFrom) and node.module == "sqlmodel":
                    if any(alias.name in {"select", "Session", "create_engine"} for alias in node.names):
                        violations.append(f"{relative}:{node.lineno}: SQLModel query/session import")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "get_session":
                    violations.append(f"{relative}:{node.lineno}: explicit session")
            if isinstance(node, ast.Attribute) and node.attr == "project_db":
                if relative != "catalog/common.py":
                    violations.append(f"{relative}:{node.lineno}: direct project_db access")
    assert violations == []


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_runtime_ui_queries_cover_details_sets_filters_and_preparation(tmp_path, monkeypatch):
    from amdockvs import AMDockVSRuntime
    from amdockvs.chemistry.filtering import (
        SmallMoleculeFilterCriteria,
        SmallMoleculeFilterField,
        SmallMoleculeFilterOperator,
        SmallMoleculeFilterRule,
    )
    from amdockvs.models import BindingSite, ComplexRecord, EngineState, LigandActivity, MoleculeRecord
    from amdockvs.vocab import MoleculeType

    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_api", folder=tmp_path / "ui_api", description="phase 3")
        with runtime.molsuite.project_db.get_session() as session:
            receptor = MoleculeRecord(
                name="REC",
                molecule_type=MoleculeType.PROTEIN,
                is_receptor=True,
                n_atoms=100,
            )
            ligand_ok = MoleculeRecord(
                name="LIG_OK",
                molecule_type=MoleculeType.SMALL_MOLECULE,
                is_ligand=True,
                n_atoms=10,
                mw=100.0,
            )
            ligand_fail = MoleculeRecord(
                name="LIG_FAIL",
                molecule_type=MoleculeType.SMALL_MOLECULE,
                is_ligand=True,
                n_atoms=20,
                mw=700.0,
            )
            session.add(receptor)
            session.add(ligand_ok)
            session.add(ligand_fail)
            session.commit()
            session.refresh(receptor)
            session.refresh(ligand_ok)
            session.refresh(ligand_fail)
            receptor_id = int(receptor.id)
            ligand_ok_id = int(ligand_ok.id)
            ligand_fail_id = int(ligand_fail.id)

            site = BindingSite(molecule_id=receptor_id, name="active")
            activity = LigandActivity(molecule_id=ligand_ok_id, value=42.0, unit="nM", activity_type="IC50")
            session.add(site)
            session.add(activity)
            session.commit()
            session.refresh(activity)
            pair = ComplexRecord(
                name="REC-LIG",
                receptor_molecule_id=receptor_id,
                ligand_molecule_id=ligand_ok_id,
                activity_id=int(activity.id),
            )
            failed_state = EngineState(
                molecule_id=ligand_fail_id,
                role_type="ligand",
                engine="ad4",
                is_ready=False,
            )
            session.add(pair)
            session.add(failed_state)
            session.commit()
            session.refresh(pair)
            pair_id = int(pair.id)

        receptor_details = runtime.molecules.details(receptor_id)
        assert receptor_details is not None
        assert receptor_details.molecule.name == "REC"
        assert [item.name for item in receptor_details.binding_sites] == ["active"]
        assert [item.name for item in receptor_details.receptor_complexes] == ["REC-LIG"]

        ligand_details = runtime.molecules.details(ligand_ok_id)
        assert ligand_details is not None
        assert ligand_details.activities[0].value == 42.0
        pair_details = runtime.complexes.details(pair_id)
        assert pair_details is not None
        assert pair_details.receptor.name == "REC"
        assert pair_details.ligand.name == "LIG_OK"

        set_ref = runtime.molecules.create_set([ligand_ok_id], name="keepers")
        assert runtime.molecules.list_sets()[0].id == set_ref.id
        assert runtime.molecules.resolve_set(int(set_ref.id)) == set_ref

        ligand_scope = runtime.molecules.select(role="ligand")
        summary = runtime.docking.preparation_summary(ligand_scope, role_type="ligand", engine="ad4")
        assert summary == {"total": 2, "ready": 0, "failed": 1}

        criteria = SmallMoleculeFilterCriteria(rules=(SmallMoleculeFilterRule(
            field=SmallMoleculeFilterField.MW,
            operator=SmallMoleculeFilterOperator.LTE,
            value=500.0,
        ),))
        counts = runtime.molecules.evaluate_filter(ligand_scope, criteria)
        assert counts == {"scanned": 2, "evaluable": 2, "matched": 1, "skipped": 0, "nonmatched": 1}
        assert runtime.molecules.apply_filter(
            ligand_scope,
            criteria,
            action="enrich",
            reason="test:filter",
        ) == (0, 1)
        assert runtime.molecules.get(ligand_fail_id).excluded is True
    finally:
        runtime.shutdown()
