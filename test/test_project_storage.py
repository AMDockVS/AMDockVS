"""What the storage panel shows, and how the project's volumes stay healthy by themselves: a fast
check when it opens, and compaction once no job runs and a volume is mostly dead bytes."""
from random import Random

import pytest

import ms_flow.payloads as flow_payloads
from ms_flow.core.database.project import ProjectStore
from payload_seed import seed_payload
from sqlmodel import select

from amdockvs.core.payloads import payload_store
from amdockvs.models.molecules import MoleculeRecord, drop_deferred_indexes, ensure_molecule_indexes
from amdockvs.project.deletion import delete_molecules
from amdockvs.project.storage import check_storage, compact_if_idle, compact_storage, repair_if_idle, storage_stats


def _project(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_payloads, "VOLUME_SIZE", 1 << 10)  # three of these structures per volume
    store = ProjectStore.open_at(tmp_path / "project.db")
    ensure_molecule_indexes(store)  # what opening the project does
    with store.get_session() as session:
        for i in range(12):
            pid = seed_payload(store, Random(i).randbytes(300))  # incompressible: sizes stay as written
            session.add(MoleculeRecord(id=i + 1, name=f"m{i}", is_ligand=True, stored_payload_id=pid,
                                       current_payload_id=pid))
        session.commit()
    return store


def test_compaction_waits_for_the_jobs_and_keeps_what_is_left_readable(tmp_path, monkeypatch):
    store = _project(tmp_path, monkeypatch)
    delete_molecules(store, [i for i in range(1, 13) if i % 3])  # two thirds of every volume die
    assert compact_if_idle(store, busy=True) is None
    stats = compact_if_idle(store, busy=False)
    assert stats["moved"] == 3 and stats["volumes"] >= 1
    assert compact_if_idle(store, busy=False) is None  # nothing left to gain
    with store.get_session() as session:
        kept = session.exec(select(MoleculeRecord).order_by(MoleculeRecord.id)).all()
    assert [row.name for row in kept] == ["m2", "m5", "m8", "m11"]
    assert [store.payloads.get(row.current_payload_id) for row in kept] == [Random(i).randbytes(300) for i in (2, 5, 8, 11)]
    assert payload_store(store).check(full=True).clean


def test_a_volume_that_is_mostly_alive_is_left_alone(tmp_path, monkeypatch):
    store = _project(tmp_path, monkeypatch)
    delete_molecules(store, [1])
    assert compact_if_idle(store, busy=False) is None


def test_a_replaced_structure_is_released_once_the_jobs_end(tmp_path, monkeypatch):
    store = _project(tmp_path, monkeypatch)
    built, rebuilt = seed_payload(store, "built"), seed_payload(store, "rebuilt")
    with store.get_session() as session:
        row = session.get(MoleculeRecord, 1)
        for current in (built, rebuilt):  # two Builds: the second replaces the first one's structure
            row.current_payload_id = current
            session.add(row)
            session.commit()
    assert compact_if_idle(store, busy=True) is None  # a running job's structure has no row yet
    assert compact_if_idle(store, busy=False)["released"] == 1
    assert check_storage(store)[0] == "info"
    assert store.payloads.get(rebuilt) == b"rebuilt" and store.payloads.get(1) == Random(0).randbytes(300)
    with pytest.raises(Exception):
        store.payloads.get(built)


def test_the_check_reports_by_how_bad_it_is(tmp_path, monkeypatch):
    store = _project(tmp_path, monkeypatch)
    assert check_storage(store)[0] == "info"
    seed_payload(store, "nobody holds this")
    level, _, text = check_storage(store)
    assert level == "warning" and "1 unused" in text
    with pytest.raises(RuntimeError):  # a running job's fresh structure has no row yet
        repair_if_idle(store, busy=True)
    assert repair_if_idle(store, busy=False).clean and check_storage(store)[0] == "info"
    seed_payload(store, "nobody holds this")
    next(payload_store(store).directory.glob("*.vol")).unlink()
    assert check_storage(store)[0] == "error"


def test_storage_stats_count_the_volumes_and_the_dead_bytes(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    assert storage_stats(store.payloads).volumes == 0

    ids = [seed_payload(store, "ATOM %d\n" % i, "pdb") for i in range(3)]
    stats = storage_stats(store.payloads)
    assert stats.volumes == 1 and stats.size > 0 and stats.dead_bytes == 0 and stats.healthy

    store.payloads.close()  # a reopened project seals the volume nobody writes to
    store = ProjectStore.open_at(tmp_path / "project.db")
    assert storage_stats(store.payloads).dead_bytes == 0

    store.payloads.release(ids[:2])  # no row references them: their bytes are dead, not yet reclaimed
    stats = storage_stats(store.payloads)
    assert stats.healthy and stats.dead_bytes > 0
    assert "reclaimable" in stats.summary() and storage_stats(store.payloads, full=True).healthy

    assert compact_storage(store.payloads)["volumes"] == 1
    assert storage_stats(store.payloads).dead_bytes == 0


def test_storage_upkeep_waits_for_the_deferred_indexes(tmp_path, monkeypatch):
    store = _project(tmp_path, monkeypatch)
    drop_deferred_indexes(store)  # an import ended; its finalizer has not rebuilt them yet
    assert compact_if_idle(store, busy=False) is None
    with pytest.raises(RuntimeError):
        repair_if_idle(store, busy=False)
    ensure_molecule_indexes(store)
    assert repair_if_idle(store, busy=False).clean
