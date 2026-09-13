"""Point-in-time price index levels for restating old awards in today's money.

Pure stdlib (the console imports p2w from the source tree). The rules:

1. A monthly level may only be used once it had been *published* by ``as_of``.
   GASTAT publishes month M roughly six weeks after M ends, so a prediction
   made on 20 Aug can use June's CPI but not July's. Using July would be a leak
   in any backtest, and in production it is simply not available yet.
2. Inside the published range, a monthly level is read at mid-month and the
   price level on a given day is interpolated linearly between neighbouring
   months, so two dates a week apart do not get an identical level.
3. After the latest published month, the level is projected with the trailing
   12-month rate computed from published months only ("nowcast"). Assuming
   prices froze at the last release would understate recent change, and would
   make many restatement factors exactly 1.0.
4. Before the first month, nothing is extrapolated: :meth:`IndexSeries.factor`
   returns None and the caller falls back to its flat assumption.
"""
from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

#: Days after a month ends before its index level may be used. GASTAT's CPI for
#: month M has been released around the middle of M+1 to early M+2; 45 days is
#: the conservative end of that range.
PUBLICATION_LAG_DAYS = 45

#: The series the market model restates awards with. National CPI is the one
#: GASTAT series that is monthly, long (2013-) and free of base-year breaks in
#: this data; sector series join once they cover enough history.
DEFAULT_SERIES = "cpi.general"

DAYS_PER_YEAR = 365.25


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _mid_month(period: date) -> date:
    return period + timedelta(days=14)


def published_by(period: date, lag_days: int = PUBLICATION_LAG_DAYS) -> date:
    """First date on which the level for ``period`` (a month) may be used."""
    return _next_month(period) + timedelta(days=lag_days)


@dataclass(frozen=True)
class IndexSeries:
    name: str
    points: tuple[tuple[date, float], ...]           # (month start, level), sorted
    lag_days: int = PUBLICATION_LAG_DAYS
    _periods: tuple[date, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_periods", tuple(p for p, _ in self.points))

    @classmethod
    def from_rows(cls, name: str, rows: Iterable[tuple[date, float]],
                  lag_days: int = PUBLICATION_LAG_DAYS) -> IndexSeries:
        clean = sorted((_month_start(p), float(v)) for p, v in rows if v and float(v) > 0)
        return cls(name=name, points=tuple(clean), lag_days=lag_days)

    def __bool__(self) -> bool:
        return bool(self.points)

    def published(self, as_of: date) -> list[tuple[date, float]]:
        """The months a reader on ``as_of`` could have seen."""
        return [(p, v) for p, v in self.points if published_by(p, self.lag_days) <= as_of]

    def latest_published(self, as_of: date) -> date | None:
        pub = self.published(as_of)
        return pub[-1][0] if pub else None

    def level(self, when: date, as_of: date) -> float | None:
        """Price level on day ``when`` as knowable on ``as_of``; None if uncovered."""
        pub = self.published(as_of)
        if not pub or when < pub[0][0]:
            return None
        mids = [_mid_month(p) for p, _ in pub]
        values = [v for _, v in pub]
        if when <= mids[0]:
            return values[0]
        if when <= mids[-1]:
            i = bisect.bisect_left(mids, when)
            d0, d1 = mids[i - 1], mids[i]
            t = (when - d0).days / max((d1 - d0).days, 1)
            return values[i - 1] + t * (values[i] - values[i - 1])
        # Nowcast past the last release with the trailing 12-month rate.
        last_period, last_value = pub[-1]
        year_ago = date(last_period.year - 1, last_period.month, 1)
        j = bisect.bisect_right([p for p, _ in pub], year_ago) - 1
        if j >= 0 and pub[j][0] == year_ago:
            annual = last_value / pub[j][1]
        else:
            annual = 1.0          # not enough published history to project: hold
        years = (when - mids[-1]).days / DAYS_PER_YEAR
        return last_value * annual ** years

    def factor(self, age_days: float, as_of: datetime | date) -> tuple[float, date] | None:
        """(multiplier restating money ``age_days`` old into ``as_of`` money,
        latest index month used). None when the award date predates the series."""
        as_of_d = as_of.date() if isinstance(as_of, datetime) else as_of
        latest = self.latest_published(as_of_d)
        if latest is None:
            return None
        if age_days <= 0:
            return 1.0, latest
        now = self.level(as_of_d, as_of_d)
        then = self.level(as_of_d - timedelta(days=age_days), as_of_d)
        if now is None or then is None:
            return None
        return now / then, latest


async def load_series(conn, name: str = DEFAULT_SERIES) -> IndexSeries | None:
    """Read one series from price_indices; None if the table or series is absent."""
    try:
        rows = await conn.fetch(
            "SELECT period, value FROM price_indices WHERE series = $1 ORDER BY period", name)
    except Exception:  # noqa: BLE001 - no table / fake conn in tests: use the flat model
        return None
    series = IndexSeries.from_rows(name, [(r["period"], float(r["value"])) for r in rows or []])
    return series or None
