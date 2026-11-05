"""Single-file Windows ADB API; business scheduling belongs to the manager."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import zlib

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

VERSION = "1.1.0"
TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("adb_controller")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\Z")
SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,255}\Z")
DEFAULTS = {
    "adb_dir": "./adb", "host": "0.0.0.0", "port": 8765,
    "default_serial": "", "default_package": "com.tencent.wework",
    "default_delay": 10, "max_delay": 120, "command_timeout": 20,
    "data_dir": "./data", "result_retention_hours": 24,
}


def now():
    return datetime.now(TZ)


def stamp(value):
    return value.astimezone(TZ).isoformat(timespec="milliseconds")


class APIError(RuntimeError):
    def __init__(self, status, code, message, step=None):
        super().__init__(message)
        self.status, self.code, self.message, self.step = status, code, message, step

    def detail(self):
        return {"code": self.code, "message": self.message, "step": self.step}


def invalid(message):
    return APIError(400, "INVALID_ARGUMENT", message)


def validate_serial(value):
    if not isinstance(value, str) or not SERIAL.fullmatch(value):
        raise invalid("serial 格式不正确")
    return value


def validate_package(value):
    if not isinstance(value, str) or not PACKAGE.fullmatch(value) or len(value) > 255:
        raise invalid("package 应为有效的应用包名")
    return value


@dataclass(frozen=True)
class Config:
    adb_exe: Path
    api_key: str
    host: str
    port: int
    default_serial: str
    default_package: str
    default_delay: int
    max_delay: int
    command_timeout: int
    data_dir: Path
    result_retention_hours: int

    @classmethod
    def load(cls, filename):
        filename = Path(filename).resolve()
        try:
            raw = json.loads(filename.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise ValueError(f"无法读取配置文件 {filename.name}：{error}") from None
        if not isinstance(raw, dict) or set(raw) - (set(DEFAULTS) | {"api_key"}):
            raise ValueError("配置应为 JSON 对象，且只能包含示例配置中的字段")
        values = {**DEFAULTS, **raw}
        key = values.get("api_key", "")
        if (not isinstance(key, str) or len(key) < 32 or len(key) > 512
                or not key.isascii() or any(not 33 <= ord(c) <= 126 for c in key)
                or key == "REPLACE_WITH_A_RANDOM_API_KEY"):
            raise ValueError("api_key 须为 32～512 位可打印 ASCII 字符；请先执行 --init-config")
        for name in ("port", "default_delay", "max_delay", "command_timeout", "result_retention_hours"):
            if type(values[name]) is not int:
                raise ValueError(f"{name} 须为整数")
        if not 1 <= values["port"] <= 65535:
            raise ValueError("port 须在 1～65535 之间")
        if not 0 <= values["default_delay"] <= values["max_delay"]:
            raise ValueError("default_delay 须在 0～max_delay 之间")
        if values["command_timeout"] <= 0 or values["result_retention_hours"] <= 0:
            raise ValueError("command_timeout 和 result_retention_hours 须大于零")
        for name in ("adb_dir", "data_dir", "host"):
            if not isinstance(values[name], str) or not values[name].strip():
                raise ValueError(f"{name} 不能为空")
        if not isinstance(values["default_serial"], str):
            raise ValueError("default_serial 须为字符串")
        try:
            if values["default_serial"]:
                validate_serial(values["default_serial"])
            validate_package(values["default_package"])
        except APIError as error:
            raise ValueError(error.message) from None
        adb_dir = (filename.parent / values.pop("adb_dir")).resolve()
        adb_exe = adb_dir / ("adb.exe" if sys.platform == "win32" else "adb")
        if not adb_exe.is_file():
            raise ValueError(f"ADB 文件不存在：{adb_exe}")
        values["data_dir"] = (filename.parent / values["data_dir"]).resolve()
        return cls(adb_exe=adb_exe, **values)


def valid_png(data):
    """Check PNG chunks, dimensions and CRC before persisting binary output."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset, seen_header, seen_pixels = 8, False, False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            return False
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            return False
        if not seen_header:
            if kind != b"IHDR" or length != 13 or not all(struct.unpack(">II", payload[:8])):
                return False
            seen_header = True
        elif kind == b"IHDR":
            return False
        if kind == b"IDAT":
            seen_pixels = True
        if kind == b"IEND":
            return length == 0 and seen_pixels and end == len(data)
        offset = end
    return False


