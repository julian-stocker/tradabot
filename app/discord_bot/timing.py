"""Where a ``/check`` spends its time.

Diagnosis, not optimisation. The first real invocation took about thirty
seconds and the obvious reactions -- cache the analyst, cache the answer, warm
it in the background -- are all wrong until it is known *which* stage is slow.
Caching an answer that was fast to compute would add staleness for nothing.

So this records stage durations and reports them. It changes no behaviour, and
a run with instrumentation produces exactly the same card as one without.

No secret can enter a timing record: the only values here are stage names, which
are literals in this file, and durations.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class Timings:
    """Stage durations for one request, in milliseconds."""

    stages: dict[str, float] = field(default_factory=dict)
    started: float = field(default_factory=time.perf_counter)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time one named stage. Records even when the body raises."""
        begin = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = round((time.perf_counter() - begin) * 1000, 1)

    @property
    def total_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)

    def as_dict(self) -> dict[str, Any]:
        return {"stages": dict(self.stages), "total_ms": self.total_ms}

    def log(self, *, symbol: str, cold: bool) -> None:
        """Emit one structured line. Symbol only -- never configuration."""
        logger.info(
            "check timing",
            symbol=symbol[:12],
            cold_start=cold,
            total_ms=self.total_ms,
            **{f"{name}_ms": value for name, value in self.stages.items()},
        )
