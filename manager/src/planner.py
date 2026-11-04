"""Persistent plans; changing a rule and invalidating its plans is one transaction."""
from datetime import date, datetime, time, timedelta
import math
import random
import re

from .storage import TZ, now, stamp

SLOTS = {"morning": "上班", "evening": "下班"}
MODES = {"default": "默认", "work": "打卡", "leave": "请假"}


def controller_configured(settings):
    return bool(settings["controller_url"] and settings["controller_api_key"])


def validate(settings):
    if not isinstance(settings["weekdays"], list) or any(type(d) is not int or not 1 <= d <= 7 for d in settings["weekdays"]):
        raise ValueError("请选择有效的每周执行日")
    if type(settings["wait_seconds"]) is not int or not 0 <= settings["wait_seconds"] <= 120:
        raise ValueError("等待时长应为 0–120 秒")
    for slot, label in SLOTS.items():
        window = settings[slot]
        try:
            start, end = (time.fromisoformat(window[k]) for k in ("start", "end"))
        except (ValueError, TypeError, KeyError):
            raise ValueError(f"请填写完整的{label}时间窗口") from None
        duration = (end.hour * 60 + end.minute - start.hour * 60 - start.minute) * 60
        if end <= start or start.second or end.second:
            raise ValueError(f"{label}结束时间应晚于开始时间，使用同一天内的时分")
        if window["enabled"] and duration <= settings["wait_seconds"] + 30:
            raise ValueError(f"{label}窗口过短，请为等待和操作预留时间")
    if settings["package"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", settings["package"]):
        raise ValueError("应用包名格式不正确")


def workday(settings, overrides, day):
    mode = overrides.get(day.isoformat(), "default")
    return mode == "work" or (mode == "default" and day.isoweekday() in settings["weekdays"])


class Planner:
    def __init__(self, store, clock=now, rng=None):
        self.store, self.clock = store, clock
        self.rng = rng or random.SystemRandom()

    def _new(self, db, day, slot, settings, at):
        window = settings[slot]
        start = datetime.combine(day, time.fromisoformat(window["start"]), TZ)
        latest = datetime.combine(day, time.fromisoformat(window["end"]), TZ) - timedelta(seconds=settings["wait_seconds"] + 30)
        earliest = max(start, datetime.fromtimestamp(math.floor(at.timestamp()) + 1, TZ))
        missed = earliest > latest
        scheduled = latest if missed else earliest + timedelta(seconds=self.rng.randrange(int((latest-earliest).total_seconds()) + 1))
        cursor = db.execute("INSERT INTO plans(day,slot,scheduled_at,status,created_at,reason) VALUES (?,?,?,?,?,?)", (
            day.isoformat(), slot, stamp(scheduled), "missed" if missed else "pending", stamp(at), "已错过可用时间" if missed else ""))
        if missed:
            plan = dict(db.execute("SELECT * FROM plans WHERE id=?", (cursor.lastrowid,)).fetchone())
            self.store.finish_plan(db, plan, "missed", "已错过可用时间", at)

    def _cancel(self, db, plan, reason, at):
        run_id = self.store.finish_plan(db, plan, "cancelled", reason, at)
        db.execute("UPDATE runs SET bark_status='not_needed' WHERE id=?", (run_id,))

    def _ensure(self, db, start, end, at, changed_days=None, changed_slots=None):
        settings = self.store.settings(db)
        overrides = dict(db.execute("SELECT day,mode FROM day_overrides").fetchall())
        changed_days, changed_slots = changed_days or set(), changed_slots or set()
        day = max(start, at.date())
        while day <= end:
            for slot in SLOTS:
                row = db.execute("SELECT * FROM plans WHERE day=? AND slot=? ORDER BY id DESC LIMIT 1", (day.isoformat(), slot)).fetchone()
                old = dict(row) if row else None
                enabled = workday(settings, overrides, day) and settings[slot]["enabled"]
                if not enabled:
                    if old and old["status"] in {"pending", "queued"}:
                        self._cancel(db, old, "请假或按当前规则跳过", at)
                    continue
                force = slot in changed_slots
                if force and old and old["status"] in {"pending", "queued"}:
                    self._cancel(db, old, "时间窗口已修改", at)
                if old is None or force or (old["status"] == "cancelled" and day.isoformat() in changed_days):
                    self._new(db, day, slot, settings, at)
            day += timedelta(days=1)

    def ensure(self, start=None, end=None):
        at = self.clock()
        start, end = start or at.date(), end or at.date() + timedelta(days=30)
        with self.store.connect(write=True) as db:
            self._ensure(db, start, end, at)

    def update_settings(self, values):
        at = self.clock()
        with self.store.connect(write=True) as db:
            old = self.store.settings(db)
            new = {**old, **values}
            validate(new)
            changed_slots = {slot for slot in SLOTS if old[slot] != new[slot] or old["wait_seconds"] != new["wait_seconds"]}
            for key, value in values.items():
                self.store.put(db, key, value)
            days = {row[0] for row in db.execute("SELECT DISTINCT day FROM plans WHERE day>=?", (at.date().isoformat(),))}
            end = max([at.date() + timedelta(days=30), *(date.fromisoformat(d) for d in days)])
            self._ensure(db, at.date(), end, at, days if old["weekdays"] != new["weekdays"] else set(), changed_slots)

    def override(self, start, end, mode):
        at = self.clock()
        if mode not in MODES or start < at.date() or end < start or (end-start).days > 366:
            raise ValueError("请选择今天起、长度不超过一年的有效日期范围")
        with self.store.connect(write=True) as db:
            day, changed = start, set()
            while day <= end:
                previous = db.execute("SELECT mode FROM day_overrides WHERE day=?", (day.isoformat(),)).fetchone()
                if (previous[0] if previous else "default") != mode:
                    changed.add(day.isoformat())
                if mode == "default":
                    db.execute("DELETE FROM day_overrides WHERE day=?", (day.isoformat(),))
                else:
                    db.execute("INSERT INTO day_overrides VALUES (?,?) ON CONFLICT(day) DO UPDATE SET mode=excluded.mode", (day.isoformat(), mode))
                day += timedelta(days=1)
            self._ensure(db, start, end, at, changed)

    def plans(self, start, end):
        self.ensure(start, end)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM plans WHERE day BETWEEN ? AND ? ORDER BY id", (start.isoformat(), end.isoformat())).fetchall()
            overrides = dict(db.execute("SELECT day,mode FROM day_overrides WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat())).fetchall())
        active = {}
        for row in rows:
            active[(row["day"], row["slot"])] = dict(row)
        return active, overrides

    def next_plan(self):
        settings = self.store.settings()
        if settings["paused"] or not controller_configured(settings):
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM plans WHERE status='pending' AND scheduled_at>? ORDER BY scheduled_at LIMIT 1", (stamp(self.clock()),)).fetchone()
            return dict(row) if row else None

    def recover(self):
        at = self.clock()
        with self.store.connect(write=True) as db:
            # Dispatched requests are recovered by Service using their original ID.
            for row in db.execute("SELECT * FROM plans WHERE status='pending' AND scheduled_at<=?", (stamp(at),)).fetchall():
                self.store.finish_plan(db, dict(row), "missed", "服务恢复时已错过计划时刻", at)
            db.execute("UPDATE runs SET bark_status='failed',bark_error='发送中断，可单独重发' WHERE bark_status='sending'")
