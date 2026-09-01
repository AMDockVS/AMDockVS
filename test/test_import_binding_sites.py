from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select

from amdockvs.io.jobs import IMPORT_GRAPH_OUTPUT
from amdockvs.io.transformers import build_import_graph_payload
from amdockvs.models import BindingSite, ComplexRecord, MoleculeRecord
from ms_flow.core.database import ProjectStore


def _row(source: str, index: int, role: str, **extra) -> dict:
    row = {
        "source": source,
        "source_index": index,
        "name": f"{source}-{index}",
        "primary_role": role,
        "molecule_kind": "protein" if role == "receptor" else "small_molecule",
        "input_format": "pdb",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    row.update(extra)
    return row


def test_import_wires_active_site_and_complex_site_through_the_deferred_relation(tmp_path):
    """FKs to binding_sites close themselves on import, with no per-molecule index.

    It is the only place where a row points at another that does not exist yet: the molecule is
    inserted before its sites. It is declared by position in the spec list and the sink resolves
    it. Two traps covered here: the active one is the middle one (so "take the first" is not
    enough), and the complex site belongs to the RECEPTOR even though complex_spec travels in the
    ligand row.
    """
    receptor = _row(
        "rec.pdb",
        0,
        "receptor",
        binding_site_specs=[
            {"name": "site-a", "source": "ligand", "source_ref": "A:LIG:1"},
            {"name": "site-b", "source": "ligand", "source_ref": "B:LIG:2"},
            {"name": "site-c", "source": "ligand", "source_ref": "C:LIG:3"},
        ],
        active_binding_site_position=1,
    )
    ligand = _row(
        "rec.pdb",
        1,
        "ligand",
        binding_site_specs=[{"name": "ligand-own-site", "source": "manual"}],
        complex_spec={
            "name": "rec-complex",
            "receptor_ref": "molecule::rec.pdb::0",
            "binding_site_position": 1,
            "purpose": "redocking",
        },
    )

    payload = build_import_graph_payload([receptor, ligand])
    store = ProjectStore.open_at(tmp_path / "project.db")
    try:
        store.persist_output_spec(IMPORT_GRAPH_OUTPUT, payload)

        with store.get_session() as session:
            sites = {
                site.name: site
                for site in session.exec(select(BindingSite)).all()
            }
            receptor_record = session.exec(
                select(MoleculeRecord).where(MoleculeRecord.is_receptor == True)  # noqa: E712
            ).one()
            complex_record = session.exec(select(ComplexRecord)).one()

        assert set(sites) == {"site-a", "site-b", "site-c", "ligand-own-site"}
        assert receptor_record.active_binding_site_id == sites["site-b"].id
        assert sites["site-b"].molecule_id == receptor_record.id
        # Position 1 of the receptor, not that of the ligand carrying the complex_spec.
        assert complex_record.binding_site_id == sites["site-b"].id
    finally:
        ProjectStore.clear_cached_stores()
