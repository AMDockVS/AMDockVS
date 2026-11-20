"""A project's shard catalog as an import leaves it, for tests that need a library without importing one."""
from pathlib import Path

from ms_flow.core.data import catalog
from ms_flow.core.data.shard import PayloadKind, Serializer, ShardWriter


def write_library_file(path, *, scope, shards=1, records=2, kind=PayloadKind.SMILES) -> Path:
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
                 kind=PayloadKind.SMILES) -> tuple[int, list[int]]:
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
