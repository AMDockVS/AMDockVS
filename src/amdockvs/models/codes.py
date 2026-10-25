"""`codes`: the categorical columns' integers, readable from outside the app (pandas, DuckDB).

The map is `core.vocab.CODES`, the same in every project. A column declared with `code_field`
stores the id with a FK here; `CodeType` binds a name to its id and reads the id back as the
name, for the ORM and for ms_flow's declarative reads alike. '' is stored as NULL.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Integer, UniqueConstraint, event
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

from amdockvs.core.vocab import CODES


class CodeRecord(SQLModel, table=True):
    __tablename__ = "codes"
    __table_args__ = (UniqueConstraint("domain", "name"),)

    id: int = Field(primary_key=True)
    domain: str = ""
    name: str = ""


def _code_rows() -> list[dict[str, Any]]:
    return [{"id": i, "domain": d, "name": n} for d, names in CODES.items() for n, i in names.items()]


@event.listens_for(CodeRecord.__table__, "after_create")
def _seed(target, connection, **_kw) -> None:
    connection.execute(target.insert(), _code_rows())


def ensure_codes(project) -> None:
    """Add the codes appended to `CODES` since the project was made (no-op when none)."""
    from ms_flow.core.database import resolve_project_store

    with resolve_project_store(project).get_session() as session:
        session.exec(insert(CodeRecord.__table__).on_conflict_do_nothing(), params=_code_rows())
        session.commit()


def code_id(domain: str, name: str) -> int:
    """`name`'s id for raw SQL; 0 (no row has it) when the name is not a code."""
    return CODES[domain].get(str(name or "").strip(), 0)


def code_name_sql(column: str) -> str:
    """A SQL expression reading the coded `column` back as its name ('' for NULL)."""
    return f"COALESCE((SELECT name FROM codes WHERE id = {column}), '')"


class CodeType(TypeDecorator):
    impl = Integer
    cache_ok = True

    def __init__(self, domain: str):
        super().__init__()
        self.domain = domain
        self._ids = CODES[domain]
        self._names = {code: name for name, code in self._ids.items()}

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return None
        try:
            return self._ids[str(value)]
        except KeyError:
            raise ValueError(f"{value!r} is not a {self.domain} code: append it to vocab.CODES") from None

    def process_result_value(self, value, dialect):
        return "" if value is None else self._names.get(value, str(value))


def code_field(domain: str, default: str = "", **kwargs: Any) -> Any:
    return Field(default=default, sa_type=CodeType(domain), foreign_key="codes.id", nullable=True, **kwargs)