class Adb:
    def __init__(self, config):
        self.config = config

    def command(self, *args, serial=None, binary=False):
        command = [str(self.config.adb_exe)]
        if serial:
            command += ["-s", serial]
        command += list(args)
        environment = os.environ.copy()
        environment.pop("ANDROID_SERIAL", None)
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=self.config.command_timeout, env=environment,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise APIError(504, "ADB_TIMEOUT", "ADB 命令超时") from None
        except OSError:
            raise APIError(503, "ADB_UNAVAILABLE", "无法启动 ADB，请检查 adb_dir 和运行权限") from None
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace").strip()
            if not detail and not binary:
                detail = result.stdout.decode("utf-8", "replace").strip()
            raise APIError(502, "ADB_FAILED", detail[:500] or f"ADB 返回错误 {result.returncode}")
        return result.stdout if binary else result.stdout.decode("utf-8", "replace").strip()

    def devices(self, *, metadata=True):
        devices = []
        output = self.command("devices", "-l")
        states = {"device", "offline", "unauthorized", "recovery", "sideload", "bootloader", "authorizing", "connecting"}
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 2 or (parts[1] not in states and "no permissions" not in line):
                continue
            state = "no permissions" if "no permissions" in line else parts[1]
            device = {"serial": parts[0], "state": state, "usable": state == "device", "brand": None, "model": None}
            if metadata and device["usable"]:
                for name in ("brand", "model"):
                    try:
                        device[name] = self.command("shell", "getprop", f"ro.product.{name}", serial=parts[0]) or None
                    except APIError:
                        pass
            devices.append(device)
        return devices

    def select(self, requested):
        devices = self.devices(metadata=False)
        target = requested or self.config.default_serial
        if target:
            device = next((item for item in devices if item["serial"] == target), None)
            if not device or not device["usable"]:
                state = device["state"] if device else "未连接"
                raise APIError(503, "DEVICE_UNAVAILABLE", f"目标设备不可用：{state}")
            return target
        usable = [item for item in devices if item["usable"]]
        if not usable:
            raise APIError(503, "DEVICE_UNAVAILABLE", "没有可用设备，请检查连接和调试授权")
        if len(usable) > 1:
            raise APIError(409, "DEVICE_AMBIGUOUS", "存在多台可用设备，请指定 serial")
        return usable[0]["serial"]

    def check_package(self, serial, package):
        output = self.command("shell", "cmd", "package", "resolve-activity", "--brief", "--user", "current",
                              "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
                              "-p", package, serial=serial)
        if not any(re.fullmatch(r"[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+", line.strip()) for line in output.splitlines()):
            raise APIError(422, "PACKAGE_UNAVAILABLE", "目标应用未安装或没有可启动的桌面入口")

    def launch(self, serial, package):
        output = self.command("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1", serial=serial)
        lowered = output.lower()
        if (any(marker in lowered for marker in ("monkey aborted", "no activities found", "error:", "exception", "** crash", "** not responding"))
                or not re.search(r"Events injected:\s*1\b", output)):
            raise APIError(502, "APP_LAUNCH_FAILED", output[:500] or "未能确认应用启动命令成功")

    def screenshot(self, serial):
        data = self.command("exec-out", "screencap", "-p", serial=serial, binary=True)
        if not valid_png(data):
            raise APIError(502, "SCREENSHOT_INVALID", "截图输出不是有效 PNG")
        return data


