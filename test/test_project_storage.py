from ms_flow.core.database.project import ProjectStore

from amdockvs.project.storage import compact_storage, storage_stats
from payload_seed import seed_payload


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
