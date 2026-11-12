"""Persistent plans; changing a rule and invalidating its plans is one transaction."""
from datetime import date, datetime, time, timedelta
import json
import threading
import math
import random
import re

from .storage import TZ, now, stamp

SLOTS = {"morning": "上班", "evening": "下班"}
MODES = {"default": "默认", "work": "打卡", "leave": "请假"}


def controller_configured(settings):
    return bool(settings["controller_url"] and settings["controller_api_key"])


def weekly_schedule(settings, day):
    custom = settings.get("weekday_overrides", {}).get(str(day.isoweekday()), {})
    return {slot: {**settings[slot], **custom.get(slot, {})} for slot in SLOTS}


def load_overrides(db):
    return {row["day"]: {"mode": row["mode"], "schedule": json.loads(row["schedule"])}
            for row in db.execute("SELECT day,mode,schedule FROM day_overrides")}


def effective_schedule(settings, overrides, day):
    entry = overrides.get(day.isoformat(), {})
    mode = entry.get("mode", "default")
    schedule = weekly_schedule(settings, day)
    if mode == "work":
        schedule = {slot: dict(entry["schedule"][slot]) for slot in SLOTS}
    elif mode == "leave" or day.isoweekday() not in settings["weekdays"]:
        schedule = {slot: {**window, "enabled": False} for slot, window in schedule.items()}
    return schedule


