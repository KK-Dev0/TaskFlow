"""Backend tests. Run with:  python -m unittest discover -s tests -v   (or: pytest)"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
import nlp  # noqa: E402
import planner  # noqa: E402
from util import in_quiet_hours, next_occurrence  # noqa: E402


class Client:
    """Test helper that signs in and sends the CSRF header automatically."""

    def __init__(self, app, email="a@example.com", name="Ada"):
        self.c = app.test_client()
        self.c.get("/register")
        with self.c.session_transaction() as s:
            token = s["csrf"]
        r = self.c.post("/register", data={"name": name, "email": email, "password": "password123",
                                           "csrf_token": token})
        assert r.status_code == 302, r.data
        with self.c.session_transaction() as s:
            self.csrf = s["csrf"]

    def req(self, method, url, json=None):
        return getattr(self.c, method)(url, json=json, headers={"X-CSRF-Token": self.csrf})

    def get(self, url): return self.req("get", url)
    def post(self, url, json=None): return self.req("post", url, json or {})
    def patch(self, url, json=None): return self.req("patch", url, json or {})
    def delete(self, url): return self.req("delete", url)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test",
                               "DB_PATH": os.path.join(self.tmp.name, "t.db"),
                               "RATELIMIT_ENABLED": False})
        self.u = Client(self.app)

    def tearDown(self):
        self.tmp.cleanup()


class AuthTests(Base):
    def test_requires_login(self):
        r = self.app.test_client().get("/api/tasks")
        self.assertEqual(r.status_code, 401)

    def test_csrf_required_for_writes(self):
        r = self.u.c.post("/api/tasks", json={"title": "x"})
        self.assertEqual(r.status_code, 403)

    def test_login_and_wrong_password(self):
        c = self.app.test_client()
        c.get("/login")
        with c.session_transaction() as s:
            token = s["csrf"]
        bad = c.post("/login", data={"email": "a@example.com", "password": "nope", "csrf_token": token})
        self.assertEqual(bad.status_code, 400)
        ok = c.post("/login", data={"email": "a@example.com", "password": "password123", "csrf_token": token})
        self.assertEqual(ok.status_code, 302)

    def test_health(self):
        self.assertEqual(self.app.test_client().get("/health").get_json(), {"status": "ok"})

    def test_password_is_hashed(self):
        import sqlite3
        conn = sqlite3.connect(self.app.config["DB_PATH"])
        h = conn.execute("SELECT password_hash FROM tf_users").fetchone()[0]
        self.assertNotIn("password123", h)

    def test_login_rate_limit(self):
        self.app.config["RATELIMIT_ENABLED"] = True
        import auth
        auth._hits.clear()
        c = self.app.test_client()
        c.get("/login")
        with c.session_transaction() as s:
            token = s["csrf"]
        codes = [c.post("/login", data={"email": "a@example.com", "password": "bad", "csrf_token": token}).status_code
                 for _ in range(10)]
        self.assertEqual(codes[0], 400)
        r = c.post("/login", data={"email": "a@example.com", "password": "password123", "csrf_token": token})
        self.assertEqual(r.status_code, 400)  # locked out even with the right password
        auth._hits.clear()

    def test_duplicate_email_rejected(self):
        c = self.app.test_client()
        c.get("/register")
        with c.session_transaction() as s:
            t = s["csrf"]
        r = c.post("/register", data={"name": "B", "email": "A@example.com", "password": "password123", "csrf_token": t})
        self.assertEqual(r.status_code, 400)


class IsolationTests(Base):
    def test_users_cannot_see_or_modify_each_others_data(self):
        t = self.u.post("/api/tasks", {"title": "private"}).get_json()
        other = Client(self.app, "b@example.com", "Bob")
        self.assertEqual(other.get("/api/tasks").get_json()["count"], 0)
        self.assertEqual(other.patch(f"/api/tasks/{t['id']}", {"title": "hacked"}).status_code, 404)
        self.assertEqual(other.delete(f"/api/tasks/{t['id']}").status_code, 404)
        self.assertEqual(other.post("/api/ai/decompose", {"task_id": t["id"]}).status_code, 404)
        self.assertEqual(other.post("/api/tasks", {"title": "x", "project_id": 999}).status_code, 404)
        self.assertEqual(self.u.get(f"/api/tasks/{t['id']}").get_json()["title"], "private")


class TaskTests(Base):
    def test_crud_validation_soft_delete_restore(self):
        self.assertEqual(self.u.post("/api/tasks", {"title": "  "}).status_code, 400)
        self.assertEqual(self.u.post("/api/tasks", {"title": "x" * 201}).status_code, 400)
        self.assertEqual(self.u.post("/api/tasks", {"title": "ok", "priority": 9}).status_code, 400)
        t = self.u.post("/api/tasks", {"title": "Write report", "priority": 3, "tags": "#Work, school"}).get_json()
        self.assertEqual(t["tags"], ["work", "school"])
        r = self.u.patch(f"/api/tasks/{t['id']}", {"status": "done"}).get_json()
        self.assertEqual(r["status"], "done")
        self.assertIsNotNone(r["completed_at"])
        self.u.delete(f"/api/tasks/{t['id']}")
        self.assertEqual(self.u.get("/api/tasks").get_json()["count"], 0)
        self.assertEqual(self.u.get("/api/tasks?trash=1").get_json()["count"], 1)
        self.u.post(f"/api/tasks/{t['id']}/restore")
        self.assertEqual(self.u.get("/api/tasks").get_json()["count"], 1)

    def test_filters_search_and_subtasks(self):
        p = self.u.post("/api/tasks", {"title": "Build app", "priority": 4}).get_json()
        self.u.post("/api/tasks", {"title": "Design logo", "parent_id": p["id"]})
        self.u.post("/api/tasks", {"title": "Buy milk", "priority": 1})
        tasks = self.u.get("/api/tasks").get_json()["tasks"]
        self.assertEqual(len(tasks), 2)
        parent = [t for t in tasks if t["title"] == "Build app"][0]
        self.assertEqual(len(parent["subtasks"]), 1)
        self.assertEqual(self.u.get("/api/tasks?q=logo").get_json()["count"], 1)  # parent returned via subtask match
        self.assertEqual(self.u.get("/api/tasks?priority=1").get_json()["count"], 1)
        nested = self.u.post("/api/tasks", {"title": "deep", "parent_id": parent["subtasks"][0]["id"]})
        self.assertEqual(nested.status_code, 400)

    def test_bulk_and_clear_completed(self):
        ids = [self.u.post("/api/tasks", {"title": f"t{i}"}).get_json()["id"] for i in range(3)]
        self.assertEqual(self.u.post("/api/tasks/bulk", {"ids": ids, "action": "done"}).get_json()["updated"], 3)
        self.assertEqual(self.u.post("/api/tasks/clear-completed").get_json()["cleared"], 3)

    def test_recurring_task_spawns_once(self):
        due = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t = self.u.post("/api/tasks", {"title": "Gym", "due_at": due, "recurrence": "daily"}).get_json()
        self.u.patch(f"/api/tasks/{t['id']}", {"status": "done"})
        self.u.patch(f"/api/tasks/{t['id']}", {"status": "todo"})
        self.u.patch(f"/api/tasks/{t['id']}", {"status": "done"})  # completing twice must not duplicate
        open_ = self.u.get("/api/tasks?status=open").get_json()["tasks"]
        self.assertEqual(len(open_), 1)
        self.assertEqual(open_[0]["title"], "Gym")
        self.assertNotEqual(open_[0]["due_at"], t["due_at"])

    def test_projects(self):
        p = self.u.post("/api/projects", {"name": "College", "color": "#112233"}).get_json()
        self.assertEqual(self.u.post("/api/projects", {"name": "college"}).status_code, 409)
        t = self.u.post("/api/tasks", {"title": "x", "project_id": p["id"]}).get_json()
        self.assertEqual(t["project_id"], p["id"])
        self.u.delete(f"/api/projects/{p['id']}")
        self.assertIsNone(self.u.get(f"/api/tasks/{t['id']}").get_json()["project_id"])


class ReminderTests(Base):
    def test_reminder_fires_once_and_respects_ack(self):
        due = (datetime.utcnow() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t = self.u.post("/api/tasks", {"title": "Call", "due_at": due}).get_json()
        self.u.patch("/api/me", {"quiet_start": 0, "quiet_end": 0})
        # default lead is 30 min and due is in 10 min => already due
        due_list = self.u.get("/api/reminders/due").get_json()["reminders"]
        self.assertEqual(len(due_list), 1)
        self.assertEqual(due_list[0]["task_id"], t["id"])
        self.u.post(f"/api/reminders/{due_list[0]['id']}/ack")
        self.u.post(f"/api/reminders/{due_list[0]['id']}/ack")  # idempotent
        self.assertEqual(self.u.get("/api/reminders/due").get_json()["reminders"], [])

    def test_editing_due_does_not_duplicate_reminders(self):
        due = (datetime.utcnow() + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t = self.u.post("/api/tasks", {"title": "x", "due_at": due}).get_json()
        for _ in range(3):
            self.u.patch(f"/api/tasks/{t['id']}", {"due_at": due})
        import sqlite3
        n = sqlite3.connect(self.app.config["DB_PATH"]).execute("SELECT COUNT(*) FROM tf_reminders").fetchone()[0]
        self.assertEqual(n, 1)

    def test_snooze_and_done_task_suppresses(self):
        due = (datetime.utcnow() + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t = self.u.post("/api/tasks", {"title": "x", "due_at": due}).get_json()
        self.u.patch("/api/me", {"quiet_start": 0, "quiet_end": 0})
        rid = self.u.get("/api/reminders/due").get_json()["reminders"][0]["id"]
        self.u.post(f"/api/reminders/{rid}/snooze", {"minutes": 30})
        self.assertEqual(self.u.get("/api/reminders/due").get_json()["reminders"], [])
        self.u.patch(f"/api/tasks/{t['id']}", {"status": "done"})
        self.assertEqual(self.u.get("/api/reminders/due").get_json()["reminders"], [])

    def test_quiet_hours(self):
        self.assertTrue(in_quiet_hours(datetime(2026, 1, 1, 23, 0), 0, 1320, 420))
        self.assertTrue(in_quiet_hours(datetime(2026, 1, 1, 3, 0), 0, 1320, 420))
        self.assertFalse(in_quiet_hours(datetime(2026, 1, 1, 12, 0), 0, 1320, 420))
        self.assertTrue(in_quiet_hours(datetime(2026, 1, 1, 18, 0), 330, 1320, 420))  # 23:30 local


class EventTimerTests(Base):
    def test_events_and_validation(self):
        bad = self.u.post("/api/events", {"title": "x", "start_at": "2026-10-12T10:00:00Z", "end_at": "2026-10-12T09:00:00Z"})
        self.assertEqual(bad.status_code, 400)
        ok = self.u.post("/api/events", {"title": "Class", "start_at": "2026-10-12T10:00:00Z", "end_at": "2026-10-12T11:00:00Z"})
        self.assertEqual(ok.status_code, 201)
        evs = self.u.get("/api/events?from=2026-10-01T00:00:00Z&to=2026-10-31T00:00:00Z").get_json()["events"]
        self.assertEqual(len(evs), 1)

    def test_timelog_idempotent_and_analytics(self):
        t = self.u.post("/api/tasks", {"title": "Study", "estimate_min": 60}).get_json()
        body = {"task_id": t["id"], "duration_sec": 1500, "kind": "pomodoro", "client_id": "abc"}
        self.assertFalse(self.u.post("/api/timelogs", body).get_json()["duplicate"])
        self.assertTrue(self.u.post("/api/timelogs", body).get_json()["duplicate"])
        self.assertEqual(self.u.post("/api/timelogs", {"duration_sec": 0}).status_code, 400)
        self.u.patch(f"/api/tasks/{t['id']}", {"status": "done"})
        a = self.u.get("/api/analytics").get_json()
        self.assertEqual(a["done_total"], 1)
        self.assertEqual(a["focus_today_min"], 25)
        self.assertEqual(a["streak"], 1)
        self.assertEqual(a["estimate_accuracy"], round(25 / 60, 2))


class PlannerTests(unittest.TestCase):
    now = datetime(2026, 10, 12, 8, 0)

    def plan(self, tasks, events=(), days=3):
        return planner.build_plan(tasks, list(events), self.now, days, 0, 540, 1080)

    def test_schedules_within_working_hours_without_overlap(self):
        tasks = [{"id": 1, "title": "A", "priority": 2, "due_at": datetime(2026, 10, 14, 17, 0), "estimate_min": 120, "logged_min": 0},
                 {"id": 2, "title": "B", "priority": 4, "due_at": datetime(2026, 10, 13, 17, 0), "estimate_min": 90, "logged_min": 0}]
        res = self.plan(tasks)
        self.assertEqual(sum(b["minutes"] for b in res["blocks"] if b["task_id"] == 1), 120)
        self.assertEqual(sum(b["minutes"] for b in res["blocks"] if b["task_id"] == 2), 90)
        spans = sorted((b["start"], b["end"]) for b in res["blocks"])
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            self.assertLessEqual(e1, s2)
        for b in res["blocks"]:
            self.assertGreaterEqual(b["start"][11:16], "09:00")
            self.assertLessEqual(b["end"][11:16], "18:00")
        self.assertEqual(res["blocks"][0]["task_id"], 2)  # earliest deadline first

    def test_respects_calendar_events(self):
        ev = [{"title": "Class", "start_at": datetime(2026, 10, 12, 9, 0), "end_at": datetime(2026, 10, 12, 17, 0)}]
        tasks = [{"id": 1, "title": "A", "priority": 2, "due_at": None, "estimate_min": 30, "logged_min": 0}]
        res = self.plan(tasks, ev, days=1)
        self.assertEqual(len(res["blocks"]), 1)
        self.assertGreaterEqual(res["blocks"][0]["start"][11:16], "17:00")

    def test_flags_deadline_risk_with_reason(self):
        tasks = [{"id": 1, "title": "Huge", "priority": 3, "due_at": datetime(2026, 10, 12, 12, 0), "estimate_min": 600, "logged_min": 0}]
        res = self.plan(tasks)
        self.assertEqual(res["risks"][0]["level"], "high")
        self.assertIn("suggestion", res["risks"][0])
        self.assertGreater(res["risks"][0]["short_minutes"], 0)

    def test_overdue_is_flagged_and_still_scheduled(self):
        tasks = [{"id": 1, "title": "Late", "priority": 2, "due_at": datetime(2026, 10, 10, 12, 0), "estimate_min": 30, "logged_min": 0}]
        res = self.plan(tasks)
        self.assertEqual(res["risks"][0]["level"], "overdue")
        self.assertEqual(len(res["blocks"]), 1)

    def test_logged_time_reduces_remaining_work(self):
        tasks = [{"id": 1, "title": "A", "priority": 2, "due_at": None, "estimate_min": 60, "logged_min": 40}]
        res = self.plan(tasks)
        self.assertEqual(sum(b["minutes"] for b in res["blocks"]), 20)

    def test_event_conflicts_detected(self):
        ev = [{"title": "A", "start_at": datetime(2026, 10, 12, 10, 0), "end_at": datetime(2026, 10, 12, 11, 0)},
              {"title": "B", "start_at": datetime(2026, 10, 12, 10, 30), "end_at": datetime(2026, 10, 12, 11, 30)}]
        self.assertEqual(len(self.plan([], ev)["conflicts"]), 1)

    def test_timezone_offset(self):
        # 03:30 UTC = 09:00 IST, so the first block should start at 03:30Z for a +330 user
        now = datetime(2026, 10, 12, 0, 0)
        tasks = [{"id": 1, "title": "A", "priority": 2, "due_at": None, "estimate_min": 30, "logged_min": 0}]
        res = planner.build_plan(tasks, [], now, 1, 330, 540, 1080)
        self.assertTrue(res["blocks"][0]["start"].startswith("2026-10-12T03:30"))


class NlpTests(unittest.TestCase):
    now = datetime(2026, 10, 12, 8, 0)  # Monday 08:00 UTC

    def test_full_sentence(self):
        r = nlp.parse("finish report tomorrow 5pm #college @uni urgent for 2h", self.now, 0)
        self.assertEqual(r["title"], "finish report")
        self.assertEqual(r["priority"], 4)
        self.assertEqual(r["due_at"], datetime(2026, 10, 13, 17, 0))
        self.assertEqual(r["tags"], ["college"])
        self.assertEqual(r["project"], "uni")
        self.assertEqual(r["estimate_min"], 120)

    def test_weekday_recurrence_relative(self):
        r = nlp.parse("pay rent on friday every month", self.now, 0)
        self.assertEqual(r["recurrence"], "monthly")
        self.assertEqual(r["due_at"].weekday(), 4)
        r = nlp.parse("stretch in 2 hours", self.now, 0)
        self.assertEqual(r["due_at"], datetime(2026, 10, 12, 10, 0))
        self.assertEqual(r["title"], "stretch")

    def test_timezone_and_month_dates(self):
        r = nlp.parse("submit form on Oct 20 at 9am", self.now, 330)
        self.assertEqual(r["due_at"], datetime(2026, 10, 20, 3, 30))  # 09:00 IST
        r = nlp.parse("call mom", self.now, 0)
        self.assertIsNone(r["due_at"])
        self.assertEqual(r["title"], "call mom")

    def test_next_occurrence(self):
        self.assertEqual(next_occurrence(datetime(2026, 1, 31), "monthly"), datetime(2026, 2, 28))
        self.assertEqual(next_occurrence(datetime(2026, 10, 16), "weekdays"), datetime(2026, 10, 19))  # Fri -> Mon


class AiTests(Base):
    def setUp(self):
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_PROVIDER"):
            os.environ.pop(k, None)
        super().setUp()

    def test_decompose_requires_approval_and_is_idempotent(self):
        t = self.u.post("/api/tasks", {"title": "Write essay on climate", "estimate_min": 200}).get_json()
        p = self.u.post("/api/ai/decompose", {"task_id": t["id"]}).get_json()
        self.assertEqual(p["source"], "rules")
        self.assertGreaterEqual(len(p["payload"]["subtasks"]), 3)
        before = self.u.get(f"/api/tasks/{t['id']}").get_json()
        self.assertEqual(before["subtasks"], [])  # nothing changes until approval
        self.assertEqual(self.u.post(f"/api/proposals/{p['id']}/approve").status_code, 200)
        self.assertEqual(self.u.post(f"/api/proposals/{p['id']}/approve").status_code, 409)  # no duplicates
        after = self.u.get(f"/api/tasks/{t['id']}").get_json()
        self.assertEqual(len(after["subtasks"]), len(p["payload"]["subtasks"]))

    def test_reject_and_other_users_cannot_decide(self):
        p = self.u.post("/api/ai/plan", {"goal": "finish project by friday"}).get_json()
        other = Client(self.app, "c@example.com", "Cy")
        self.assertEqual(other.post(f"/api/proposals/{p['id']}/approve").status_code, 404)
        self.assertEqual(self.u.post(f"/api/proposals/{p['id']}/reject").status_code, 200)
        self.assertEqual(self.u.get("/api/tasks").get_json()["count"], 0)

    def test_plan_creates_parent_and_subtasks_on_approval(self):
        p = self.u.post("/api/ai/plan", {"goal": "build a website for the club by friday"}).get_json()
        self.u.post(f"/api/proposals/{p['id']}/approve")
        tasks = self.u.get("/api/tasks").get_json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertTrue(len(tasks[0]["subtasks"]) >= 3)
        self.assertIsNotNone(tasks[0]["due_at"])

    def test_untrusted_llm_output_is_sanitised(self):
        import ai_agent
        cleaned = ai_agent.clean_subtasks([{"title": "ok", "estimate_min": 99999, "priority": 99},
                                           {"title": ""}, "junk", {"title": "x" * 500}] + [{"title": "n"}] * 20)
        self.assertEqual(len(cleaned), ai_agent.MAX_SUBTASKS - 0 if len(cleaned) == ai_agent.MAX_SUBTASKS else len(cleaned))
        self.assertLessEqual(cleaned[0]["estimate_min"], 240)
        self.assertLessEqual(cleaned[0]["priority"], 4)
        self.assertTrue(all(len(c["title"]) <= 120 for c in cleaned))

    def test_priority_advisor_and_review(self):
        soon = (datetime.utcnow() + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t = self.u.post("/api/tasks", {"title": "Soon", "priority": 1, "due_at": soon}).get_json()
        made = self.u.post("/api/ai/advise").get_json()["proposals"]
        self.assertEqual(len(made), 1)
        self.u.post(f"/api/proposals/{made[0]['id']}/approve")
        self.assertEqual(self.u.get(f"/api/tasks/{t['id']}").get_json()["priority"], 3)
        rv = self.u.get("/api/ai/review?kind=daily").get_json()
        self.assertTrue(rv["summary"])
        self.assertEqual(rv["source"], "rules")

    def test_ai_rate_limit(self):
        import api
        old = api.AI_HOURLY_LIMIT
        api.AI_HOURLY_LIMIT = 2
        try:
            codes = [self.u.post("/api/ai/plan", {"goal": "do something big"}).status_code for _ in range(3)]
        finally:
            api.AI_HOURLY_LIMIT = old
        self.assertEqual(codes, [201, 201, 429])


class BackupTests(Base):
    def test_export_import_roundtrip_and_delete_account(self):
        p = self.u.post("/api/projects", {"name": "P"}).get_json()
        parent = self.u.post("/api/tasks", {"title": "Parent", "project_id": p["id"]}).get_json()
        self.u.post("/api/tasks", {"title": "Child", "parent_id": parent["id"]})
        data = self.u.get("/api/export").get_json()
        fresh = Client(self.app, "n@example.com", "New")
        self.assertEqual(fresh.post("/api/import", data).status_code, 201)
        tasks = fresh.get("/api/tasks").get_json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(tasks[0]["subtasks"]), 1)
        self.assertEqual(fresh.post("/api/import", {"app": "other"}).status_code, 400)
        self.assertEqual(fresh.post("/api/account/delete", {"password": "wrong"}).status_code, 403)
        self.assertEqual(fresh.post("/api/account/delete", {"password": "password123"}).status_code, 200)
        self.assertEqual(fresh.get("/api/tasks").status_code, 401)


if __name__ == "__main__":
    unittest.main()