class Store:
    def __init__(self, config, clock=now):
        self.clock, self.config = clock, config
        self.directory = config.data_dir
        self.directory.mkdir(parents=True, exist_ok=True)
        self.screenshots = self.directory / "screenshots"
        self.screenshots.mkdir(exist_ok=True)
        self.path = self.directory / "requests.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, params TEXT NOT NULL,
                    status TEXT NOT NULL, step TEXT, created_at TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, http_status INTEGER NOT NULL DEFAULT 202, result TEXT,
                    screenshot TEXT, expires_at REAL, expired INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS request_expiry ON requests(expired, expires_at);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, request_id):
        with self.lock:
            self.prune()
            with self.connect() as db:
                row = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                return None
            row = dict(row)
            row["params"] = json.loads(row["params"])
            row["result"] = json.loads(row["result"]) if row["result"] else None
            return row

    def accept(self, request_id, operation, params):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO requests(request_id,operation,params,status,created_at) VALUES (?,?,?,?,?)",
                       (request_id, operation, json.dumps(params), "accepted", stamp(self.clock())))

    def progress(self, request_id, step):
        with self.lock, self.connect() as db:
            db.execute("UPDATE requests SET status='running',step=?,started_at=COALESCE(started_at,?) WHERE request_id=?",
                       (step, stamp(self.clock()), request_id))

    def finish(self, request_id, result, http_status, screenshot=None):
        with self.lock, self.connect() as db:
            db.execute("UPDATE requests SET status=?,step=NULL,finished_at=?,http_status=?,result=?,screenshot=?,expires_at=? WHERE request_id=?",
                       (result["status"], result["finished_at"], http_status, json.dumps(result, ensure_ascii=False), screenshot,
                        (self.clock() + timedelta(hours=self.config.result_retention_hours)).timestamp(), request_id))

    def render(self, request_id, *, query=False):
        with self.lock:
            row = self.get(request_id)
            if row is None:
                raise APIError(404, "REQUEST_NOT_FOUND", "请求编号不存在")
            payload = {"request_id": request_id, "operation": row["operation"], "status": row["status"],
                       **row["params"], "created_at": row["created_at"], "started_at": row["started_at"],
                       "finished_at": row["finished_at"], "current_step": row["step"]}
            if row["expired"]:
                payload["error"] = APIError(410, "RESULT_EXPIRED", "完整结果和截图已过期，原请求不会重新执行").detail()
                return payload, 410
            if row["result"]:
                payload.update(row["result"])
                if payload.get("screenshot"):
                    try:
                        data = (self.screenshots / row["screenshot"]).read_bytes()
                    except OSError:
                        raise APIError(500, "RESULT_UNAVAILABLE", "已保存的截图文件不可读取，原请求不会重新执行") from None
                    payload["screenshot"]["base64"] = base64.b64encode(data).decode("ascii")
            return payload, 200 if query else row["http_status"]

    def recover(self):
        with self.lock, self.connect() as db:
            pending = db.execute("SELECT * FROM requests WHERE status IN ('accepted','running')").fetchall()
        for row in pending:
            result = {"request_id": row["request_id"], "status": "unknown", "screenshot": None,
                      "finished_at": stamp(self.clock()), "error": APIError(200, "RESULT_UNKNOWN", "控制端中断，执行结果未知，请核对后决定是否重新执行", row["step"]).detail(),
                      "cleanup_errors": []}
            self.finish(row["request_id"], result, 200)

    def prune(self):
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT request_id,screenshot FROM requests WHERE (expired=0 AND expires_at<=?) OR (expired=1 AND screenshot IS NOT NULL)",
                              (self.clock().timestamp(),)).fetchall()
            for row in rows:
                remaining = row["screenshot"]
                if remaining:
                    try:
                        (self.screenshots / remaining).unlink(missing_ok=True)
                        remaining = None
                    except OSError:
                        LOG.warning("Could not remove expired screenshot for %s", row["request_id"])
                db.execute("UPDATE requests SET expired=1,result=NULL,screenshot=? WHERE request_id=?", (remaining, row["request_id"]))


