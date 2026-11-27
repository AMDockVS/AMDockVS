"""A receptor is docked once per active site, and still once when it has none."""
from __future__ import annotations

from ms_flow.core.database.project import ProjectStore
from payload_seed import seed_payload

from amdockvs.binding_sites.repository import active_sites
from amdockvs.docking.pairs import site_targets
from amdockvs.docking.repository import get_molecule_rows_by_ids
from amdockvs.models import BindingSite, MoleculeRecord, ReceptorPrep


def _box(**extra) -> dict:
    return {"center_x": 1.0, "center_y": 2.0, "center_z": 3.0,
            "size_x": 20.0, "size_y": 20.0, "size_z": 20.0, **extra}


def _project(tmp_path):
    """Two receptors: one with two active sites (the second one flexible) and one plain rigid."""
    store = ProjectStore.open_at(tmp_path / "project.db")
    structure, pdbqt, flex, plain = [seed_payload(store, f"REMARK {name}\n", "pdbqt")
                                     for name in ("structure", "rigid", "flex", "plain")]
    with store.get_session() as session:
        many = MoleculeRecord(name="two sites", is_receptor=True, has_3d=True, current_payload_id=structure)
        rigid = MoleculeRecord(name="no site", is_receptor=True, has_3d=True, current_payload_id=structure)
        session.add_all([many, rigid])
        session.commit()
        first = BindingSite(molecule_id=many.id, name="P1", is_active=True, **_box())
        second = BindingSite(molecule_id=many.id, name="", is_active=True,
                             flex_residues=["A:TYR:100"], **_box(center_x=9.0))
        off = BindingSite(molecule_id=many.id, name="old", is_active=False, **_box(center_x=30.0))
        session.add_all([first, second, off])
        session.commit()
        # The rigid receptor of the whole molecule, plus the one carved for the flexible site.
        session.add(ReceptorPrep(receptor_molecule_id=many.id, family="ad4", rigid_payload_id=pdbqt))
        session.add(ReceptorPrep(receptor_molecule_id=many.id, family="ad4", binding_site_id=second.id,
                                 rigid_payload_id=plain, flex_payload_id=flex))
        session.add(ReceptorPrep(receptor_molecule_id=rigid.id, family="ad4", rigid_payload_id=pdbqt))
        session.commit()
        return store, many.id, rigid.id, first.id, second.id, (pdbqt, plain, flex)


def test_a_receptor_is_one_target_per_active_site(tmp_path):
    store, receptor, _rigid, first, second, (pdbqt, plain, flex) = _project(tmp_path)
    with store.get_session() as session:  # the inactive site is not docked into
        assert [site.id for site in active_sites(session, receptor)] == [first, second]

    row = get_molecule_rows_by_ids(store, [receptor])[receptor]
    targets = site_targets(row)

    assert [target["binding_site_id"] for target in targets] == [first, second]
    # Each site docks into its own box...
    assert [target["grid_engine_payload"]["center"][0] for target in targets] == [1.0, 9.0]
    # ...against its own inputs: the plain rigid receptor, or the one its side chains were carved from.
    assert [target["prepared_payload_id"] for target in targets] == [pdbqt, plain]
    assert [target["flex_payload_id"] for target in targets] == [None, flex]
    # A target is expanded once: whoever wrote its input files keeps it as it is.
    assert site_targets(targets[1]) == [targets[1]]


def test_a_receptor_without_an_active_site_is_one_target_as_it_came(tmp_path):
    """As before multisite: a blind run, or a box the caller passes in, with no site to name."""
    store, _receptor, rigid, _first, _second, (pdbqt, _plain, _flex) = _project(tmp_path)
    row = get_molecule_rows_by_ids(store, [rigid])[rigid]
    targets = site_targets(row)

    assert len(targets) == 1 and targets[0].get("binding_site_id") is None
    assert targets[0]["prepared_payload_id"] == pdbqt and targets[0]["grid_engine_payload"] is None
