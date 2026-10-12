"""Small date/time helpers. All datetimes are stored as naive UTC strings."""
from datetime import datetime, timedelta, timezone
import calendar

FMT = "%Y-%m-%d %H:%M:%S"


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def parse_dt(value):
    """Parse ISO-8601 (with Z/offset or naive-as-UTC) into a naive UTC datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        d = value
    else:
        s = str(value).strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            raise ValueError("Invalid date/time")
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d.replace(microsecond=0)


def to_str(d):
    return d.strftime(FMT) if d else None


def from_str(s):
    return datetime.strptime(s, FMT) if s else None


def iso(s):
    """DB string -> ISO string with Z suffix for the browser."""
    return s.replace(" ", "T") + "Z" if s else None


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


def next_occurrence(d, rule):
    if rule == "daily":
        return d + timedelta(days=1)
    if rule == "weekly":
        return d + timedelta(days=7)
    if rule == "monthly":
        return add_months(d, 1)
    if rule == "weekdays":
        n = d + timedelta(days=1)
        while n.weekday() >= 5:
            n += timedelta(days=1)
        return n
    return None


def local_minutes(d_utc, tz_offset):
    d = d_utc + timedelta(minutes=tz_offset)
    return d.hour * 60 + d.minute


def in_quiet_hours(d_utc, tz_offset, qs, qe):
    if qs == qe:
        return False
    m = local_minutes(d_utc, tz_offset)
    if qs > qe:
        return m >= qs or m < qe
    return qs <= m < qe