class Controller:
    def __init__(self, config, *, adb=None, clock=now, sleeper=time.sleep):
        self.config, self.clock, self.sleeper = config, clock, sleeper
        self.adb = adb or Adb(config)
        self.store = Store(config, clock)
        self.store.recover()
        self.guard = threading.RLock()
        self.current_id = None
        self.thread = None
        self.closing = False
        self.started_at, self.started_tick = clock(), time.monotonic()
        self.maintenance_stop = threading.Event()
        self.maintenance_thread = None

    def health(self):
        return {"version": VERSION, "started_at": stamp(self.started_at),
                "uptime_seconds": int(time.monotonic() - self.started_tick),
                "busy": self.current_id is not None, "current_request_id": self.current_id}

    def validate_body(self, operation, body):
        if not isinstance(body, dict):
            raise invalid("请求体须为 JSON 对象")
        allowed = {"request_id", "serial", "pin"} | ({"package", "delay"} if operation == "app/run" else set())
        if set(body) - allowed:
            raise invalid("请求包含不支持的字段")
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise invalid("request_id 须为 1～64 位字母、数字、短横线或下划线")
        if "serial" in body:
            validate_serial(body["serial"])
        if "pin" in body and (not isinstance(body["pin"], str) or not re.fullmatch(r"[0-9]{4,16}", body["pin"])):
            raise invalid("pin 须为 4～16 位数字字符串")
        if "package" in body:
            validate_package(body["package"])
        if "delay" in body and (type(body["delay"]) is not int or body["delay"] < 0):
            raise invalid("delay 须为非负整数秒数")
        return request_id

    def submit(self, operation, body):
        request_id = self.validate_body(operation, body)
        body = body.copy()
        pin = body.pop("pin", None)  # Credential for this attempt only; never include it in the request ledger.
        with self.guard:
            previous = self.store.get(request_id)
            if previous:
                if previous["operation"] != operation or any(previous["params"].get(key) != value for key, value in body.items() if key != "request_id"):
                    raise APIError(409, "REQUEST_CONFLICT", "该 request_id 已绑定其他操作或参数")
                return self.store.render(request_id)
            if body.get("delay", self.config.default_delay) > self.config.max_delay:
                raise invalid("delay 须为 0～max_delay 之间的整数秒数")
            if self.closing:
                raise APIError(503, "SERVICE_STOPPING", "控制端正在停止，暂不受理新操作")
            if self.current_id is not None:
                raise APIError(409, "DEVICE_BUSY", "控制端正在执行其他操作，请稍后使用新的操作请求")
            self.current_id = request_id
            try:
                params = {"serial": self.adb.select(body.get("serial"))}
                if operation == "app/run":
                    params.update(package=body.get("package", self.config.default_package), delay=body.get("delay", self.config.default_delay))
                    self.adb.check_package(params["serial"], params["package"])
                self.store.accept(request_id, operation, params)
                completed = threading.Event()
                self.thread = threading.Thread(target=self._execute, args=(request_id, operation, params, completed, pin),
                                               name=f"phone-{request_id}", daemon=False)
                try:
                    self.thread.start()
                except Exception:
                    result = {"status": "failed", "screenshot": None, "finished_at": stamp(self.clock()),
                              "error": APIError(500, "INTERNAL_ERROR", "无法启动执行线程").detail(), "cleanup_errors": []}
                    self.store.finish(request_id, result, 500)
                    raise
            except Exception:
                self.current_id = None
                raise
        # A dedicated worker completes the operation even if the HTTP client disconnects.
        completed.wait()
        return self.store.render(request_id)

    def _execute(self, request_id, operation, params, completed, pin=None):
        serial = params["serial"]
        screenshot, screenshot_file = None, None
        error, cleanup_errors, steps = None, [], []
        cleanup = {"home": False, "force_stop": False, "lock": False} if operation == "app/run" else None
        started = stamp(self.clock())
        http_status = 200

        def perform(name, action, *, is_cleanup=False):
            item = {"name": name, "status": "running", "error": None}
            steps.append(item)
            try:
                try:
                    self.store.progress(request_id, name)
                except Exception:
                    if not is_cleanup:
                        raise
                    LOG.exception("Could not persist cleanup progress for %s", request_id)
                value = action()
            except Exception as caught:
                failure = caught if isinstance(caught, APIError) else APIError(500, "INTERNAL_ERROR", "控制端内部操作失败，请检查日志")
                if not isinstance(caught, APIError):
                    LOG.exception("Operation failed for %s at %s", request_id, name)
                failure.step = name
                item.update(status="failed", error=failure.detail())
                raise failure from None
            item["status"] = "succeeded"
            return value

        def adb(*args):
            return self.adb.command(*args, serial=serial)

        def locked():
            policy = adb("shell", "dumpsys", "window", "policy")
            block = re.search(r"(?m)^\s*KeyguardServiceDelegate[^\n]*\n((?:[ \t]+\w+=[^\n]*\n?)+)", policy)
            fields = dict(re.findall(r"(?m)^\s*(\w+)=(true|false)\s*$", block[1])) if block else {}
            if fields.get("showing") == "false":
                return False
            if all(fields.get(k) == v for k, v in (("showing", "true"), ("secure", "true"), ("occluded", "false"))):
                return True
            raise APIError(502, "LOCK_STATE_UNKNOWN", "无法确认安全锁屏状态")

        def unlock():
            adb("shell", "wm", "dismiss-keyguard")
            if pin is None:
                return
            self.sleeper(0.5)
            if not locked():
                return
            try:
                adb("shell", "input", "text", pin)
            except APIError as failure:
                # ADB error output can include its arguments.
                raise APIError(failure.status, failure.code, "发送锁屏 PIN 失败") from None
            for _ in range(6):
                self.sleeper(0.5)
                if not locked():
                    return
            raise APIError(422, "UNLOCK_FAILED", "输入 PIN 后锁屏仍未解除")

        def capture():
            nonlocal screenshot, screenshot_file
            data = self.adb.screenshot(serial)
            filename = hashlib.sha256(request_id.encode("ascii")).hexdigest() + ".png"
            path = self.store.screenshots / filename
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
            screenshot_file = filename
            screenshot = {"content_type": "image/png", "captured_at": stamp(self.clock())}

        try:
            try:
                perform("wake", lambda: adb("shell", "input", "keyevent", "KEYCODE_WAKEUP"))
                perform("unlock", unlock)
                if operation != "screenshot":
                    perform("home_before", lambda: adb("shell", "input", "keyevent", "KEYCODE_HOME"))
                if operation == "app/run":
                    perform("stop_before", lambda: adb("shell", "am", "force-stop", "--user", "current", params["package"]))
                    perform("launch", lambda: self.adb.launch(serial, params["package"]))
                    perform("delay", lambda: self.sleeper(params["delay"]))
                elif operation == "device/test":
                    perform("desktop_wait", lambda: self.sleeper(0.5))
                else:
                    perform("screen_wait", lambda: self.sleeper(0.5))
                perform("screenshot", capture)
                if operation == "screenshot":
                    perform("lock_after", lambda: adb("shell", "input", "keyevent", "KEYCODE_SLEEP"), is_cleanup=True)
            except APIError as failure:
                error, http_status = failure, failure.status
            finally:
                if operation == "app/run":
                    for name, key, action in [
                        ("home_after", "home", lambda: adb("shell", "input", "keyevent", "KEYCODE_HOME")),
                        ("stop_after", "force_stop", lambda: adb("shell", "am", "force-stop", "--user", "current", params["package"])),
                        ("lock_after", "lock", lambda: adb("shell", "input", "keyevent", "KEYCODE_SLEEP")),
                    ]:
                        try:
                            perform(name, action, is_cleanup=True)
                            cleanup[key] = True
                        except APIError as failure:
                            cleanup_errors.append(failure.detail())
                            if error is None:
                                error, http_status = failure, failure.status
            result = {"request_id": request_id, "status": "failed" if error else "succeeded",
                      "started_at": started, "finished_at": stamp(self.clock()), "screenshot": screenshot,
                      "error": error.detail() if error else None, "cleanup_errors": cleanup_errors, "steps": steps}
            if cleanup is not None:
                result["cleanup"] = cleanup
            self.store.finish(request_id, result, http_status, screenshot_file)
            LOG.info("Request %s (%s) finished: %s", request_id, operation, result["status"])
        except Exception:
            # Keep the durable in-flight marker; recovery will report unknown, never replay it.
            LOG.exception("Could not finalize request %s; result requires confirmation", request_id)
        finally:
            with self.guard:
                self.current_id = None
            completed.set()

    def start_maintenance(self):
        def loop():
            while not self.maintenance_stop.wait(60):
                try:
                    self.store.prune()
                except Exception:
                    LOG.exception("Result maintenance failed")
        self.maintenance_thread = threading.Thread(target=loop, name="result-maintenance", daemon=True)
        self.maintenance_thread.start()

    def close(self):
        with self.guard:
            self.closing = True
            worker = self.thread
        if worker and worker.is_alive():
            LOG.info("Waiting for active phone operation and cleanup")
            worker.join()
        self.maintenance_stop.set()
        if self.maintenance_thread:
            self.maintenance_thread.join(timeout=2)


