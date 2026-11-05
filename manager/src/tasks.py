"""A small scheduler, one HTTP phone worker, monitoring and Bark notifications."""
from __future__ import annotations

from datetime import datetime
import itertools
import json
import logging
import queue
import secrets
import threading
import time
import urllib.error
import urllib.request

from .bark import screenshot_signer
from .controller_client import ControllerClient, ControllerError, ControllerImageError
from .planner import Planner, SLOTS, controller_configured, workday
from .storage import now, stamp

KINDS = {"full": "手动执行", "test": "手机连接测试", "screenshot": "获取当前截图"}
METHODS = {"full": "run_app", "test": "test_device", "screenshot": "screenshot"}
STEP_NAMES = {"wake": "点亮屏幕", "unlock": "解除锁屏", "home_before": "返回手机桌面",
              "stop_before": "关闭目标应用", "launch": "启动目标应用", "delay": "等待应用就绪",
              "desktop_wait": "等待桌面稳定", "screen_wait": "等待屏幕就绪", "screenshot": "保存本次截图",
              "home_after": "返回手机桌面", "stop_after": "再次关闭目标应用", "lock_after": "锁定屏幕"}
TERMINAL = {"completed", "failed", "missed", "unknown", "cancelled"}
LOG = logging.getLogger(__name__)


