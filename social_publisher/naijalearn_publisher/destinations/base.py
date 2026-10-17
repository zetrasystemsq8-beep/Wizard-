from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class PublishResult:
    external_id: str
    permalink: str | None = None
    http_status: int | None = None


class PublishError(Exception):
    """A publish attempt failed. retryable=False means retrying cannot help (or could duplicate)."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        code: int | None = None,
        subcode: int | None = None,
        trace_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status
        self.code = code
        self.subcode = subcode
        self.trace_id = trace_id
        self.retry_after_seconds = retry_after_seconds


class Publisher(ABC):
    """One publishing destination. Add a new destination by subclassing and registering it."""

    name: str

    @abstractmethod
    def publish(self, post: Mapping[str, Any]) -> PublishResult: ...

    def secrets(self) -> tuple[str, ...]:
        """Strings that must never appear in logs or stored errors."""
        return ()
