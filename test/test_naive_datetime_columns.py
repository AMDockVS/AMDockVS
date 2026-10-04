"""Every datetime column stays naive, whatever sqlmodel version is installed.

sqlmodel>=0.0.45 maps a bare ``datetime`` field to ``UTCDateTime``, which
rejects the naive ``datetime.now()`` values the whole stack writes. Fields
must pin ``sa_type=DateTime``.
"""

from sqlalchemy import DateTime
from sqlmodel import SQLModel

import amdockvs.models  # noqa: F401  (registers tables)
import ms_flow.core.database.executor_models  # noqa: F401
import ms_flow.core.database.master_models  # noqa: F401
import ms_flow.core.database.project_models  # noqa: F401


def _is_datetime(col_type) -> bool:
    # TypeDecorators (sqlmodel's UTCDateTime) hide DateTime behind impl_instance.
    return isinstance(getattr(col_type, "impl_instance", col_type), DateTime)


def test_datetime_columns_are_plain_naive_datetime():
    bad = [
        f"{table.name}.{col.name}: {col.type!r}"
        for table in SQLModel.metadata.tables.values()
        for col in table.columns
        if _is_datetime(col.type)
        and (type(col.type) is not DateTime or col.type.timezone)
    ]
    assert not bad, bad
