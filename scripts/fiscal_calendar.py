"""Tso fiscal calendar: 13 periods x 28 days, each ending on a Saturday.

Anchor verified two independent ways:
  1. The P03 2026 P&L header literally reads "12 Periods Ending 03/21/2026".
  2. Rolling 28 days forward from that anchor yields P08 2026 = 2026-07-12..2026-08-08,
     which matches the Period 8 window used by the already-shipped vendor
     profitability automation.
"""

import datetime as dt

# End date of period 3, fiscal year 2026 (a Saturday).
ANCHOR = (2026, 3, dt.date(2026, 3, 21))
PERIODS_PER_YEAR = 13
PERIOD_DAYS = 28


def _index(year, period):
    return year * PERIODS_PER_YEAR + (period - 1)


def period_end(year, period):
    if not 1 <= period <= PERIODS_PER_YEAR:
        raise ValueError(f"period must be 1..{PERIODS_PER_YEAR}, got {period}")
    ay, ap, adate = ANCHOR
    delta = _index(year, period) - _index(ay, ap)
    return adate + dt.timedelta(days=delta * PERIOD_DAYS)


def period_start(year, period):
    return period_end(year, period) - dt.timedelta(days=PERIOD_DAYS - 1)


def period_range(year, period):
    return period_start(year, period), period_end(year, period)


def period_for_date(date):
    """Which fiscal (year, period) contains `date`."""
    ay, ap, adate = ANCHOR
    offset = (date - adate).days
    steps = offset // PERIOD_DAYS + (1 if offset % PERIOD_DAYS > 0 else 0)
    idx = _index(ay, ap) + steps
    return divmod(idx, PERIODS_PER_YEAR)[0], divmod(idx, PERIODS_PER_YEAR)[1] + 1


def most_recent_closed_period(today):
    """Latest fiscal period whose end date is strictly before `today`."""
    year, period = period_for_date(today)
    if period_end(year, period) < today:
        return year, period
    idx = _index(year, period) - 1
    return idx // PERIODS_PER_YEAR, idx % PERIODS_PER_YEAR + 1


def subject_for(year, period):
    """TRIS email subject, e.g. 'TSO Preliminary Financial Statement Package | P08 2026'."""
    return f"TSO Preliminary Financial Statement Package | P{period:02d} {year}"


def label_for(year, period):
    """Sales & Trends row label, e.g. '8.2026'."""
    return f"{period}.{year}"
