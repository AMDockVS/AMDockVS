from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class TimestampedRecord(SQLModel):
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


__all__ = ["TimestampedRecord"]
