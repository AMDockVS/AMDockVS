"""A project's shard catalog as an import leaves it, for tests that need a library without importing one."""
from pathlib import Path

from ms_flow.core.data import catalog
from ms_flow.core.data.shard import Serializer, ShardWriter
from amdockvs.core.constants import ShardKind


def write_library_file(path, *, scope, shards=1, records=2, kind=ShardKind.SMILES) -> Path:
    """A file of `shards` shards (row groups) of `records` records each, ids 0.. in order."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ShardWriter(path, dataset_id=None, shard_id=0, base_id=0, kind=kind, serializer=Serializer.UTF8,
                     slot_count=shards * records, scope=scope) as writer:
        for index in range(shards):
            base = index * records
            for record_id in range(base, base + records):
                writer.add(record_id, b"C")
            writer.end_group(scope=scope, base_id=base, slot_count=records)
    return path


def seed_library(project_db, folder, *, scope, shards=1, records=2, name="lib", generation_id=None,
                 kind=ShardKind.SMILES) -> tuple[int, list[int]]:
    """`write_library_file` into `folder`, registered in the catalog: (file id, the shard ids).
    Shard ids are the catalog's own (`catalog.shard_id`)."""
    path = write_library_file(Path(folder) / f"{name}.parquet", scope=scope, shards=shards, records=records,
                              kind=kind)
    file_id = catalog.add_file(project_db, path, generation_id=generation_id)
    return file_id, [catalog.shard_id(file_id, index) for index in range(shards)]


def file_id_of(project_db, path) -> int:
    """The catalog's id of the file at `path`."""
    from sqlmodel import select

    from ms_flow.core.database.project_models import CatalogFile

    with project_db.get_session() as session:
        return int(session.exec(select(CatalogFile.id).where(CatalogFile.path == str(path))).one())


def seed_prepared(project_db, shard, folder, *, dropped=(), family="ad4") -> Path:
    """A prepared copy of one catalog shard row (a derived file: same ids, ``origin`` = its id),
    registered with its family's role. `dropped`: the ids that failed, each with reason "failed"."""
    from amdockvs.molecules.storage import prepared_role

    base, slots = int(shard["base_id"]), int(shard["slot_count"])
    path = Path(folder) / f"prepared_{family}" / f"{shard['id']}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    with ShardWriter(path, dataset_id=None, shard_id=0, base_id=base, kind=ShardKind.PDBQT,
                     serializer=Serializer.UTF8, slot_count=slots) as writer:
        for record_id in range(base, base + slots):
            if record_id in dropped:
                writer.drop(record_id, "failed")
            else:
                writer.add(record_id, b"REMARK SMILES C\n")
        writer.end_group(scope=shard["scope"], base_id=base, slot_count=slots, origin=int(shard["id"]))
    catalog.add_file(project_db, path, generation_id=shard["generation_id"], role=prepared_role(family))
    return path
