"""
Column types.

`EnumString` exists because of a genuine bug: storing a StrEnum in a plain
String column writes fine but loads back as `str`, so every
`job.status is JobStatus.SUBMITTED` in the codebase silently evaluated False
after a reload. `==` still worked (StrEnum compares equal to its value), which
is exactly what made it dangerous — the guards looked correct and tested
correct anywhere the object stayed in the identity map.

Coercing on load means identity comparison works everywhere, which is the
form the rest of the code is written in.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import String, TypeDecorator


class EnumString(TypeDecorator):
    """
    Stores a StrEnum as its string value and returns it as the enum.

    Values are stored as readable strings rather than as a database ENUM so a
    new status does not require a migration on Postgres, and so the column is
    legible in a plain SQL client.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[StrEnum], length: int = 32) -> None:
        self.enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.value
        # Accept a bare string, but validate it: a typo'd status that silently
        # persisted would be far harder to find later.
        return self.enum_class(value).value

    def process_result_value(self, value: Any, dialect: Any) -> StrEnum | None:
        if value is None:
            return None
        try:
            return self.enum_class(value)
        except ValueError:
            # A value written by an older version of the schema. Returning it
            # raw beats raising on read and making the row unloadable.
            return value  # type: ignore[return-value]