def validate_schedule(schedule, wait, label=""):
    for slot, name in SLOTS.items():
        window = schedule.get(slot)
        if not isinstance(window, dict) or type(window.get("enabled")) is not bool:
            raise ValueError(f"{label}{name}场次配置不正确")
        try:
            values = [window[k] for k in ("start", "end")]
            if any(not isinstance(v, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", v) for v in values):
                raise ValueError()
            start, end = map(time.fromisoformat, values)
        except (ValueError, TypeError, KeyError):
            raise ValueError(f"请填写完整的{label}{name}时间窗口（时:分）") from None
        if end <= start:
            raise ValueError(f"{label}{name}结束时间应晚于开始时间")
        duration = (end.hour * 60 + end.minute - start.hour * 60 - start.minute) * 60
        if window["enabled"] and duration <= wait + 30:
            raise ValueError(f"{label}{name}窗口过短，请为等待和操作预留时间")


def validate(settings):
    if not isinstance(settings["weekdays"], list) or any(type(d) is not int or not 1 <= d <= 7 for d in settings["weekdays"]):
        raise ValueError("请选择有效的每周执行日")
    if type(settings["wait_seconds"]) is not int or not 0 <= settings["wait_seconds"] <= 120:
        raise ValueError("等待时长应为 0–120 秒")
    validate_schedule(settings, settings["wait_seconds"], "默认")
    custom = settings.get("weekday_overrides", {})
    if not isinstance(custom, dict) or set(custom) - {str(i) for i in range(1, 6)}:
        raise ValueError("星期配置仅支持周一至周五")
    for weekday, windows in custom.items():
        if not isinstance(windows, dict) or set(windows) - set(SLOTS):
            raise ValueError("星期场次配置不正确")
        for window in windows.values():
            if not isinstance(window, dict) or set(window) != {"start", "end"}:
                raise ValueError("请填写完整的星期时间窗口")
        schedule = weekly_schedule(settings, date(2024, 1, int(weekday)))
        validate_schedule(schedule, settings["wait_seconds"], f"周{weekday}")
    if settings["package"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", settings["package"]):
        raise ValueError("应用包名格式不正确")


def signature(schedule, slot, wait):
    window = schedule[slot]
    return (True, window["start"], window["end"], wait) if window["enabled"] else (False,)


class Planner:
    def __init__(self, store, clock=now, rng=None, lock=None):
        self.store, self.clock = store, clock
        self.rng = rng or random.SystemRandom()
        self.lock = lock or threading.RLock()

    def schedule(self, day, *, initial=False):
        with self.store.connect() as db:
            settings = self.store.settings(db)
            overrides = load_overrides(db)
            if initial and overrides.get(day.isoformat(), {}).get("mode") != "work":
                return weekly_schedule(settings, day)
            return effective_schedule(settings, overrides, day)

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

    def _ensure(self, db, start, end, at, previous=None):
        settings = self.store.settings(db)
        overrides = load_overrides(db)
        day = max(start, at.date())
        while day <= end:
            schedule = effective_schedule(settings, overrides, day)
            before = effective_schedule(previous[0], previous[1], day) if previous else schedule
            for slot in SLOTS:
                history = [dict(row) for row in db.execute(
                    "SELECT p.*,r.status AS run_status,r.dispatch FROM plans p LEFT JOIN runs r ON r.plan_id=p.id "
                    "WHERE p.day=? AND p.slot=? ORDER BY p.id DESC", (day.isoformat(), slot))]
                protected = any(p["status"] not in {"pending", "queued", "cancelled"}
                                or p["run_status"] in {"running", "completed", "failed", "unknown", "missed"}
                                or json.loads(p["dispatch"] or "{}").get("sent") for p in history)
                old = history[0] if history else None
                changed = previous is not None and signature(before, slot, previous[0]["wait_seconds"]) != signature(schedule, slot, settings["wait_seconds"])
                enabled = schedule[slot]["enabled"]
                if protected:
                    for plan in history:
                        if plan["status"] in {"pending", "queued"} and plan["run_status"] in {None, "queued"} and not json.loads(plan["dispatch"] or "{}").get("sent"):
                            self._cancel(db, plan, "该场次已有执行或终态记录", at)
                    continue
                if old and old["status"] in {"pending", "queued"}:
                    if not enabled or changed:
                        self._cancel(db, old, "计划配置已修改", at)
                    else:
                        continue
                if enabled and (old is None or changed):
                    self._new(db, day, slot, {**settings, **schedule}, at)
            day += timedelta(days=1)

    def ensure(self, start=None, end=None):
        at = self.clock()
        start, end = start or at.date(), end or at.date() + timedelta(days=30)
        with self.lock, self.store.connect(write=True) as db:
            self._ensure(db, start, end, at)

    def update_settings(self, values):
        at = self.clock()
        with self.lock, self.store.connect(write=True) as db:
            old = self.store.settings(db)
            overrides = load_overrides(db)
            new = {**old, **values}
            validate(new)
            for day, entry in overrides.items():
                if day >= at.date().isoformat() and entry["mode"] == "work":
                    validate_schedule(entry["schedule"], new["wait_seconds"], day + " ")
            for key, value in values.items():
                self.store.put(db, key, value)
            days = {row[0] for row in db.execute("SELECT DISTINCT day FROM plans WHERE day>=?", (at.date().isoformat(),))}
            end = max([at.date() + timedelta(days=30), *(date.fromisoformat(d) for d in days)])
            self._ensure(db, at.date(), end, at, (old, overrides))

    def override(self, start, end, mode, schedule=None):
        at = self.clock()
        if mode not in MODES or start < at.date() or end < start or (end-start).days > 366:
            raise ValueError("请选择今天起、日期差不超过 366 天的有效日期范围")
        if schedule is not None and start != end:
            raise ValueError("自定义时间仅支持单日调整")
        with self.lock, self.store.connect(write=True) as db:
            settings = self.store.settings(db)
            previous = (settings, load_overrides(db))
            day = start
            while day <= end:
                if mode == "default":
                    db.execute("DELETE FROM day_overrides WHERE day=?", (day.isoformat(),))
                else:
                    snapshot = (schedule if schedule is not None else weekly_schedule(settings, day)) if mode == "work" else {}
                    if mode == "work":
                        validate_schedule(snapshot, settings["wait_seconds"], day.isoformat() + " ")
                    db.execute("INSERT INTO day_overrides(day,mode,schedule) VALUES (?,?,?) "
                               "ON CONFLICT(day) DO UPDATE SET mode=excluded.mode,schedule=excluded.schedule",
                               (day.isoformat(), mode, json.dumps(snapshot)))
                day += timedelta(days=1)
            self._ensure(db, start, end, at, previous)

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
