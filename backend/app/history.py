"""In-memory ring buffer for indicator samples."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any


class HistoryBuffer:
    """A bounded FIFO of samples keyed by ``t`` (UNIX milliseconds)."""

    def __init__(self, max_samples: int) -> None:
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)

    def append(self, sample: dict[str, Any]) -> None:
        self._samples.append(sample)

    @property
    def latest(self) -> dict[str, Any] | None:
        if not self._samples:
            return None
        return self._samples[-1]

    def since(self, since_ms: int | None) -> list[dict[str, Any]]:
        if since_ms is None:
            return list(self._samples)
        # samples are in insertion order which is also time order
        return [s for s in self._samples if s["t"] > since_ms]

    def __len__(self) -> int:
        return len(self._samples)

    def __iter__(self) -> Iterable[dict[str, Any]]:
        return iter(self._samples)
