"""REST API (JSON). Every route is authenticated and scoped to the current user."""
import json
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, g, jsonify, request, session

import ai_agent
import db
import nlp
import planner
from util import (from_str, in_quiet_hours, iso, next_occurrence, now_utc,
                  parse_dt, to_str)

bp = Blueprint("api", __name__, url_prefix="/api")

STATUSES = ("todo", "doing", "done")
RECURRENCES = ("none", "daily", "weekly", "monthly", "weekdays")
AI_HOURLY_LIMIT = 30


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


@bp.errorhandler(ApiError)
def _api_error(e):
    return jsonify(error=e.message), e.status


# ---------------------------------------------------------------- auth/csrf

def current_user():
    if "user" not in g:
        uid = session.get("uid")
        g.user = db.one("SELECT * FROM tf_users WHERE id=?", (uid,)) if uid else None
    return g.user


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            raise ApiError("Please sign in", 401)
        return fn(*a, **kw)
    return wrapper


@bp.before_request
def _guard():
    if not current_user():
        raise ApiError("Please sign in", 401)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        token = request.headers.get("X-CSRF-Token", "")
        if not token or token != session.get("csrf"):
            raise ApiError("Invalid or missing CSRF token", 403)


# ---------------------------------------------------------------- helpers

def body():
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError("JSON object expected")
    return data


def to_int(v, lo, hi, field):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(f"{field} must be a number")
    if n < lo or n > hi:
        raise ApiError(f"{field} must be between {lo} and {hi}")
    return n


def clean_tags(v):
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list):
        return ""
    seen = []
    for t in v:
        t = re.sub(r"[^\w-]", "", str(t).strip().lower().lstrip("#"))[:24]
        if t and t not in seen:
            seen.append(t)
    return ",".join(seen[:10])


def owned_project(uid, pid):
    if pid in (None, "", 0):
        return None
    pid = to_int(pid, 1, 2**31, "project_id")
    if not db.one("SELECT 1 FROM tf_projects WHERE id=? AND user_id=?", (pid, uid)):
        raise ApiError("Project not found", 404)
    return pid


def clean_task(uid, d, partial):
    out = {}
    if "title" in d or not partial:
        t = str(d.get("title") or "").strip()
        if not t:
            raise ApiError("Title is required")
        if len(t) > 200:
            raise ApiError("Title is too long (max 200 characters)")
        out["title"] = t
    if "notes" in d:
        n = str(d["notes"] or "")
        if len(n) > 2000:
            raise ApiError("Notes are too long (max 2000 characters)")
        out["notes"] = n
    if "status" in d:
        if d["status"] not in STATUSES:
            raise ApiError("Invalid status")
        out["status"] = d["status"]
    if "priority" in d:
        out["priority"] = to_int(d["priority"], 1, 4, "priority")
    if "due_at" in d:
        try:
            out["due_at"] = to_str(parse_dt(d["due_at"]))
        except ValueError:
            raise ApiError("Invalid due date")
    if "estimate_min" in d:
        v = d["estimate_min"]
        out["estimate_min"] = None if v in (None, "", 0) else to_int(v, 1, 1440, "estimate_min")
    if "tags" in d:
        out["tags"] = clean_tags(d["tags"])
    if "recurrence" in d:
        if d["recurrence"] not in RECURRENCES:
            raise ApiError("Invalid recurrence")
        out["recurrence"] = d["recurrence"]
    if "project_id" in d:
        out["project_id"] = owned_project(uid, d["project_id"])
    return out


