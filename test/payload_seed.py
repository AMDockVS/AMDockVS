"""Stored structures for tests that need a molecule, a prepared input or a pose without the importer."""
from amdockvs.core.constants import SERIES_MOLECULES
from amdockvs.core.payloads import structure_payload


def seed_payload(store, data: bytes | str, kind: str = "sdf", series: str = SERIES_MOLECULES) -> int:
    """Store `data` in the project of `store` (a ProjectStore) and return its payload id."""
    with store.get_session() as session:
        (payload_id,) = store.payloads.put(session, [structure_payload(data, kind, series)])
        session.commit()
    return payload_id
