"""Indicator levels and DIFF combinations.

Mirrors the BID/ASK SPOT COIN indicators from trading-platform.ru:
levels are percentage offsets from the current mid price and each "BID P" /
"ASK P" value is the sum of price*quantity for all limit orders inside the
[mid*(1-P/100), mid] / [mid, mid*(1+P/100)] bands respectively.
"""

from __future__ import annotations

from dataclasses import dataclass

# Percentage offsets from mid price that are computed every iteration.
LEVELS: tuple[float, ...] = (1.5, 3.0, 5.0, 8.0, 15.0, 30.0, 60.0)


def fmt(level: float) -> str:
    """Format a level as the API key (e.g. 1.5, 3, 5)."""
    if level.is_integer():
        return str(int(level))
    return format(level, "g")


LEVEL_KEYS: tuple[str, ...] = tuple(fmt(level) for level in LEVELS)


@dataclass(frozen=True)
class DiffSpec:
    """Specification of a DIFF line.

    Symmetric DIFF P  -> bid_level == ask_level
    Cross   DIFF aB-bA -> bid_level != ask_level
    Ring    DIFF outer-inner -> (BID outer - BID inner) + (ASK outer - ASK inner)
    """

    name: str
    bid_outer: float
    ask_outer: float
    bid_inner: float | None = None
    ask_inner: float | None = None
    kind: str = "diff"  # "diff" or "ring"

    def compute(self, bid: dict[str, float], ask: dict[str, float]) -> float:
        bo = bid[fmt(self.bid_outer)]
        ao = ask[fmt(self.ask_outer)]
        if self.kind == "diff":
            return bo - ao
        # ring
        assert self.bid_inner is not None and self.ask_inner is not None
        bi = bid[fmt(self.bid_inner)]
        ai = ask[fmt(self.ask_inner)]
        return (bo - bi) + (ao - ai)


def _symmetric_diffs() -> list[DiffSpec]:
    out: list[DiffSpec] = []
    for level in LEVELS:
        out.append(
            DiffSpec(name=f"DIFF {fmt(level)}", bid_outer=level, ask_outer=level)
        )
    return out


def _cross_diffs() -> list[DiffSpec]:
    pairs: list[tuple[float, float, str]] = [
        (3.0, 8.0, "DIFF 3B-8A"),
        (8.0, 3.0, "DIFF 8B-3A"),
        (8.0, 30.0, "DIFF 8B-30A"),
        (5.0, 15.0, "DIFF 5B-15A"),
        (15.0, 5.0, "DIFF 15B-5A"),
        (8.0, 15.0, "DIFF 8B-15A"),
        (15.0, 30.0, "DIFF 15B-30A"),
        (30.0, 15.0, "DIFF 30B-15A"),
    ]
    return [DiffSpec(name=name, bid_outer=b, ask_outer=a) for b, a, name in pairs]


def _ring_diffs() -> list[DiffSpec]:
    pairs: list[tuple[float, float]] = [
        (30.0, 15.0),
        (30.0, 8.0),
        (15.0, 8.0),
        (8.0, 5.0),
    ]
    return [
        DiffSpec(
            name=f"DIFF {fmt(outer)}-{fmt(inner)}",
            bid_outer=outer,
            ask_outer=outer,
            bid_inner=inner,
            ask_inner=inner,
            kind="ring",
        )
        for outer, inner in pairs
    ]


DIFFS: tuple[DiffSpec, ...] = tuple(_symmetric_diffs() + _cross_diffs() + _ring_diffs())
DIFF_NAMES: tuple[str, ...] = tuple(spec.name for spec in DIFFS)


def compute_diffs(bid: dict[str, float], ask: dict[str, float]) -> dict[str, float]:
    return {spec.name: spec.compute(bid, ask) for spec in DIFFS}
