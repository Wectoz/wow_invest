from datetime import UTC, datetime, timedelta

from wow_invest.models import Point

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


def pts(spec):
    """Build Points from (hours_ago, units, min_ask, partial, early[, new_units]) tuples."""
    return [Point(NOW - timedelta(hours=t[0]), *t[1:]) for t in spec]


# ages 0..47: 24 points in [0, 24) -> 24 * 4 * 0.5 = 48 effective sales/day
steady = pts([(h, 300, 20000, 0, 4) for h in range(48)])
