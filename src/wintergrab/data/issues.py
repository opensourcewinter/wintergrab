"""The :class:`Issue` record shared by schema validation and data-quality checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["Issue"]

SEVERITIES = ("error", "warning", "info")


@dataclass(frozen=True)
class Issue:
    """Something wrong (or suspicious) with a value, a record or a dataset.

    Attributes:
        field: The field concerned (dotted for nested fields), ``None`` for the whole record.
        code: Stable machine-readable kind: ``"missing"``, ``"invalid"``, ``"type"``,
            ``"range"``, ``"pattern"``, ``"enum"``, ``"length"``, ``"encoding"``,
            ``"suspicious"``, ``"duplicate"``, ``"unknown-field"``...
        message: Human-readable explanation.
        severity: ``"error"`` (the value is wrong), ``"warning"`` (it may be) or ``"info"``.
        value: The offending value, when there is one.
    """

    field: str | None
    code: str
    message: str
    severity: str = "error"
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if out["value"] is not None and not isinstance(out["value"], (str, int, float, bool, list, dict)):
            out["value"] = str(out["value"])
        return out

    def __str__(self) -> str:
        where = f"{self.field}: " if self.field else ""
        return f"[{self.severity}] {where}{self.message} ({self.code})"
