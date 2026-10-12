"""Deterministic natural-language task parser (no AI key required).

"finish report tomorrow 5pm #college @uni urgent for 2h" ->
title, due_at (UTC), priority, tags, project, estimate, recurrence.
"""
import re
from datetime import datetime, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD_SHORT = {d[:3]: i for i, d in enumerate(WEEKDAYS)}
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
MONTH_IDX = {m: i + 1 for i, m in enumerate(MONTHS)}
MONTH_IDX.update({m[:3]: i + 1 for i, m in enumerate(MONTHS)})
MONTH_RE = "|".join(sorted(MONTH_IDX, key=len, reverse=True))
WD_RE = "|".join(WEEKDAYS + list(WD_SHORT))

DEFAULT_HOUR = 17


def _cut(text, m):
    return (text[:m.start()] + " " + text[m.end():])


def parse(text, now_utc, tz_offset=0):
    raw = (text or "").strip()
    t = " " + raw + " "
    out = {"title": "", "priority": 2, "due_at": None, "estimate_min": None,
           "tags": [], "project": None, "recurrence": "none"}
    now_l = now_utc + timedelta(minutes=tz_offset)

    # tags and project
    for m in re.finditer(r"(?<!\S)#([\w-]{1,24})", t):
        if m.group(1).lower() not in out["tags"]:
            out["tags"].append(m.group(1).lower())
    t = re.sub(r"(?<!\S)#[\w-]{1,24}", " ", t)
    m = re.search(r"(?<!\S)@([\w-]{1,30})", t)
    if m:
        out["project"] = m.group(1)
        t = _cut(t, m)

    # priority
    prio_rules = [
        (r"(?i)(?<!\S)(urgent|asap|p1|!!!)(?!\S)", 4),
        (r"(?i)(?<!\S)(high priority|important|p2|!!)(?!\S)", 3),
        (r"(?i)(?<!\S)(low priority|someday|p4)(?!\S)", 1),
        (r"(?i)(?<!\S)p3(?!\S)", 2),
    ]
    for pat, val in prio_rules:
        m = re.search(pat, t)
        if m:
            out["priority"] = val
            t = _cut(t, m)
            break

    # recurrence
    rec_rules = [
        (r"(?i)\bevery\s+weekday\b|\bweekdays\b", "weekdays"),
        (r"(?i)\bevery\s+day\b|\bdaily\b", "daily"),
        (r"(?i)\bevery\s+week\b|\bweekly\b", "weekly"),
        (r"(?i)\bevery\s+month\b|\bmonthly\b", "monthly"),
    ]
    for pat, val in rec_rules:
        m = re.search(pat, t)
        if m:
            out["recurrence"] = val
            t = _cut(t, m)
            break

    # estimate: "for 2h", "for 45 min"
    m = re.search(r"(?i)\bfor\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", t)
    if m:
        n = float(m.group(1))
        mins = int(round(n * 60)) if m.group(2).lower().startswith("h") else int(round(n))
        out["estimate_min"] = max(5, min(mins, 1440))
        t = _cut(t, m)

    # relative "in N units"
    due_local = None
    rel = re.search(r"(?i)\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\b", t)
    time_set = False
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        if unit.startswith("min"):
            due_local = now_l + timedelta(minutes=n)
        elif unit.startswith(("hour", "hr")):
            due_local = now_l + timedelta(hours=n)
        elif unit.startswith("day"):
            due_local = now_l + timedelta(days=n)
        else:
            due_local = now_l + timedelta(weeks=n)
        time_set = unit.startswith(("min", "hour", "hr"))
        t = _cut(t, rel)

    # explicit time
    hh = mm = None
    m = re.search(r"(?i)\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if not m:
        m = re.search(r"(?i)\b(?:at\s+)?(\d{1,2}):(\d{2})()\b", t)
    if not m:
        m = re.search(r"(?i)\bat\s+(\d{1,2})()()\b", t)
    if m and not rel:
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        ap = (m.group(3) or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            hh, mm = h, mi
            t = _cut(t, m)
    else:
        for word, h in (("noon", 12), ("midnight", 23), ("tonight", 20), ("evening", 18),
                        ("morning", 9), ("afternoon", 15)):
            m2 = re.search(r"(?i)\b%s\b" % word, t)
            if m2 and not rel:
                hh, mm = h, (59 if word == "midnight" else 0)
                if word != "tonight":
                    t = _cut(t, m2)
                break

    # explicit date
    date_local = None
    if not rel:
        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
        if m:
            try:
                date_local = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                t = _cut(t, m)
            except ValueError:
                pass
        if not date_local:
            m = re.search(r"(?i)\b(?:on\s+|by\s+|due\s+)?(%s)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b" % MONTH_RE, t)
            m_alt = re.search(r"(?i)\b(?:on\s+|by\s+|due\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(%s)\b" % MONTH_RE, t)
            mo = day = None
            mm_ = None
            if m:
                mo, day, mm_ = MONTH_IDX[m.group(1).lower()], int(m.group(2)), m
            elif m_alt:
                mo, day, mm_ = MONTH_IDX[m_alt.group(2).lower()], int(m_alt.group(1)), m_alt
            if mm_:
                y = now_l.year
                try:
                    cand = datetime(y, mo, day)
                    if cand.date() < now_l.date():
                        cand = datetime(y + 1, mo, day)
                    date_local = cand
                    t = _cut(t, mm_)
                except ValueError:
                    pass
        if not date_local:
            m = re.search(r"(?i)\b(today|tonight|tomorrow|tmrw|tmr)\b", t)
            if m:
                w = m.group(1).lower()
                base = datetime(now_l.year, now_l.month, now_l.day)
                date_local = base + timedelta(days=1 if w.startswith("t") and w not in ("today", "tonight") else 0)
                if w != "tonight":
                    t = _cut(t, m)
                else:
                    t = _cut(t, m)
                    hh, mm = (20, 0) if hh is None else (hh, mm)
        if not date_local:
            m = re.search(r"(?i)\bnext\s+week\b", t)
            if m:
                base = datetime(now_l.year, now_l.month, now_l.day)
                date_local = base + timedelta(days=7 - base.weekday())  # next Monday
                t = _cut(t, m)
        if not date_local:
            m = re.search(r"(?i)\b(?:on\s+|by\s+|next\s+|this\s+)?(%s)\b" % WD_RE, t)
            if m:
                key = m.group(1).lower()
                wd = WEEKDAYS.index(key) if key in WEEKDAYS else WD_SHORT[key[:3]]
                base = datetime(now_l.year, now_l.month, now_l.day)
                delta = (wd - base.weekday()) % 7 or 7
                date_local = base + timedelta(days=delta)
                t = _cut(t, m)

    if rel and due_local is not None:
        if not time_set:
            due_local = due_local.replace(hour=DEFAULT_HOUR, minute=0, second=0)
    elif date_local is not None:
        h, mi = (hh, mm) if hh is not None else (DEFAULT_HOUR, 0)
        due_local = date_local.replace(hour=h, minute=mi)
    elif hh is not None:
        cand = now_l.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now_l:
            cand += timedelta(days=1)
        due_local = cand

    if due_local is not None:
        out["due_at"] = (due_local - timedelta(minutes=tz_offset)).replace(microsecond=0)

    title = re.sub(r"(?i)\b(?:due|by|on|at)\s*$", "", re.sub(r"\s+", " ", t).strip()).strip(" ,.-")
    title = re.sub(r"(?i)\s+\b(?:due|by|on|at|for)\s*$", "", title).strip(" ,.-")
    if not title:
        title = raw[:200]
    out["title"] = title[:200]
    return out
