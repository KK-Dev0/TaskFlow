"""Deterministic scheduling engine + deadline-risk analysis.

Pure functions: no database, no network. AI may *suggest* goals and breakdowns,
but this module is what places time blocks and flags risk, so results are
predictable and testable.

Algorithm (earliest-deadline-first with priority tie-breaks):
1. Build free intervals per local day inside working hours (today starts at "now").
2. Remove fixed calendar events from those intervals (and report event conflicts).
3. Sort open tasks by (deadline, -priority). Overdue tasks are scheduled ASAP.
4. Allocate each task into the earliest free intervals before its deadline in
   focus chunks (<= 50 min) separated by short buffers.
5. Any minutes that cannot be placed before the deadline => at-risk, with a reason
   and a recovery suggestion.
"""
from datetime import datetime, timedelta

CHUNK = 50          # max focus block (minutes)
BUFFER = 10         # break after a block
MIN_FRAGMENT = 15   # don't create tiny blocks unless the task needs less
DEFAULT_ESTIMATE = 30


def _ceil15(d):
    d = d.replace(second=0, microsecond=0)
    extra = (-d.minute) % 15
    return d + timedelta(minutes=extra)


def _subtract(intervals, s, e):
    out = []
    for a, b in intervals:
        if e <= a or s >= b:
            out.append([a, b])
            continue
        if s > a:
            out.append([a, s])
        if e < b:
            out.append([e, b])
    return out


def remaining_minutes(estimate, logged):
    est = estimate if estimate else DEFAULT_ESTIMATE
    return max(est - int(logged or 0), 10)


def build_plan(tasks, events, now, days=7, tz_offset=0, work_start=540, work_end=1080):
    """tasks: [{id,title,priority,due_at(datetime|None),estimate_min,logged_min}]
    events: [{title,start_at,end_at}] (naive UTC datetimes)
    Returns dict(blocks, risks, days, conflicts)."""
    off = timedelta(minutes=tz_offset)
    now_l = now + off
    days = max(1, min(int(days), 21))

    day_slots = []   # list of (date, [[s,e],...])
    ev_local = sorted(
        [(e["start_at"] + off, e["end_at"] + off, e["title"]) for e in events],
        key=lambda x: x[0])

    for i in range(days):
        d0 = datetime(now_l.year, now_l.month, now_l.day) + timedelta(days=i)
        ws = d0 + timedelta(minutes=work_start)
        we = d0 + timedelta(minutes=work_end)
        if i == 0:
            ws = max(ws, _ceil15(now_l))
        ivs = [[ws, we]] if ws < we else []
        for s, e, _t in ev_local:
            ivs = _subtract(ivs, s, e)
        day_slots.append((d0, ivs))

    capacity = {d0.date().isoformat(): sum(int((b - a).total_seconds() // 60) for a, b in ivs)
                for d0, ivs in day_slots}
    flat = [iv for _d, ivs in day_slots for iv in ivs]

    # conflicts between fixed events
    conflicts = []
    for i in range(len(ev_local)):
        for j in range(i + 1, len(ev_local)):
            a, b = ev_local[i], ev_local[j]
            if b[0] < a[1]:
                conflicts.append({"a": a[2], "b": b[2], "at": (b[0] - off).isoformat() + "Z"})

    def sort_key(t):
        due = t.get("due_at")
        overdue = due is not None and due < now
        return (0 if overdue else 1, due or datetime.max, -int(t.get("priority", 2)), t["id"])

    blocks, risks = [], []
    scheduled_by_day = {}

    for t in sorted(tasks, key=sort_key):
        need = remaining_minutes(t.get("estimate_min"), t.get("logged_min"))
        total_need = need
        due = t.get("due_at")
        overdue = due is not None and due < now
        deadline_l = None if (due is None or overdue) else due + off
        last_end = None

        for iv in flat:
            if need <= 0:
                break
            while need > 0:
                s, e = iv[0], iv[1]
                if deadline_l is not None:
                    e = min(e, deadline_l)
                avail = int((e - s).total_seconds() // 60)
                if avail <= 0 or (avail < MIN_FRAGMENT and avail < need):
                    break
                chunk = min(need, avail, CHUNK)
                end = s + timedelta(minutes=chunk)
                blocks.append({"task_id": t["id"], "title": t["title"],
                               "start": (s - off).isoformat() + "Z",
                               "end": (end - off).isoformat() + "Z",
                               "minutes": chunk, "priority": t.get("priority", 2)})
                key = s.date().isoformat()
                scheduled_by_day[key] = scheduled_by_day.get(key, 0) + chunk
                need -= chunk
                last_end = end
                iv[0] = min(iv[1], end + timedelta(minutes=BUFFER))
                if iv[0] >= iv[1]:
                    break

        base = {"task_id": t["id"], "title": t["title"], "minutes_needed": total_need,
                "due_at": (due.isoformat() + "Z") if due else None}
        if overdue:
            risks.append({**base, "level": "overdue",
                          "reason": "Past its due date" + ("" if need <= 0 else f"; {need} min still unplaced this period"),
                          "suggestion": "Do this first, renegotiate the deadline, or split it into a smaller deliverable."})
        elif need > 0 and due is not None:
            risks.append({**base, "level": "high", "short_minutes": need,
                          "reason": f"Only {total_need - need} of {total_need} min fit before the deadline given working hours and calendar events.",
                          "suggestion": "Free up time, cut scope, move a lower-priority task, or extend the deadline."})
        elif need > 0:
            risks.append({**base, "level": "info", "short_minutes": need,
                          "reason": f"{need} min don't fit in the next {days} days.",
                          "suggestion": "Extend the planning window or reduce other commitments."})
        elif due is not None and last_end is not None and (deadline_l - last_end) < timedelta(hours=2):
            risks.append({**base, "level": "medium",
                          "reason": "Fits, but with less than 2 hours of slack before the deadline.",
                          "suggestion": "Start earlier if you can so a delay doesn't make it late."})

    order = {"overdue": 0, "high": 1, "medium": 2, "info": 3}
    risks.sort(key=lambda r: order[r["level"]])
    blocks.sort(key=lambda b: b["start"])
    day_summary = [{"date": k, "capacity_min": capacity[k], "scheduled_min": scheduled_by_day.get(k, 0)}
                   for k in sorted(capacity)]
    return {"blocks": blocks, "risks": risks, "days": day_summary, "conflicts": conflicts}