def insert_task(user, f, parent_id=None):
    """Insert a task from already-cleaned fields. Returns new id."""
    uid = user["id"]
    row = {"title": f["title"], "notes": f.get("notes", ""), "status": f.get("status", "todo"),
           "priority": f.get("priority", 2), "due_at": f.get("due_at"),
           "estimate_min": f.get("estimate_min"), "tags": f.get("tags", ""),
           "recurrence": f.get("recurrence", "none"), "project_id": f.get("project_id")}
    if parent_id:
        row["recurrence"] = "none"
    pos = db.one("SELECT COALESCE(MAX(position),0)+1 AS p FROM tf_tasks WHERE user_id=?", (uid,))["p"]
    now = to_str(now_utc())
    done = row["status"] == "done"
    cur = db.run(
        """INSERT INTO tf_tasks(user_id,project_id,parent_id,title,notes,status,priority,due_at,
           estimate_min,tags,recurrence,created_at,completed_at,position)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, row["project_id"], parent_id, row["title"], row["notes"], row["status"], row["priority"],
         row["due_at"], row["estimate_min"], row["tags"], row["recurrence"], now,
         now if done else None, pos))
    tid = cur.lastrowid
    sync_reminder(user, tid)
    return tid


def sync_reminder(user, task_id):
    """Keep exactly one automatic reminder per open task with a due date."""
    t = db.one("SELECT * FROM tf_tasks WHERE id=?", (task_id,))
    db.run("DELETE FROM tf_reminders WHERE task_id=? AND delivered_at IS NULL AND message=''", (task_id,))
    if not t or t["deleted_at"] or t["status"] == "done" or not t["due_at"]:
        return
    due = from_str(t["due_at"])
    remind = due - timedelta(minutes=user["remind_lead"])
    if due <= now_utc():
        return
    db.run("INSERT OR IGNORE INTO tf_reminders(user_id,task_id,remind_at) VALUES(?,?,?)",
           (user["id"], task_id, to_str(remind)))


def get_task(uid, tid, include_deleted=False):
    r = db.one("SELECT * FROM tf_tasks WHERE id=? AND user_id=?", (tid, uid))
    if not r or (r["deleted_at"] and not include_deleted):
        raise ApiError("Task not found", 404)
    return r


def task_dict(r, subs=None, logged=0):
    d = {"id": r["id"], "title": r["title"], "notes": r["notes"], "status": r["status"],
         "priority": r["priority"], "due_at": iso(r["due_at"]), "estimate_min": r["estimate_min"],
         "tags": [t for t in r["tags"].split(",") if t], "recurrence": r["recurrence"],
         "project_id": r["project_id"], "parent_id": r["parent_id"],
         "created_at": iso(r["created_at"]), "completed_at": iso(r["completed_at"]),
         "logged_min": int(logged // 60)}
    if subs is not None:
        d["subtasks"] = subs
    return d


def logged_seconds(uid):
    rows = db.query("SELECT task_id, SUM(duration_sec) s FROM tf_timelogs WHERE user_id=? AND task_id IS NOT NULL GROUP BY task_id", (uid,))
    return {r["task_id"]: r["s"] for r in rows}


def local_now(user):
    return now_utc() + timedelta(minutes=user["tz_offset"])


def local_date_of(user, s):
    return (from_str(s) + timedelta(minutes=user["tz_offset"])).date()


def set_status(user, tid, status):
    """Change status; completing a recurring task spawns the next one exactly once."""
    t = get_task(user["id"], tid)
    if t["status"] == status:
        return
    now = to_str(now_utc())
    db.run("UPDATE tf_tasks SET status=?, completed_at=? WHERE id=?",
           (status, now if status == "done" else None, tid))
    if status == "done" and t["recurrence"] != "none" and t["due_at"] and not t["parent_id"]:
        claim = db.run("INSERT OR IGNORE INTO tf_spawns(task_id) VALUES(?)", (tid,))
        if claim.rowcount == 1:
            nxt = from_str(t["due_at"])
            nowu = now_utc()
            nxt = next_occurrence(nxt, t["recurrence"])
            while nxt and nxt <= nowu:
                nxt = next_occurrence(nxt, t["recurrence"])
            new_id = insert_task(user, {
                "title": t["title"], "notes": t["notes"], "priority": t["priority"],
                "due_at": to_str(nxt), "estimate_min": t["estimate_min"], "tags": t["tags"],
                "recurrence": t["recurrence"], "project_id": t["project_id"]})
            db.run("UPDATE tf_spawns SET next_task_id=? WHERE task_id=?", (new_id, tid))
    sync_reminder(user, tid)


# ---------------------------------------------------------------- profile

SETTINGS = {"work_start": (0, 1439), "work_end": (1, 1440), "quiet_start": (0, 1439),
            "quiet_end": (0, 1439), "remind_lead": (0, 10080), "pomo_work": (1, 180),
            "pomo_short": (1, 60), "pomo_long": (1, 120), "pomo_cycles": (1, 12),
            "tz_offset": (-840, 840)}


def user_dict(u):
    d = {"id": u["id"], "email": u["email"], "name": u["name"]}
    d.update({k: u[k] for k in SETTINGS})
    return d


def project_dict(r):
    return {"id": r["id"], "name": r["name"], "color": r["color"]}


@bp.get("/me")
def me():
    u = current_user()
    projects = [project_dict(p) for p in db.query("SELECT * FROM tf_projects WHERE user_id=? ORDER BY name", (u["id"],))]
    return jsonify(user=user_dict(u), projects=projects,
                   ai={"enabled": ai_agent.enabled(), "provider": ai_agent.provider()})


@bp.patch("/me")
def update_me():
    u, d = current_user(), body()
    sets, args = [], []
    if "name" in d:
        name = str(d["name"]).strip()
        if not name or len(name) > 60:
            raise ApiError("Name must be 1-60 characters")
        sets.append("name=?"); args.append(name)
    for k, (lo, hi) in SETTINGS.items():
        if k in d:
            sets.append(f"{k}=?"); args.append(to_int(d[k], lo, hi, k))
    ws = d.get("work_start", u["work_start"])
    we = d.get("work_end", u["work_end"])
    if int(ws) >= int(we):
        raise ApiError("Work day must end after it starts")
    if sets:
        db.run(f"UPDATE tf_users SET {', '.join(sets)} WHERE id=?", (*args, u["id"]))
    g.pop("user", None)
    return jsonify(user=user_dict(current_user()))


# ---------------------------------------------------------------- projects

@bp.post("/projects")
def create_project():
    u, d = current_user(), body()
    name = str(d.get("name") or "").strip()
    if not name or len(name) > 40:
        raise ApiError("Project name must be 1-40 characters")
    color = str(d.get("color") or "#8b7cf6")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ApiError("Invalid color")
    if db.one("SELECT 1 FROM tf_projects WHERE user_id=? AND lower(name)=lower(?)", (u["id"], name)):
        raise ApiError("A project with this name already exists", 409)
    cur = db.run("INSERT INTO tf_projects(user_id,name,color,created_at) VALUES(?,?,?,?)",
                 (u["id"], name, color, to_str(now_utc())))
    return jsonify(project_dict(db.one("SELECT * FROM tf_projects WHERE id=?", (cur.lastrowid,)))), 201


@bp.delete("/projects/<int:pid>")
def delete_project(pid):
    u = current_user()
    owned_project(u["id"], pid)
    db.run("DELETE FROM tf_projects WHERE id=?", (pid,))
    return jsonify(ok=True)


# ---------------------------------------------------------------- tasks

def _matches(t, q):
    hay = " ".join([t["title"], t["notes"], t["tags"]]).lower()
    return all(w in hay for w in q.lower().split())


@bp.get("/tasks")
def list_tasks():
    u = current_user()
    uid = u["id"]
    a = request.args
    trash = a.get("trash") == "1"
    rows = db.query(
        "SELECT * FROM tf_tasks WHERE user_id=? AND deleted_at IS %s NULL ORDER BY position DESC LIMIT 3000"
        % ("NOT" if trash else ""), (uid,))
    logged = logged_seconds(uid)
    now = now_utc()
    today = local_now(u).date()

    subs_by_parent = defaultdict(list)
    tops = []
    for r in rows:
        if r["parent_id"] and not trash:
            subs_by_parent[r["parent_id"]].append(r)
        else:
            tops.append(r)

    def keep(r, subs):
        if a.get("status") and a["status"] != "all":
            if a["status"] == "open":
                if r["status"] == "done":
                    return False
            elif r["status"] != a["status"]:
                return False
        if a.get("project") and str(r["project_id"] or "none") != a["project"]:
            return False
        if a.get("priority") and str(r["priority"]) != a["priority"]:
            return False
        if a.get("tag") and a["tag"].lower() not in r["tags"].split(","):
            return False
        due = a.get("due")
        if due:
            if due == "none":
                if r["due_at"]:
                    return False
            else:
                if not r["due_at"]:
                    return False
                dd = local_date_of(u, r["due_at"])
                if due == "overdue" and not (from_str(r["due_at"]) < now and r["status"] != "done"):
                    return False
                if due == "today" and dd != today:
                    return False
                if due == "week" and not (today <= dd <= today + timedelta(days=6)):
                    return False
        q = (a.get("q") or "").strip()
        if q and not (_matches(r, q) or any(_matches(s, q) for s in subs)):
            return False
        return True

    result = []
    for r in tops:
        subs = subs_by_parent.get(r["id"], [])
        if not keep(r, subs):
            continue
        sd = [task_dict(s, None, logged.get(s["id"], 0)) for s in sorted(subs, key=lambda x: x["id"])]
        result.append(task_dict(r, sd, logged.get(r["id"], 0)))

    sort = a.get("sort", "smart")
    far = "9999"
    if sort == "due":
        result.sort(key=lambda t: (t["status"] == "done", t["due_at"] or far))
    elif sort == "priority":
        result.sort(key=lambda t: (t["status"] == "done", -t["priority"], t["due_at"] or far))
    elif sort == "created":
        result.sort(key=lambda t: t["created_at"], reverse=True)
    else:  # smart: open first, overdue/soonest first, then priority
        result.sort(key=lambda t: (t["status"] == "done", t["due_at"] or far, -t["priority"]))
    return jsonify(tasks=result, count=len(result))


@bp.post("/tasks")
def create_task():
    u, d = current_user(), body()
    f = clean_task(u["id"], d, partial=False)
    parent_id = None
    if d.get("parent_id"):
        p = get_task(u["id"], to_int(d["parent_id"], 1, 2**31, "parent_id"))
        if p["parent_id"]:
            raise ApiError("Subtasks cannot have subtasks")
        parent_id = p["id"]
        f.setdefault("project_id", p["project_id"])
    tid = insert_task(u, f, parent_id)
    return jsonify(task_dict(get_task(u["id"], tid))), 201


@bp.get("/tasks/<int:tid>")
def read_task(tid):
    u = current_user()
    r = get_task(u["id"], tid)
    subs = db.query("SELECT * FROM tf_tasks WHERE parent_id=? AND deleted_at IS NULL ORDER BY id", (tid,))
    lg = logged_seconds(u["id"])
    return jsonify(task_dict(r, [task_dict(s, None, lg.get(s["id"], 0)) for s in subs], lg.get(tid, 0)))


@bp.patch("/tasks/<int:tid>")
def update_task(tid):
    u, d = current_user(), body()
    get_task(u["id"], tid)
    f = clean_task(u["id"], d, partial=True)
    status = f.pop("status", None)
    if f:
        if "recurrence" in f and db.one("SELECT parent_id FROM tf_tasks WHERE id=?", (tid,))["parent_id"]:
            f["recurrence"] = "none"
        cols = ", ".join(f"{k}=?" for k in f)
        db.run(f"UPDATE tf_tasks SET {cols} WHERE id=?", (*f.values(), tid))
    if status:
        set_status(u, tid, status)
    sync_reminder(u, tid)
    return jsonify(task_dict(get_task(u["id"], tid)))


@bp.delete("/tasks/<int:tid>")
def delete_task(tid):
    u = current_user()
    get_task(u["id"], tid, include_deleted=True)
    if request.args.get("purge") == "1":
        db.run("DELETE FROM tf_tasks WHERE id=? OR parent_id=?", (tid, tid))
    else:
        now = to_str(now_utc())
        db.run("UPDATE tf_tasks SET deleted_at=? WHERE (id=? OR parent_id=?) AND deleted_at IS NULL", (now, tid, tid))
        db.run("DELETE FROM tf_reminders WHERE task_id IN (SELECT id FROM tf_tasks WHERE id=? OR parent_id=?)", (tid, tid))
    return jsonify(ok=True)


@bp.post("/tasks/<int:tid>/restore")
def restore_task(tid):
    u = current_user()
    get_task(u["id"], tid, include_deleted=True)
    db.run("UPDATE tf_tasks SET deleted_at=NULL WHERE id=? OR parent_id=?", (tid, tid))
    sync_reminder(u, tid)
    return jsonify(task_dict(get_task(u["id"], tid)))


@bp.post("/tasks/clear-completed")
def clear_completed():
    u = current_user()
    now = to_str(now_utc())
    cur = db.run("UPDATE tf_tasks SET deleted_at=? WHERE user_id=? AND status='done' AND deleted_at IS NULL AND parent_id IS NULL",
                 (now, u["id"]))
    return jsonify(cleared=cur.rowcount)


@bp.post("/tasks/bulk")
def bulk_tasks():
    u, d = current_user(), body()
    ids = d.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 500:
        raise ApiError("Provide 1-500 task ids")
    ids = [to_int(i, 1, 2**31, "id") for i in ids]
    action, value = d.get("action"), d.get("value")
    mine = [r["id"] for r in db.query(
        "SELECT id FROM tf_tasks WHERE user_id=? AND deleted_at IS NULL AND id IN (%s)" % ",".join("?" * len(ids)),
        (u["id"], *ids))]
    if action in ("done", "todo", "doing"):
        for i in mine:
            set_status(u, i, action)
    elif action == "delete":
        now = to_str(now_utc())
        for i in mine:
            db.run("UPDATE tf_tasks SET deleted_at=? WHERE id=? OR parent_id=?", (now, i, i))
    elif action == "priority":
        p = to_int(value, 1, 4, "priority")
        for i in mine:
            db.run("UPDATE tf_tasks SET priority=? WHERE id=?", (p, i))
    elif action == "project":
        pid = owned_project(u["id"], value)
        for i in mine:
            db.run("UPDATE tf_tasks SET project_id=? WHERE id=?", (pid, i))
    else:
        raise ApiError("Unknown bulk action")
    return jsonify(updated=len(mine))


# ---------------------------------------------------------------- reminders

@bp.post("/tasks/<int:tid>/reminders")
def add_reminder(tid):
    u, d = current_user(), body()
    get_task(u["id"], tid)
    try:
        at = parse_dt(d.get("remind_at"))
    except ValueError:
        raise ApiError("Invalid reminder time")
    if not at:
        raise ApiError("remind_at is required")
    msg = (str(d.get("message") or "Reminder").strip() or "Reminder")[:120]
    db.run("INSERT OR IGNORE INTO tf_reminders(user_id,task_id,remind_at,message) VALUES(?,?,?,?)",
           (u["id"], tid, to_str(at), msg))
    return jsonify(ok=True), 201


@bp.get("/reminders/due")
def due_reminders():
    u = current_user()
    now = now_utc()
    if in_quiet_hours(now, u["tz_offset"], u["quiet_start"], u["quiet_end"]):
        return jsonify(reminders=[], quiet=True)
    rows = db.query(
        """SELECT r.id, r.task_id, r.remind_at, r.message, t.title, t.due_at
           FROM tf_reminders r JOIN tf_tasks t ON t.id=r.task_id
           WHERE r.user_id=? AND r.delivered_at IS NULL AND r.remind_at<=?
             AND t.deleted_at IS NULL AND t.status!='done'
           ORDER BY r.remind_at LIMIT 20""", (u["id"], to_str(now)))
    return jsonify(quiet=False, reminders=[
        {"id": r["id"], "task_id": r["task_id"], "title": r["title"], "message": r["message"],
         "due_at": iso(r["due_at"]), "remind_at": iso(r["remind_at"])} for r in rows])


@bp.post("/reminders/<int:rid>/ack")
def ack_reminder(rid):
    u = current_user()
    db.run("UPDATE tf_reminders SET delivered_at=? WHERE id=? AND user_id=? AND delivered_at IS NULL",
           (to_str(now_utc()), rid, u["id"]))
    return jsonify(ok=True)


@bp.post("/reminders/<int:rid>/snooze")
def snooze_reminder(rid):
    u, d = current_user(), body()
    mins = to_int(d.get("minutes", 10), 1, 1440, "minutes")
    r = db.one("SELECT * FROM tf_reminders WHERE id=? AND user_id=?", (rid, u["id"]))
    if not r:
        raise ApiError("Reminder not found", 404)
    new_at = now_utc() + timedelta(minutes=mins)
    try:
        db.run("UPDATE tf_reminders SET remind_at=?, delivered_at=NULL WHERE id=?", (to_str(new_at), rid))
    except Exception:
        db.run("UPDATE tf_reminders SET delivered_at=? WHERE id=?", (to_str(now_utc()), rid))
    return jsonify(ok=True, remind_at=iso(to_str(new_at)))


# ---------------------------------------------------------------- events

def clean_event(d):
    title = str(d.get("title") or "").strip()
    if not title or len(title) > 120:
        raise ApiError("Event title must be 1-120 characters")
    try:
        s, e = parse_dt(d.get("start_at")), parse_dt(d.get("end_at"))
    except ValueError:
        raise ApiError("Invalid event time")
    if not s or not e:
        raise ApiError("Start and end are required")
    if e <= s:
        raise ApiError("Event must end after it starts")
    if e - s > timedelta(days=14):
        raise ApiError("Event is too long")
    return title, s, e, str(d.get("notes") or "")[:1000]


def event_dict(r):
    return {"id": r["id"], "title": r["title"], "start_at": iso(r["start_at"]),
            "end_at": iso(r["end_at"]), "notes": r["notes"]}


@bp.get("/events")
def list_events():
    u = current_user()
    try:
        s = parse_dt(request.args.get("from")) or (now_utc() - timedelta(days=45))
        e = parse_dt(request.args.get("to")) or (now_utc() + timedelta(days=120))
    except ValueError:
        raise ApiError("Invalid range")
    rows = db.query("SELECT * FROM tf_events WHERE user_id=? AND end_at>=? AND start_at<=? ORDER BY start_at",
                    (u["id"], to_str(s), to_str(e)))
    return jsonify(events=[event_dict(r) for r in rows])


@bp.post("/events")
def create_event():
    u = current_user()
    title, s, e, notes = clean_event(body())
    cur = db.run("INSERT INTO tf_events(user_id,title,start_at,end_at,notes) VALUES(?,?,?,?,?)",
                 (u["id"], title, to_str(s), to_str(e), notes))
    return jsonify(event_dict(db.one("SELECT * FROM tf_events WHERE id=?", (cur.lastrowid,)))), 201


@bp.patch("/events/<int:eid>")
def update_event(eid):
    u, d = current_user(), body()
    r = db.one("SELECT * FROM tf_events WHERE id=? AND user_id=?", (eid, u["id"]))
    if not r:
        raise ApiError("Event not found", 404)
    merged = {"title": r["title"], "start_at": r["start_at"], "end_at": r["end_at"], "notes": r["notes"], **d}
    title, s, e, notes = clean_event(merged)
    db.run("UPDATE tf_events SET title=?, start_at=?, end_at=?, notes=? WHERE id=?",
           (title, to_str(s), to_str(e), notes, eid))
    return jsonify(event_dict(db.one("SELECT * FROM tf_events WHERE id=?", (eid,))))


@bp.delete("/events/<int:eid>")
def delete_event(eid):
    u = current_user()
    if not db.one("SELECT 1 FROM tf_events WHERE id=? AND user_id=?", (eid, u["id"])):
        raise ApiError("Event not found", 404)
    db.run("DELETE FROM tf_events WHERE id=?", (eid,))
    return jsonify(ok=True)


# ---------------------------------------------------------------- time logs

@bp.post("/timelogs")
def create_timelog():
    u, d = current_user(), body()
    secs = to_int(d.get("duration_sec"), 1, 43200, "duration_sec")
    kind = d.get("kind", "pomodoro")
    if kind not in ("pomodoro", "stopwatch", "countdown"):
        raise ApiError("Invalid timer kind")
    tid = d.get("task_id") or None
    if tid:
        get_task(u["id"], to_int(tid, 1, 2**31, "task_id"))
    try:
        started = parse_dt(d.get("started_at")) or (now_utc() - timedelta(seconds=secs))
    except ValueError:
        raise ApiError("Invalid start time")
    cid = str(d.get("client_id") or "")[:64] or None
    cur = db.run("INSERT OR IGNORE INTO tf_timelogs(user_id,task_id,client_id,started_at,duration_sec,kind) VALUES(?,?,?,?,?,?)",
                 (u["id"], tid, cid, to_str(started), secs, kind))
    return jsonify(ok=True, duplicate=cur.rowcount == 0), 201


@bp.get("/timelogs")
def list_timelogs():
    u = current_user()
    days = to_int(request.args.get("days", 14), 1, 365, "days")
    since = to_str(now_utc() - timedelta(days=days))
    rows = db.query(
        """SELECT l.id,l.task_id,l.started_at,l.duration_sec,l.kind,t.title
           FROM tf_timelogs l LEFT JOIN tf_tasks t ON t.id=l.task_id
           WHERE l.user_id=? AND l.started_at>=? ORDER BY l.started_at DESC LIMIT 300""", (u["id"], since))
    return jsonify(logs=[{"id": r["id"], "task_id": r["task_id"], "title": r["title"],
                          "started_at": iso(r["started_at"]), "duration_sec": r["duration_sec"],
                          "kind": r["kind"]} for r in rows])


# ---------------------------------------------------------------- planner

def plan_for(u, days):
    uid = u["id"]
    now = now_utc()
    rows = db.query("SELECT * FROM tf_tasks WHERE user_id=? AND deleted_at IS NULL AND status!='done'", (uid,))
    lg = logged_seconds(uid)
    by_id = {r["id"]: r for r in rows}
    open_children = defaultdict(int)
    for r in rows:
        if r["parent_id"]:
            open_children[r["parent_id"]] += 1
    items = []
    for r in rows:
        if open_children.get(r["id"]):
            continue  # schedule the subtasks instead of the parent
        due = r["due_at"]
        if not due and r["parent_id"] and r["parent_id"] in by_id:
            due = by_id[r["parent_id"]]["due_at"]
        title = r["title"]
        if r["parent_id"] and r["parent_id"] in by_id:
            title = f"{by_id[r['parent_id']]['title']} - {title}"
        items.append({"id": r["id"], "title": title, "priority": r["priority"],
                      "due_at": from_str(due), "estimate_min": r["estimate_min"],
                      "logged_min": lg.get(r["id"], 0) // 60})
    end = now + timedelta(days=days + 1)
    evs = db.query("SELECT * FROM tf_events WHERE user_id=? AND end_at>=? AND start_at<=?",
                   (uid, to_str(now), to_str(end)))
    events = [{"title": e["title"], "start_at": from_str(e["start_at"]), "end_at": from_str(e["end_at"])} for e in evs]
    return planner.build_plan(items, events, now, days, u["tz_offset"], u["work_start"], u["work_end"])


@bp.get("/planner")
def get_planner():
    u = current_user()
    days = to_int(request.args.get("days", 7), 1, 21, "days")
    return jsonify(plan_for(u, days))


# ---------------------------------------------------------------- analytics

def analytics(u, days=14):
    uid = u["id"]
    off = timedelta(minutes=u["tz_offset"])
    now = now_utc()
    today = (now + off).date()
    rows = db.query("SELECT * FROM tf_tasks WHERE user_id=? AND deleted_at IS NULL", (uid,))
    logs = db.query("SELECT * FROM tf_timelogs WHERE user_id=?", (uid,))
    day_keys = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
    done_by, created_by, focus_by = defaultdict(int), defaultdict(int), defaultdict(int)
    for r in rows:
        created_by[(from_str(r["created_at"]) + off).date()] += 1
        if r["completed_at"]:
            done_by[(from_str(r["completed_at"]) + off).date()] += 1
    for l in logs:
        focus_by[(from_str(l["started_at"]) + off).date()] += l["duration_sec"]

    done_dates = set(done_by)
    streak, cursor = 0, today
    if cursor not in done_dates:
        cursor -= timedelta(days=1)
    while cursor in done_dates:
        streak += 1
        cursor -= timedelta(days=1)

    open_rows = [r for r in rows if r["status"] != "done"]
    overdue = [r for r in open_rows if r["due_at"] and from_str(r["due_at"]) < now]
    done_with_due = [r for r in rows if r["status"] == "done" and r["due_at"] and r["completed_at"]]
    on_time = sum(1 for r in done_with_due if from_str(r["completed_at"]) <= from_str(r["due_at"]))
    last_minute = sum(1 for r in done_with_due
                      if timedelta(0) <= from_str(r["due_at"]) - from_str(r["completed_at"]) <= timedelta(hours=6))
    lg = logged_seconds(uid)
    ratios = [lg[r["id"]] / 60 / r["estimate_min"] for r in rows
              if r["status"] == "done" and r["estimate_min"] and lg.get(r["id"])]
    projects = {p["id"]: p["name"] for p in db.query("SELECT id,name FROM tf_projects WHERE user_id=?", (uid,))}
    by_project = defaultdict(int)
    by_priority = defaultdict(int)
    for r in open_rows:
        by_project[projects.get(r["project_id"], "No project")] += 1
        by_priority[r["priority"]] += 1
    week_cut = today - timedelta(days=6)
    done_week = sum(v for k, v in done_by.items() if k >= week_cut)
    focus_week = sum(v for k, v in focus_by.items() if k >= week_cut) // 60

    return {
        "days": [{"date": k.isoformat(), "done": done_by.get(k, 0), "created": created_by.get(k, 0),
                  "focus_min": focus_by.get(k, 0) // 60} for k in day_keys],
        "streak": streak,
        "open": len(open_rows), "overdue": len(overdue),
        "done_total": sum(1 for r in rows if r["status"] == "done"),
        "done_week": done_week, "focus_week_min": focus_week,
        "done_today": done_by.get(today, 0), "focus_today_min": focus_by.get(today, 0) // 60,
        "on_time_rate": round(100 * on_time / len(done_with_due)) if done_with_due else None,
        "last_minute_rate": round(100 * last_minute / len(done_with_due)) if done_with_due else None,
        "estimate_accuracy": round(sum(ratios) / len(ratios), 2) if ratios else None,
        "by_project": dict(by_project),
        "by_priority": {str(k): v for k, v in sorted(by_priority.items(), reverse=True)},
    }


@bp.get("/analytics")
def get_analytics():
    u = current_user()
    return jsonify(analytics(u, to_int(request.args.get("days", 14), 7, 60, "days")))


@bp.get("/dashboard")
def dashboard():
    u = current_user()
    a = analytics(u, 7)
    plan = plan_for(u, 7)
    now = now_utc()
    upcoming = db.query(
        """SELECT * FROM tf_tasks WHERE user_id=? AND deleted_at IS NULL AND status!='done'
           AND due_at IS NOT NULL ORDER BY due_at LIMIT 6""", (u["id"],))
    today = local_now(u).date().isoformat()
    today_blocks = [b for b in plan["blocks"]
                    if (from_str(b["start"][:-1].replace("T", " ")) + timedelta(minutes=u["tz_offset"])).date().isoformat() == today]
    return jsonify(stats=a, risks=plan["risks"][:5], today_blocks=today_blocks,
                   upcoming=[task_dict(r) for r in upcoming])


# ---------------------------------------------------------------- AI agents

def ai_gate(u):
    since = to_str(now_utc() - timedelta(hours=1))
    used = db.one("SELECT COUNT(*) c FROM tf_ai_usage WHERE user_id=? AND created_at>?", (u["id"], since))["c"]
    if used >= AI_HOURLY_LIMIT:
        raise ApiError(f"AI usage limit reached ({AI_HOURLY_LIMIT}/hour). Try again later.", 429)
    db.run("INSERT INTO tf_ai_usage(user_id,created_at) VALUES(?,?)", (u["id"], to_str(now_utc())))


@bp.get("/ai/status")
def ai_status():
    u = current_user()
    since = to_str(now_utc() - timedelta(hours=1))
    used = db.one("SELECT COUNT(*) c FROM tf_ai_usage WHERE user_id=? AND created_at>?", (u["id"], since))["c"]
    return jsonify(enabled=ai_agent.enabled(), provider=ai_agent.provider(),
                   used_last_hour=used, limit=AI_HOURLY_LIMIT)


@bp.post("/ai/parse")
def ai_parse():
    u, d = current_user(), body()
    text = str(d.get("text") or "").strip()
    if not text or len(text) > 500:
        raise ApiError("Enter 1-500 characters")
    p = nlp.parse(text, now_utc(), u["tz_offset"])
    pid = None
    if p["project"]:
        r = db.one("SELECT id FROM tf_projects WHERE user_id=? AND lower(name)=lower(?)", (u["id"], p["project"]))
        pid = r["id"] if r else None
    return jsonify(title=p["title"], priority=p["priority"], due_at=iso(to_str(p["due_at"])),
                   estimate_min=p["estimate_min"], tags=p["tags"], recurrence=p["recurrence"],
                   project_name=p["project"], project_id=pid)


def make_proposal(u, kind, title, payload, source):
    cur = db.run("INSERT INTO tf_proposals(user_id,kind,title,payload,source,created_at) VALUES(?,?,?,?,?,?)",
                 (u["id"], kind, title[:200], json.dumps(payload), source, to_str(now_utc())))
    return cur.lastrowid


def proposal_dict(r):
    return {"id": r["id"], "kind": r["kind"], "title": r["title"], "payload": json.loads(r["payload"]),
            "source": r["source"], "status": r["status"], "created_at": iso(r["created_at"]),
            "decided_at": iso(r["decided_at"])}


@bp.post("/ai/decompose")
def ai_decompose():
    u, d = current_user(), body()
    t = get_task(u["id"], to_int(d.get("task_id"), 1, 2**31, "task_id"))
    if t["parent_id"]:
        raise ApiError("Subtasks cannot be broken down further")
    ai_gate(u)
    subs, source = ai_agent.decompose(t["title"], t["notes"], t["estimate_min"])
    pid = make_proposal(u, "decompose", f"Break down: {t['title']}", {"task_id": t["id"], "subtasks": subs}, source)
    return jsonify(proposal_dict(db.one("SELECT * FROM tf_proposals WHERE id=?", (pid,)))), 201


@bp.post("/ai/plan")
def ai_plan():
    u, d = current_user(), body()
    goal = str(d.get("goal") or "").strip()
    if len(goal) < 3 or len(goal) > 500:
        raise ApiError("Describe your goal in 3-500 characters")
    ai_gate(u)
    parsed = nlp.parse(goal, now_utc(), u["tz_offset"])
    payload, source = ai_agent.plan_goal(goal, parsed)
    pid = make_proposal(u, "plan", f"Plan: {payload['parent']['title']}", payload, source)
    return jsonify(proposal_dict(db.one("SELECT * FROM tf_proposals WHERE id=?", (pid,)))), 201


@bp.post("/ai/advise")
def ai_advise():
    """Priority advisor (deterministic): raise priority of urgent, low-priority tasks."""
    u = current_user()
    ai_gate(u)
    now = now_utc()
    rows = db.query("SELECT * FROM tf_tasks WHERE user_id=? AND deleted_at IS NULL AND status!='done' AND due_at IS NOT NULL", (u["id"],))
    pending = {json.loads(p["payload"]).get("task_id") for p in
               db.query("SELECT payload FROM tf_proposals WHERE user_id=? AND kind='priority' AND status='pending'", (u["id"],))}
    made = []
    for r in rows:
        if r["id"] in pending:
            continue
        hrs = (from_str(r["due_at"]) - now).total_seconds() / 3600
        want = None
        if hrs <= 0 and r["priority"] < 4:
            want, why = 4, "overdue"
        elif hrs <= 24 and r["priority"] < 3:
            want, why = 3, "due within 24 hours"
        elif hrs <= 72 and r["priority"] < 2:
            want, why = 2, "due within 3 days"
        if want:
            pid = make_proposal(u, "priority", f"Raise priority: {r['title']} ({why})",
                                {"task_id": r["id"], "priority": want, "reason": why}, "rules")
            made.append(proposal_dict(db.one("SELECT * FROM tf_proposals WHERE id=?", (pid,))))
    return jsonify(proposals=made)


@bp.get("/ai/review")
def ai_review():
    u = current_user()
    kind = request.args.get("kind", "daily")
    if kind not in ("daily", "weekly"):
        raise ApiError("kind must be daily or weekly")
    ai_gate(u)
    a = analytics(u, 7 if kind == "weekly" else 3)
    plan = plan_for(u, 7 if kind == "weekly" else 2)
    stats = {k: a[k] for k in ("open", "overdue", "done_week", "done_today", "focus_week_min",
                              "focus_today_min", "streak", "on_time_rate", "last_minute_rate", "estimate_accuracy")}
    stats["at_risk"] = len([r for r in plan["risks"] if r["level"] in ("overdue", "high")])
    summary, tips, source = ai_agent.narrative(kind, stats)
    if summary is None:
        tips = []
        if kind == "daily":
            summary = (f"You have {a['open']} open task(s), {a['overdue']} overdue and "
                       f"{stats['at_risk']} at risk. Focused so far today: {a['focus_today_min']} min.")
        else:
            summary = (f"This week you completed {a['done_week']} task(s) and focused {a['focus_week_min']} min. "
                       f"{a['open']} remain open, {a['overdue']} overdue.")
        if a["overdue"]:
            tips.append("Clear or renegotiate the overdue tasks first; they cost the most attention.")
        if stats["at_risk"]:
            tips.append("Open the Planner and act on the at-risk list before adding new work.")
        if a["last_minute_rate"] and a["last_minute_rate"] >= 50:
            tips.append("Many tasks finish within 6 hours of the deadline; try an earlier internal deadline.")
        if a["estimate_accuracy"] and a["estimate_accuracy"] > 1.3:
            tips.append("Your tasks take longer than estimated; add ~30% buffer to estimates.")
        if not tips:
            tips.append("Pick the single most important task and start a 25-minute focus session.")
    return jsonify(kind=kind, summary=summary, tips=tips, source=source, stats=stats,
                   risks=plan["risks"][:5])


@bp.get("/proposals")
def list_proposals():
    u = current_user()
    status = request.args.get("status", "pending")
    if status == "pending":
        rows = db.query("SELECT * FROM tf_proposals WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 50", (u["id"],))
    else:
        rows = db.query("SELECT * FROM tf_proposals WHERE user_id=? ORDER BY id DESC LIMIT 40", (u["id"],))
    return jsonify(proposals=[proposal_dict(r) for r in rows])


def apply_proposal(u, p):
    payload = json.loads(p["payload"])
    kind = p["kind"]
    if kind == "decompose":
        t = get_task(u["id"], payload["task_id"])
        if t["parent_id"]:
            raise ApiError("Target is already a subtask")
        for s in ai_agent.clean_subtasks(payload.get("subtasks")):
            insert_task(u, {"title": s["title"], "priority": s["priority"], "estimate_min": s["estimate_min"],
                            "due_at": t["due_at"], "project_id": t["project_id"], "tags": t["tags"]}, t["id"])
    elif kind == "plan":
        par = payload["parent"]
        f = {"title": str(par["title"])[:200], "priority": max(1, min(int(par.get("priority", 2)), 4)),
             "due_at": par.get("due_at"), "estimate_min": par.get("estimate_min"),
             "tags": clean_tags(par.get("tags", []))}
        parent_id = insert_task(u, f)
        for s in ai_agent.clean_subtasks(payload.get("subtasks")):
            insert_task(u, {"title": s["title"], "priority": s["priority"], "estimate_min": s["estimate_min"],
                            "due_at": f["due_at"], "tags": f["tags"]}, parent_id)
    elif kind == "priority":
        t = get_task(u["id"], payload["task_id"])
        db.run("UPDATE tf_tasks SET priority=? WHERE id=?", (max(1, min(int(payload["priority"]), 4)), t["id"]))
    else:
        raise ApiError("Unknown proposal type")


@bp.post("/proposals/<int:pid>/<action>")
def decide_proposal(pid, action):
    u = current_user()
    if action not in ("approve", "reject"):
        raise ApiError("Unknown action", 404)
    p = db.one("SELECT * FROM tf_proposals WHERE id=? AND user_id=?", (pid, u["id"]))
    if not p:
        raise ApiError("Proposal not found", 404)
    status = "approved" if action == "approve" else "rejected"
    claim = db.run("UPDATE tf_proposals SET status=?, decided_at=? WHERE id=? AND status='pending'",
                   (status, to_str(now_utc()), pid))
    if claim.rowcount != 1:
        raise ApiError("This proposal was already decided", 409)
    if action == "approve":
        apply_proposal(u, p)
    return jsonify(ok=True, status=status)


# ---------------------------------------------------------------- export / import / account

@bp.get("/export")
def export_data():
    u = current_user()
    uid = u["id"]
    def rows(sql):
        return [dict(r) for r in db.query(sql, (uid,))]
    data = {"app": "taskflow", "version": 1, "exported_at": iso(to_str(now_utc())),
            "projects": rows("SELECT id,name,color FROM tf_projects WHERE user_id=?"),
            "tasks": rows("""SELECT id,project_id,parent_id,title,notes,status,priority,due_at,estimate_min,tags,
                             recurrence,created_at,completed_at FROM tf_tasks WHERE user_id=? AND deleted_at IS NULL"""),
            "events": rows("SELECT title,start_at,end_at,notes FROM tf_events WHERE user_id=?"),
            "timelogs": rows("SELECT task_id,started_at,duration_sec,kind FROM tf_timelogs WHERE user_id=?")}
    resp = jsonify(data)
    resp.headers["Content-Disposition"] = "attachment; filename=taskflow-backup.json"
    return resp


@bp.post("/import")
def import_data():
    u, d = current_user(), body()
    if d.get("app") != "taskflow":
        raise ApiError("This is not a TaskFlow backup file")
    pmap, tmap = {}, {}
    for p in (d.get("projects") or [])[:200]:
        name = str(p.get("name") or "").strip()[:40]
        if not name:
            continue
        ex = db.one("SELECT id FROM tf_projects WHERE user_id=? AND lower(name)=lower(?)", (u["id"], name))
        if ex:
            pmap[p.get("id")] = ex["id"]
        else:
            color = p.get("color") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(p.get("color"))) else "#8b7cf6"
            pmap[p.get("id")] = db.run("INSERT INTO tf_projects(user_id,name,color,created_at) VALUES(?,?,?,?)",
                                       (u["id"], name, color, to_str(now_utc()))).lastrowid
    tasks = (d.get("tasks") or [])[:3000]
    for t in sorted(tasks, key=lambda x: (x.get("parent_id") is not None, x.get("id") or 0)):
        try:
            f = clean_task(u["id"], {k: t.get(k) for k in ("title", "notes", "status", "priority", "due_at",
                                                           "estimate_min", "tags", "recurrence") if k in t}, False)
        except ApiError:
            continue
        f["project_id"] = pmap.get(t.get("project_id"))
        parent = tmap.get(t.get("parent_id"))
        tmap[t.get("id")] = insert_task(u, f, parent)
    n_ev = 0
    for e in (d.get("events") or [])[:2000]:
        try:
            title, s, en, notes = clean_event(e)
        except ApiError:
            continue
        db.run("INSERT INTO tf_events(user_id,title,start_at,end_at,notes) VALUES(?,?,?,?,?)",
               (u["id"], title, to_str(s), to_str(en), notes))
        n_ev += 1
    for l in (d.get("timelogs") or [])[:5000]:
        try:
            secs = to_int(l.get("duration_sec"), 1, 43200, "d")
            st = parse_dt(l.get("started_at"))
        except (ApiError, ValueError):
            continue
        if st:
            db.run("INSERT INTO tf_timelogs(user_id,task_id,started_at,duration_sec,kind) VALUES(?,?,?,?,?)",
                   (u["id"], tmap.get(l.get("task_id")), to_str(st), secs,
                    l.get("kind") if l.get("kind") in ("pomodoro", "stopwatch", "countdown") else "pomodoro"))
    return jsonify(tasks=len(tmap), events=n_ev), 201


@bp.post("/account/delete")
def delete_account():
    from werkzeug.security import check_password_hash
    u, d = current_user(), body()
    if not check_password_hash(u["password_hash"], str(d.get("password") or "")):
        raise ApiError("Password is incorrect", 403)
    db.run("DELETE FROM tf_users WHERE id=?", (u["id"],))
    session.clear()
    return jsonify(ok=True)
