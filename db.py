"""SQLite access layer (standard library only)."""
import os
import sqlite3
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS tf_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    tz_offset INTEGER NOT NULL DEFAULT 0,
    work_start INTEGER NOT NULL DEFAULT 540,
    work_end INTEGER NOT NULL DEFAULT 1080,
    quiet_start INTEGER NOT NULL DEFAULT 1320,
    quiet_end INTEGER NOT NULL DEFAULT 420,
    remind_lead INTEGER NOT NULL DEFAULT 30,
    pomo_work INTEGER NOT NULL DEFAULT 25,
    pomo_short INTEGER NOT NULL DEFAULT 5,
    pomo_long INTEGER NOT NULL DEFAULT 15,
    pomo_cycles INTEGER NOT NULL DEFAULT 4
);
CREATE TABLE IF NOT EXISTS tf_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES tf_users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#8b7cf6',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tf_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES tf_users(id) ON DELETE CASCADE,
    project_id INTEGER REFERENCES tf_projects(id) ON DELETE SET NULL,
    parent_id INTEGER REFERENCES tf_tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    priority INTEGER NOT NULL DEFAULT 2,
    due_at TEXT,
    estimate_min INTEGER,
    tags TEXT NOT NULL DEFAULT '',
    recurrence TEXT NOT NULL DEFAULT 'none',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    deleted_at TEXT,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tasks_user ON tf_tasks(user_id, deleted_at);
CREATE TABLE IF NOT EXISTS tf_spawns (
    task_id INTEGER PRIMARY KEY,
    next_task_id INTEGER
);
CREATE TABLE IF NOT EXISTS tf_timelogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES tf_users(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tf_tasks(id) ON DELETE SET NULL,
    client_id TEXT,
    started_at TEXT NOT NULL,
    duration_sec INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'pomodoro',
    UNIQUE(user_id, client_id)
);
CREATE TABLE IF NOT EXISTS tf_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES tf_users(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tf_tasks(id) ON DELETE CASCADE,
    remind_at TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    delivered_at TEXT,
    UNIQUE(task_id, remind_at)
);
CREATE TABLE IF NOT EXISTS tf_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES tf_users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tf_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES tf_users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'rules',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS tf_ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(path):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def query(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def one(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def run(sql, args=()):
    cur = get_db().execute(sql, args)
    return cur