class Service:
    def __init__(self, store, *, client_factory=ControllerClient, clock=now, sender=None):
        self.store, self.clock = store, clock
        self.client_factory = client_factory
        self.planner = Planner(store, clock)
        self.sender = sender or self._send_bark
        self.phone_queue = queue.PriorityQueue()
        self.notice_queue = queue.Queue()
        self.sequence = itertools.count()
        self.lock = threading.RLock()
        self.phone_busy = False
        self.queued_notices = set()
        self.stop_event = threading.Event()
        self.probe_event = threading.Event()
        self.threads = []
        self.last_day = None
        self.poll_seconds = 2
        self.not_found_seconds = 60

    def recover(self):
        self.planner.recover()
        with self.store.connect() as db:
            rows = db.execute("SELECT id,status,trigger FROM runs WHERE status IN ('queued','running') ORDER BY created_at").fetchall()
        for row in rows:
            if not self.store.dispatch(row["id"]):
                # Legacy local ADB tasks have no remote request to recover.
                status = "unknown" if row["status"] == "running" else "missed" if row["trigger"] == "automatic" else "cancelled"
                self.store.change_run(row["id"], status=status, reason="旧版本遗留任务，请核对后手动执行", finished_at=stamp(self.clock()),
                                      bark_status="not_needed" if status == "cancelled" else "pending")
                continue
            priority = -10 if row["status"] == "running" else 0 if row["trigger"] == "manual" else 10
            self.phone_queue.put((priority, next(self.sequence), row["id"]))
        for notice in self.store.get("_notices", []):
            if notice["status"] == "queued":
                self.notice_queue.put(("event", notice["id"]))

    def start(self):
        self.recover()
        self.planner.ensure()
        self.last_day = self.clock().date()
        for name, target in [("phone", self._phone_loop), ("scheduler", self._scheduler_loop),
                             ("monitor", self._monitor_loop), ("notifications", self._notice_loop)]:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        self.probe_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        # In-flight HTTP requests stay persisted and are queried on the next start.

    def submit(self, kind, request_id):
        if kind not in KINDS:
            raise ValueError("未知操作")
        with self.lock, self.store.connect(write=True) as db:
            previous = db.execute("SELECT id FROM runs WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                return previous["id"]
            settings = self.store.settings(db)
            if not controller_configured(settings):
                raise ValueError("请先在设备设置中填写控制端地址和密钥")
            previous = db.execute("SELECT id FROM runs WHERE kind=? AND trigger='manual' AND status IN ('queued','running') LIMIT 1", (kind,)).fetchone()
            if previous:
                return previous["id"]
            run_id = self.store.add_run(db, kind=kind, request_id=request_id, at=self.clock())
        self.phone_queue.put((0, next(self.sequence), run_id))
        return run_id

    def tick(self):
        at = self.clock()
        if self.last_day != at.date():
            self.planner.ensure()
            self.last_day = at.date()
        with self.lock, self.store.connect(write=True) as db:
            settings = self.store.settings(db)
            overrides = dict(db.execute("SELECT day,mode FROM day_overrides").fetchall())
            for row in db.execute("SELECT * FROM plans WHERE status='pending' AND scheduled_at<=? ORDER BY scheduled_at", (stamp(at),)).fetchall():
                plan = dict(row)
                age = (at - datetime.fromisoformat(plan["scheduled_at"])).total_seconds()
                reason = ""
                if settings["paused"]:
                    reason = "计划时刻自动任务已暂停"
                elif not workday(settings, overrides, datetime.fromisoformat(plan["scheduled_at"]).date()) or not settings[plan["slot"]]["enabled"]:
                    reason = "当前日期或场次设置跳过"
                elif not controller_configured(settings):
                    reason = "控制端未配置"
                elif age > 5:
                    reason = "已错过计划时刻"
                elif self.phone_busy or not self.phone_queue.empty() or db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                    reason = "计划时刻手机正在执行其他操作"
                if reason:
                    self.store.finish_plan(db, plan, "missed", reason, at)
                else:
                    db.execute("UPDATE plans SET status='queued' WHERE id=?", (plan["id"],))
                    run_id = self.store.add_run(db, kind="full", trigger="automatic", plan=plan, at=at)
                    self.phone_queue.put((10, next(self.sequence), run_id))
        with self.store.connect() as db:
            rows = db.execute("SELECT id FROM runs WHERE bark_status='pending' AND status IN ('completed','failed','missed','unknown')").fetchall()
        for row in rows:
            self.enqueue_notice(row["id"])

    def refresh_connection(self):
        self.store.update({"_device_state": {}})
        self.probe_event.set()

    def probe(self):
        settings = self.store.settings()
        previous = self.store.get("_device_state", {})
        current = {"controller_connected": False, "controller_status": "unconfigured", "connected": None,
                   "serial": settings["serial"], "brand": "未知", "model": "未知", "devices": [], "reason": "请配置控制端地址和密钥"}
        if controller_configured(settings):
            try:
                client = self.client_factory(settings["controller_url"], settings["controller_api_key"])
                health = client.health()
                if health.http_status != 200:
                    current.update(controller_status="error", reason=health.error["message"])
                else:
                    current.update(controller_connected=True, controller_status="online", busy=health.payload["busy"])
                    result = client.devices()
                    if result.http_status != 200:
                        current.update(connected=False, reason=result.error["message"])
                    else:
                        devices = result.payload["devices"]
                        current["devices"] = devices
                        usable = [item for item in devices if item["usable"]]
                        selected = next((item for item in devices if item["serial"] == settings["serial"]), None) if settings["serial"] else (usable[0] if len(usable) == 1 else devices[0] if len(devices) == 1 else None)
                        if selected:
                            state = selected["state"]
                            current.update(connected=selected["usable"], serial=selected["serial"],
                                           brand=selected.get("brand") or "未知", model=selected.get("model") or "未知", state=state,
                                           reason="" if selected["usable"] else {"offline": "设备离线", "unauthorized": "手机尚未授权 USB 调试"}.get(state, state))
                        else:
                            reason = "目标设备未连接" if settings["serial"] else "多台设备可用，请选择目标设备" if len(usable) > 1 else "没有可用设备"
                            current.update(connected=False, reason=reason)
                            if not usable and devices:
                                current["reason"] += "：" + "、".join(item["state"] for item in devices)
            except (ControllerError, ValueError) as error:
                current.update(controller_connected=False, controller_status="unreachable", connected=None, reason=str(error))
        current["checked_at"] = stamp(self.clock())
        # A settings save can happen while the monitor is waiting for HTTP.
        latest = self.store.settings()
        if any(settings[key] != latest[key] for key in ("controller_url", "controller_api_key", "serial")):
            return current
        self.store.update({"_device_state": current})
        keys = ("controller_status", "connected", "serial", "reason")
        if previous and any(previous.get(key) != current.get(key) for key in keys):
            label = "控制端不可用" if not current["controller_connected"] else "设备已连接" if current["connected"] else "设备不可用"
            self.create_notice(label, current["reason"] or f"{current['brand']} · {current['model']} · {current['serial']}")
        return current

    def _monitor_loop(self):
        while not self.stop_event.is_set():
            self.probe_event.clear()
            try:
                self.probe()
            except Exception:
                LOG.exception("Device monitor failed")
            self.probe_event.wait(5)

    def _scheduler_loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                LOG.exception("Scheduler check failed")
            self.stop_event.wait(1)

    def _phone_loop(self):
        while not self.stop_event.is_set():
            try:
                _, _, run_id = self.phone_queue.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                self.execute(run_id)
            except Exception:
                LOG.exception("Phone worker interrupted")
                self._finish(run_id, status="unknown", reason="管理端执行器中断，请核对后决定是否手动执行")
            finally:
                with self.lock:
                    self.phone_busy = False
                self.phone_queue.task_done()

    def execute(self, run_id):
        with self.lock:
            run = self.store.run(run_id)
            if not run or run["status"] not in {"queued", "running"}:
                return
            dispatch = self.store.dispatch(run_id)
            if not dispatch.get("sent") and run["trigger"] == "automatic":
                with self.store.connect() as db:
                    plan = dict(db.execute("SELECT * FROM plans WHERE id=?", (run["plan_id"],)).fetchone())
                    settings = self.store.settings(db)
                    overrides = dict(db.execute("SELECT day,mode FROM day_overrides").fetchall())
                    valid = (plan["status"] in {"queued", "running"} and not settings["paused"] and settings[plan["slot"]]["enabled"]
                             and workday(settings, overrides, datetime.fromisoformat(plan["scheduled_at"]).date())
                             and (self.clock() - datetime.fromisoformat(plan["scheduled_at"])).total_seconds() <= 5
                             and not db.execute("SELECT 1 FROM runs WHERE trigger='manual' AND status='queued' LIMIT 1").fetchone())
                if not valid:
                    self._finish(run_id, status="missed", reason="启动前检查：计划已失效或错过")
                    return
            self.phone_busy = True
            self.store.change_run(run_id, status="running", started_at=run["started_at"] or stamp(self.clock()))
        try:
            client = self.client_factory(dispatch["url"], dispatch["api_key"])
            polling = dispatch["sent"]
            not_found_since = None
            while not self.stop_event.is_set():
                try:
                    if polling:
                        result = client.get_request(run["request_id"])
                    else:
                        params = dispatch["params"].copy()
                        settings = self.store.settings()
                        if settings["phone_pin"]:
                            if (dispatch["url"] != settings["controller_url"] or
                                    params["serial"] != settings["serial"]):
                                self._finish(run_id, status="failed", reason="设备配置已变更，请重新发起任务")
                                return
                            params["pin"] = settings["phone_pin"]
                        # Commit before POST; a restart from this point always queries first.
                        dispatch["sent"] = True
                        self.store.change_run(run_id, dispatch=dispatch)
                        result = getattr(client, METHODS[run["kind"]])(run["request_id"], **params)
                except ControllerError:
                    polling = True
                    self.store.change_run(run_id, reason="控制端暂不可达，正在按原请求编号查询结果")
                else:
                    if result.terminal or result.expired:
                        self._finish(run_id, result=result)
                        return
                    if result.http_status == 409 and result.error["code"] == "DEVICE_BUSY" and not polling:
                        if run["trigger"] == "automatic":
                            self._finish(run_id, result=result, status="missed", reason="控制端忙，已错过计划时刻")
                            return
                        self.store.change_run(run_id, reason="控制端忙，等待当前操作完成")
                        # Busy means not accepted. Waiting manual tasks reuse the same ID.
                    elif result.http_status == 404 and polling:
                        not_found_since = not_found_since if not_found_since is not None else time.monotonic()
                        if time.monotonic() - not_found_since >= self.not_found_seconds:
                            self._finish(run_id, result=result, status="unknown", reason="控制端未查到原请求，未自动重发，请核对后手动执行")
                            return
                        self.store.change_run(run_id, reason="暂未查到原请求，继续确认中")
                    elif result.status in {"accepted", "running"}:
                        not_found_since = None
                        polling = True
                        step = result.payload.get("current_step")
                        self.store.change_run(run_id, reason="控制端正在执行", steps=[{"name": STEP_NAMES.get(step, step or "等待执行"), "state": "running", "error": ""}])
                    else:
                        self._finish(run_id, result=result, status="unknown" if polling else "failed")
                        return
                if self.stop_event.wait(self.poll_seconds):
                    return
        except ValueError as error:
            self._finish(run_id, status="unknown" if dispatch.get("sent") else "failed", reason=str(error))
        finally:
            with self.lock:
                self.phone_busy = False

    def _finish(self, run_id, *, result=None, status=None, reason=""):
        run = self.store.run(run_id)
        payload = dict(result.payload) if result else {}
        if status is None:
            status = {"succeeded": "completed", "failed": "failed", "unknown": "unknown"}.get(payload.get("status"), "unknown")
        errors = [payload.get("error"), *payload.get("cleanup_errors", [])]
        if not reason:
            reason = "；".join(dict.fromkeys(item["message"] for item in errors if item))
        if result and result.expired:
            reason = "控制端结果已过期，保留原状态，本次截图不可获取"
        steps = [{"name": STEP_NAMES.get(item["name"], item["name"]),
                  "state": {"succeeded": "completed"}.get(item["status"], item["status"]),
                  "error": item["error"]["message"] if item.get("error") else ""} for item in payload.get("steps", [])]
        values = {"status": status, "reason": reason, "steps": steps,
                  "finished_at": payload.get("finished_at") or stamp(self.clock())}
        if payload.get("started_at"):
            values["started_at"] = payload["started_at"]
        if result:
            filename = f"{run['day']}/{run_id}.png"
            try:
                if result.save_screenshot(self.store.screenshots / filename):
                    values.update(screenshot=filename, screenshot_at=payload["screenshot"]["captured_at"])
            except (ControllerImageError, OSError) as error:
                values["reason"] = (reason + "；" if reason else "") + f"截图未保存：{error}"
        if payload.get("screenshot"):
            payload["screenshot"] = {key: value for key, value in payload["screenshot"].items() if key != "base64"}
        payload["http_status"] = result.http_status if result else None
        values["remote_result"] = payload
        device = self.store.get("_device_state", {})
        serial = payload.get("serial", self.store.dispatch(run_id).get("params", {}).get("serial", ""))
        values["device"] = {"serial": serial, "brand": device.get("brand", "未知") if serial == device.get("serial") else "未知",
                            "model": device.get("model", "未知") if serial == device.get("serial") else "未知"}
        dispatch = self.store.dispatch(run_id)
        dispatch.pop("api_key", None)  # Finished history does not need an old credential.
        values["dispatch"] = dispatch
        self.store.change_run(run_id, **values)
        if status != "cancelled":
            self.enqueue_notice(run_id)

    @staticmethod
    def _send_bark(settings, title, body, image=""):
        if not settings["bark_key"]:
            return "not_configured", "未配置 Bark 密钥"
        payload = {"device_key": settings["bark_key"], "title": title, "body": body, "group": "打卡助手"}
        if image:
            payload["image"] = image
        request = urllib.request.Request(settings["bark_server"].rstrip("/")+"/push", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read(65536))
            if result.get("code") != 200:
                return "failed", "Bark 返回发送失败"
            return "sent", ""
        except (OSError, ValueError, urllib.error.URLError):
            return "failed", "Bark 发送失败，请检查地址、密钥和网络"

    def enqueue_notice(self, run_id):
        with self.lock:
            if run_id not in self.queued_notices:
                self.queued_notices.add(run_id)
                self.notice_queue.put(("run", run_id))

    def create_notice(self, title, body):
        notice_id = secrets.token_hex(10)
        with self.store.connect(write=True) as db:
            notices = self.store.get("_notices", [], db)
            notices.insert(0, {"id": notice_id, "title": title, "body": body, "at": stamp(self.clock()), "status": "queued", "error": ""})
            self.store.put(db, "_notices", notices[:30])
        self.notice_queue.put(("event", notice_id))
        return notice_id

    def send_notice(self, kind, identifier):
        settings = self.store.settings()
        if kind == "run":
            run = self.store.run(identifier)
            if not run or run["status"] not in TERMINAL:
                return
            labels = {"completed": "操作完成", "failed": "执行失败", "missed": "计划已错过", "unknown": "结果未知"}
            name = SLOTS.get(run["slot"], KINDS[run["kind"]])
            title = f"{name} · {labels.get(run['status'], run['status'])}"
            body = f"{run['created_at'][:19]}\n{run['reason'] or '手机操作完成，请查看本次截图。'}"
            if not run["screenshot"]:
                body += "\n本次未采集到截图。"
            image = ""
            if run["screenshot"] and settings["public_url"]:
                token = screenshot_signer(self.store.get("_secret")).dumps(identifier)
                image = settings["public_url"].rstrip("/")+"/bark/screenshots/"+token+".png"
            self.store.change_run(identifier, bark_status="sending", bark_error="")
            status, error = self.sender(settings, title, body, image)
            self.store.change_run(identifier, bark_status=status, bark_error=error)
        else:
            entry = next((n for n in self.store.get("_notices", []) if n["id"] == identifier), None)
            if not entry:
                return
            status, error = self.sender(settings, entry["title"], entry["body"])
            with self.store.connect(write=True) as db:
                notices = self.store.get("_notices", [], db)
                for item in notices:
                    if item["id"] == identifier:
                        item.update(status=status, error=error)
                self.store.put(db, "_notices", notices)

    def _notice_loop(self):
        while not self.stop_event.is_set():
            try:
                kind, identifier = self.notice_queue.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                self.send_notice(kind, identifier)
            except Exception:
                if kind == "run":
                    self.store.change_run(identifier, bark_status="failed", bark_error="发送中断，请重发通知")
                else:
                    with self.store.connect(write=True) as db:
                        notices = self.store.get("_notices", [], db)
                        for entry in notices:
                            if entry["id"] == identifier:
                                entry.update(status="failed", error="通知发送中断，请重新测试")
                        self.store.put(db, "_notices", notices)
            finally:
                with self.lock:
                    self.queued_notices.discard(identifier)
                self.notice_queue.task_done()