def create_app(config, *, controller=None):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024, JSON_SORT_KEYS=False)
    app.json.ensure_ascii = False
    service = controller or Controller(config)
    app.extensions["controller"] = service

    @app.before_request
    def authorize():
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if (scheme.lower() != "bearer" or not token.isascii()
                or not hmac.compare_digest(token.encode("ascii"), config.api_key.encode("ascii"))):
            raise APIError(401, "UNAUTHORIZED", "API key 缺失或错误")

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if response.status_code == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(request_id=getattr(g, "request_id", None), error=error.detail()), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        code = error.code if error.code != 415 else 400
        return jsonify(request_id=getattr(g, "request_id", None),
                       error={"code": "INVALID_ARGUMENT" if code in {400, 413} else "HTTP_ERROR", "message": error.description, "step": None}), code

    @app.errorhandler(Exception)
    def internal_error(error):
        LOG.exception("Unhandled API error")
        return jsonify(request_id=getattr(g, "request_id", None), error=APIError(500, "INTERNAL_ERROR", "控制端内部错误，请检查日志").detail()), 500

    @app.get("/api/v1/health")
    def health():
        return jsonify(service.health())

    @app.get("/api/v1/devices")
    def devices():
        return jsonify(devices=service.adb.devices())

    def operate(operation):
        if not request.is_json:
            raise invalid("请求体须使用 application/json")
        body = request.get_json()
        if isinstance(body, dict) and isinstance(body.get("request_id"), str) and REQUEST_ID.fullmatch(body["request_id"]):
            g.request_id = body["request_id"]
        payload, status = service.submit(operation, body)
        return jsonify(payload), status

    app.add_url_rule("/api/v1/app/run", "app_run", lambda: operate("app/run"), methods=["POST"])
    app.add_url_rule("/api/v1/screenshot", "screenshot", lambda: operate("screenshot"), methods=["POST"])
    app.add_url_rule("/api/v1/device/test", "device_test", lambda: operate("device/test"), methods=["POST"])

    @app.get("/api/v1/requests/<request_id>")
    def result(request_id):
        if not REQUEST_ID.fullmatch(request_id):
            raise invalid("request_id 格式不正确")
        g.request_id = request_id
        payload, status = service.store.render(request_id, query=True)
        return jsonify(payload), status

    return app


