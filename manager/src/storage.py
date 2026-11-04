"""Local SQLite storage. Connections are never shared between threads."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sqlite3

from werkzeug.security import generate_password_hash

TZ = timezone(timedelta(hours=8))
DEFAULTS = {
    "weekdays": [1, 2, 3, 4, 5],
    "morning": {"enabled": True, "start": "08:30", "end": "09:00"},
    "evening": {"enabled": True, "start": "17:30", "end": "18:30"},
    "paused": False, "package": "", "wait_seconds": 10,
    "controller_url": "", "controller_api_key": "", "serial": "", "bark_server": "https://api.day.app",
    "bark_key": "", "public_url": "",
}


def now():
    return datetime.now(TZ)


def stamp(value=None):
    return (value or now()).astimezone(TZ).isoformat(timespec="microseconds")


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.screenshots = self.directory / "screenshots"
        self.screenshots.mkdir(exist_ok=True)
        self.path = self.directory / "checkin.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS day_overrides (day TEXT PRIMARY KEY, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, slot TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_plan ON plans(day,slot)
                    WHERE status IN ('pending','queued');
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, plan_id INTEGER UNIQUE REFERENCES plans(id),
                    request_id TEXT UNIQUE, kind TEXT NOT NULL, trigger TEXT NOT NULL,
                    day TEXT NOT NULL, slot TEXT, scheduled_at TEXT, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    reason TEXT NOT NULL DEFAULT '', steps TEXT NOT NULL DEFAULT '[]',
                    screenshot TEXT, screenshot_at TEXT, device TEXT NOT NULL DEFAULT '{}',
                    bark_status TEXT NOT NULL DEFAULT 'pending', bark_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS runs_date ON runs(day,created_at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            for column in ("dispatch", "remote_result"):
                if column not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")
            db.execute("DELETE FROM settings WHERE key IN ('adb_path','activity')")
            for key, value in DEFAULTS.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
            for key, value in {"_secret": secrets.token_hex(32), "_auth_version": secrets.token_hex(16)}.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
            if not db.execute("SELECT 1 FROM settings WHERE key='_password' ").fetchone():
                self.put(db, "_password", generate_password_hash("tianhe"))

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))

    def get(self, key, default=None, db=None):
        if db is None:
            with self.connect() as connection:
                return self.get(key, default, connection)
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def settings(self, db=None):
        if db is None:
            with self.connect() as connection:
                return self.settings(connection)
        return {key: self.get(key, value, db) for key, value in DEFAULTS.items()}

    def update(self, values):
        with self.connect(write=True) as db:
            for key, value in values.items():
                self.put(db, key, value)

    def run(self, run_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.run(run_id, connection)
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self.decode_run(row) if row else None

    @staticmethod
    def decode_run(row):
        result = dict(row)
        result.pop("dispatch", None)  # The snapshot contains a server-side API key.
        for key in ("steps", "device", "remote_result"):
            result[key] = json.loads(result[key])
        return result

    def dispatch(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT dispatch FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def change_run(self, run_id, **values):
        allowed = {"status", "reason", "steps", "started_at", "finished_at", "screenshot", "screenshot_at", "device", "bark_status", "bark_error", "dispatch", "remote_result"}
        if not values or set(values) - allowed:
            raise ValueError("Invalid run fields")
        values = {k: json.dumps(v, ensure_ascii=False) if k in {"steps", "device", "dispatch", "remote_result"} else v for k, v in values.items()}
        with self.connect(write=True) as db:
            db.execute("UPDATE runs SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", [*values.values(), run_id])
            if "status" in values:
                db.execute("UPDATE plans SET status=?,reason=? WHERE id=(SELECT plan_id FROM runs WHERE id=?)",
                           (values["status"], values.get("reason", ""), run_id))

    def add_run(self, db, *, kind, trigger="manual", plan=None, request_id=None, status="queued", reason="", at=None):
        at = at or now()
        run_id = secrets.token_hex(12)
        dispatch = {}
        if status == "queued":
            settings = self.settings(db)
            params = {"serial": settings["serial"]}
            if kind == "full":
                params["delay"] = settings["wait_seconds"]
                if settings["package"]:
                    params["package"] = settings["package"]
            dispatch = {"url": settings["controller_url"], "api_key": settings["controller_api_key"],
                        "params": params, "sent": False}
        db.execute("""INSERT INTO runs(id,plan_id,request_id,kind,trigger,day,slot,scheduled_at,status,created_at,reason,dispatch)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (run_id, plan["id"] if plan else None, request_id or secrets.token_hex(16), kind, trigger,
                   plan["day"] if plan else at.astimezone(TZ).date().isoformat(), plan["slot"] if plan else None,
                   plan["scheduled_at"] if plan else None, status, stamp(at), reason, json.dumps(dispatch)))
        return run_id

    def finish_plan(self, db, plan, status, reason, at=None):
        db.execute("UPDATE plans SET status=?,reason=? WHERE id=?", (status, reason, plan["id"]))
        run = db.execute("SELECT id,status FROM runs WHERE plan_id=?", (plan["id"],)).fetchone()
        if run:
            if run["status"] in {"queued", "running"}:
                db.execute("UPDATE runs SET status=?,reason=?,finished_at=? WHERE id=?", (status, reason, stamp(at), run["id"]))
            return run["id"]
        return self.add_run(db, kind="full", trigger="automatic", plan=plan, status=status, reason=reason, at=at)
