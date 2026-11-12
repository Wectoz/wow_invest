from datetime import UTC, datetime, timedelta

import pytest

from wow_invest import config
from wow_invest.models import Point

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


def pts(spec):
    """Build Points from (hours_ago, units, min_ask, partial, early[, new_units]) tuples."""
    return [Point(NOW - timedelta(hours=t[0]), *t[1:]) for t in spec]


# ages 0..47: 24 points in [0, 24) -> 24 * 4 * 0.5 = 48 effective sales/day
steady = pts([(h, 300, 20000, 0, 4) for h in range(48)])


@pytest.fixture(autouse=True)
def _fixed_thresholds(monkeypatch):
    """The fixtures are sized for these values; tuning config.py must not move the tests."""
    monkeypatch.setattr(config, "MIN_PROFIT", 30000)
    monkeypatch.setattr(config, "TOP_N", 15)
    monkeypatch.setattr(config, "DETAIL_QUOTAS", {"flip": 25, "cross": 20, "vendor": 10, "hold": 25})
