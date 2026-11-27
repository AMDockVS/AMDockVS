"""Parts and receipts for tests that feed `catalog.ContainerWriter` (not a test module)."""
from ms_flow.core.data.shard import PayloadKind, Serializer, ShardWriter


def part(path, *bases):
    """A task's part: one shard of 4 records per base."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with ShardWriter(path, dataset_id=None, shard_id=0, base_id=min(bases), kind=PayloadKind.GENERIC_BYTES,
                     serializer=Serializer.UTF8, slot_count=1 << 20) as writer:
        for base in bases:
            for id_ in range(base, base + 4):
                writer.add(id_, f"r{id_}".encode())
            writer.end_group(scope=1, base_id=base, slot_count=4)
    return path


def receipt(path, part_id):
    return {"part": part_id, "path": str(path)}
