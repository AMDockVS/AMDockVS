from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class TimestampedRecord(SQLModel):
    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)


__all__ = ["TimestampedRecord"]