@contextmanager
def instance_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "service.lock").open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("同一数据目录已有控制端运行") from None
    try:
        yield
    finally:
        handle.close()


def init_config(filename):
    values = {**DEFAULTS, "api_key": secrets.token_urlsafe(48)}
    filename.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects an existing deployment's key and configuration.
    with filename.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(values, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Windows ADB 控制端")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json", help="JSON 配置文件路径")
    parser.add_argument("--init-config", action="store_true", help="生成包含随机密钥的配置文件后退出，不覆盖已有文件")
    args = parser.parse_args()
    try:
        if args.init_config:
            init_config(args.config.resolve())
            print(f"已生成配置：{args.config.resolve()}（密钥请在本机配置文件中查看）")
            return
        config = Config.load(args.config)
        with instance_lock(config.data_dir):
            handler = RotatingFileHandler(config.data_dir / "controller.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                                handlers=[logging.StreamHandler(), handler])
            service = Controller(config)
            service.start_maintenance()
            try:
                from waitress import serve
                LOG.info("ADB controller %s listening on %s:%s", VERSION, config.host, config.port)
                serve(create_app(config, controller=service), host=config.host, port=config.port, threads=8)
            except KeyboardInterrupt:
                LOG.info("Stopping controller")
            finally:
                service.close()
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(1, f"启动失败：{error}\n")


if __name__ == "__main__":
    main()
